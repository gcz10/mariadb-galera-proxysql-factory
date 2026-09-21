"""Modele artefaktu kopii i odczyt znacznika czasu z metadanych."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..errors import BackupError

OWNER_MARKER_FORMAT_VERSION = 1


@dataclass
class ArtifactSet:
    backup_name: str
    payload_path: Path
    checksum_path: Path
    metadata_path: Path


@dataclass
class PublishedArtifact:
    backup_name: str
    prefix: str
    encrypted_sha256: str
    encrypted_size: int
    unixtime: int


def metadata_unixtime(meta: dict[str, Any], object_name: str) -> int:
    created_ts = meta.get("created_unixtime")
    if type(created_ts) is not int or created_ts < 0:
        raise BackupError(
            "E_STORAGE",
            f"Invalid integer created_unixtime in metadata '{object_name}'",
        )
    return created_ts


# === Znacznik restore drill (ISC-36/37/49) ===
#
# POWSTAL PO REALNEJ AWARII KONTRAKTU (n13, 2026-08-18). Drill uruchamiany z crona
# biegnie na IZOLOWANYM hoscie `restore`, ktorego nikt nie scrapuje, wiec jego sukces
# nie mial jak dotrzec do metryki `isa_restore_test_last_success_unixtime`. Alert ISC-47
# "Restore drill stale" mierzyl w efekcie, kiedy ostatnio uruchomiono Ansible.
#
# Kanal, ktory JUZ istnieje miedzy hostem restore a scrapowanym hostem schedulera, to
# backend kopii (S3/MinIO albo montowany filesystem) — oba hosty i tak sie do niego
# uwierzytelniaja. Drill zostawia tam znacznik, a nocny backup (biegnacy na hoscie
# schedulera, ktory JEST scrapowany) przepisuje go do textfile collectora.
#
# Opoznienie propagacji to najwyzej jeden cykl backupu (doba) przy oknie alertu 8 dni.
#
# Znacznik LEZY POZA prefiksem `galera-<cluster>-`, ktory skanuja `fetch_latest`
# i `prune`. Dzieki temu retencja go nie kasuje i nie jest mylony z kopia.
DRILL_MARKER_FORMAT_VERSION = 2
# Czytnik przyjmuje v1 dla SWIEZOSCI (znaczniki zapisane przed v2 leza juz w
# backendach), ale pomiary pochodza WYLACZNIE z v2. v1 ich nie ma, wiec metryki
# duration/size sa wtedy POMIJANE — nigdy zerowane. Zero jest pomiarem
# ("trwalo 0 s"), brak pomiaru nie jest.
DRILL_MARKER_READABLE_FORMAT_VERSIONS = frozenset({1, 2})
DRILL_MARKER_S3_PREFIX = "drill-state"
DRILL_MARKER_FILENAME = "drill-state.json"


def validated_duration_seconds(value: Any, source: str) -> float:
    """Czas drillu: skonczona liczba >= 0. Wartosci niemozliwe -> E_STORAGE.

    `bool` jest podklasa `int`, wiec `True` przeszloby jako 1 s; odrzucamy go
    jawnie. NaN i nieskonczonosc lamia format Prometheusa NIE bledem skladni,
    tylko cicho zatruwaja serie, wiec tez sa odrzucane.
    """
    if type(value) not in (int, float):
        raise BackupError(
            "E_STORAGE",
            f"Invalid duration_seconds in drill marker '{source}': {value!r}",
        )
    duration = float(value)
    if not math.isfinite(duration) or duration < 0:
        raise BackupError(
            "E_STORAGE",
            f"Invalid duration_seconds in drill marker '{source}': {value!r}",
        )
    return duration


def validated_size_bytes(value: Any, source: str) -> int:
    """Rozmiar artefaktu: calkowita liczba >= 0. Wartosci niemozliwe -> E_STORAGE."""
    if type(value) is not int or value < 0:
        raise BackupError(
            "E_STORAGE",
            f"Invalid size_bytes in drill marker '{source}': {value!r}",
        )
    return value


def build_drill_marker(
    cluster_name: str,
    last_success_unixtime: int,
    backup_name: str,
    rows_verified: int,
    duration_seconds: float,
    size_bytes: int,
) -> dict[str, Any]:
    """Zbuduj tresc znacznika zapisywana przez udany restore drill.

    Pomiary sa walidowane PRZED publikacja i lecą tym samym snapshotem co
    znacznik, marker i metryka lokalna. Niepoprawna wartosc zatrzymuje zapis
    (fail closed) zamiast utrwalic klamstwo w backendzie.
    """
    return {
        "format_version": DRILL_MARKER_FORMAT_VERSION,
        "cluster_name": cluster_name,
        "last_success_unixtime": int(last_success_unixtime),
        "backup_name": backup_name,
        "rows_verified": int(rows_verified),
        "duration_seconds": validated_duration_seconds(duration_seconds, cluster_name),
        "size_bytes": validated_size_bytes(size_bytes, cluster_name),
    }


def _marker_belongs_to_cluster(marker: dict[str, Any], cluster_name: str) -> bool:
    """Czy znacznik pochodzi z TEGO klastra i ma znana wersje formatu.

    Wersja jest sprawdzana typem PRZED przynaleznoscia do zbioru: `in` na
    zbiorze wymaga hashable, wiec uszkodzony znacznik z lista albo slownikiem
    w `format_version` rzucalby `TypeError` zamiast dac uczciwe "nie wiem".
    """
    version = marker.get("format_version")
    if type(version) is not int or version not in DRILL_MARKER_READABLE_FORMAT_VERSIONS:
        return False
    return marker.get("cluster_name") == cluster_name


def drill_marker_unixtime(marker: dict[str, Any], cluster_name: str, source: str) -> int:
    """Odczytaj unixtime ze znacznika, odrzucajac cudzy albo niezgodny format.

    Zwraca 0, gdy znacznik nie nalezy do tego klastra albo ma nieznana wersje
    formatu — metryka ma wtedy uczciwie pokazac brak potwierdzonego drillu,
    zamiast cicho przepisac wartosc z innego zrodla.

    v1 i v2 sa rownowazne dla swiezosci: to samo pole, ta sama semantyka.
    """
    if not _marker_belongs_to_cluster(marker, cluster_name):
        return 0
    value = marker.get("last_success_unixtime")
    if type(value) is not int or value < 0:
        raise BackupError(
            "E_STORAGE",
            f"Invalid integer last_success_unixtime in drill marker '{source}'",
        )
    return value


def drill_marker_measurements(
    marker: dict[str, Any],
    cluster_name: str,
    source: str,
) -> tuple[Optional[float], Optional[int]]:
    """Odczytaj pomiary drillu (czas, rozmiar) ze znacznika v2.

    Zwraca `(None, None)` gdy pomiaru NIE MA: znacznik v1, brak pola albo
    `null`. Brak pomiaru to brak serii — konsument nie dostaje zmyslonego zera,
    ktory wygladalby jak realny, natychmiastowy drill.

    Wartosc OBECNA, ale niemozliwa (zly typ, NaN, liczba ujemna), jest bledem:
    znacznik jest uszkodzony, a nie "bez pomiaru".
    """
    if not _marker_belongs_to_cluster(marker, cluster_name):
        return (None, None)
    if marker.get("format_version") == 1:
        return (None, None)

    raw_duration = marker.get("duration_seconds")
    raw_size = marker.get("size_bytes")
    duration = None if raw_duration is None else validated_duration_seconds(raw_duration, source)
    size = None if raw_size is None else validated_size_bytes(raw_size, source)
    return (duration, size)
