"""Modele artefaktu kopii i odczyt znacznika czasu z metadanych."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import BackupError


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
DRILL_MARKER_FORMAT_VERSION = 1
DRILL_MARKER_S3_PREFIX = "drill-state"
DRILL_MARKER_FILENAME = "drill-state.json"


def build_drill_marker(
    cluster_name: str,
    last_success_unixtime: int,
    backup_name: str,
    rows_verified: int,
) -> dict[str, Any]:
    """Zbuduj tresc znacznika zapisywana przez udany restore drill."""
    return {
        "format_version": DRILL_MARKER_FORMAT_VERSION,
        "cluster_name": cluster_name,
        "last_success_unixtime": int(last_success_unixtime),
        "backup_name": backup_name,
        "rows_verified": int(rows_verified),
    }


def drill_marker_unixtime(marker: dict[str, Any], cluster_name: str, source: str) -> int:
    """Odczytaj unixtime ze znacznika, odrzucajac cudzy albo niezgodny format.

    Zwraca 0, gdy znacznik nie nalezy do tego klastra albo ma nieznana wersje
    formatu — metryka ma wtedy uczciwie pokazac brak potwierdzonego drillu,
    zamiast cicho przepisac wartosc z innego zrodla.
    """
    if marker.get("format_version") != DRILL_MARKER_FORMAT_VERSION:
        return 0
    if marker.get("cluster_name") != cluster_name:
        return 0
    value = marker.get("last_success_unixtime")
    if type(value) is not int or value < 0:
        raise BackupError(
            "E_STORAGE",
            f"Invalid integer last_success_unixtime in drill marker '{source}'",
        )
    return value
