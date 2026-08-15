#!/usr/bin/env python3
"""Walidacja lockfile'a platformy (ISC-63 + dual-platform).

Sprawdza:
  1. Plik jest poprawnym YAML.
  2. Brak placeholderow (to-confirm-F0 / to-verify / TODO / FIXME / XXX) — bramka ISC-63.
  3. Obecne sa wszystkie klucze konsumowane przez playbooki (lista nizej).
  4. Spojnosc wewnetrzna: mariadb.repo_setup_args pasuje do serii wynikajacej
     z mariadb.version (asercja, ktora f2_preflight egzekwuje w runtime).

Uzycie: validate-lockfile.py <lockfile.yml> [<lockfile.yml> ...]
Kod wyjscia != 0, gdy ktorykolwiek lockfile nie przeszedl.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("BLAD: brak modulu pyyaml", file=sys.stderr)
    sys.exit(2)

# Klucze konsumowane przez playbooki (z audytu R1). Lista jest intencjonalnie
# pelna — kazde dodanie nowego lock.X w playbooku musi tu dopisac pole,
# inaczej ten walidator (i CI) nie zlapie jego braku w nowym lockfilu.
REQUIRED = {
    "rocky_linux": ["major", "allowed_minors", "major_eol"],
    "mariadb": [
        "version", "repo_setup_script", "repo_setup_args", "repo_setup_sha256",
        "server_package", "client_package", "mariadb_backup_package", "rpm_release",
        "galera_provider", "galera_provider_version", "galera_provider_rpm_release",
    ],
    "proxysql": ["version", "series", "rpm_release", "rpm_sha256"],
    "docker": [
        "engine_version", "cli_version", "containerd_version",
        "buildx_version", "compose_version", "repo_baseurl", "repo_gpgkey",
    ],
    "minio": ["image", "image_digest", "sdk_version", "mc_image", "mc_image_digest"],
    "backup_tools": [
        "python_pip_package", "encryption_package", "archive_package",
        "cron_package", "cifs_userspace_package",
    ],
    "pmm": ["version", "image", "image_digest"],
    "maildev": ["image"],
    "node_exporter": ["version", "linux_sha256"],
}

# Proweniencja pinow (dodane 2026-08-15). Kazda przypieta wersja musi zapisac,
# SKAD zostala wzieta. Powod jest konkretny: `mariadb` i `proxysql` mialy tu
# przypiete wersje bez zadnego `source`, wiec nikt nie mogl zweryfikowac pinu
# inaczej niz powtarzajac cale rozeznanie — a to jest dokladnie ta droga, ktora
# wpuszcza niesprawdzona liczbe do repo i utrwala ja na lata.
# Format: (sekcja, klucz z wersja, klucz ze zrodlem).
PROVENANCE = (
    ("mariadb", "version", "source"),
    ("mariadb", "galera_provider_version", "galera_provider_source"),
    ("proxysql", "version", "source"),
    ("pmm", "version", "source"),
    ("pmm_client", "version", "source"),
    ("node_exporter", "version", "source"),
)

PLACEHOLDER_MARKERS = ("to-confirm-f0", "to-verify", "todo:", "fixme:", "xxx:")


def validate(path: Path) -> list[str]:
    """Zwraca liste bledow (pusta = OK)."""
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")

    # 1. YAML
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"{path}: niepoprawny YAML: {exc}"]
    if not isinstance(data, dict):
        return [f"{path}: korzen nie jest slownikiem"]

    # Rygor zalezy od dojrzalosci lockfile'a, ktora deklaruje on sam w stopce
    # "# Status: LOCKED" / "# Status: candidate". Kandydat jest Z DEFINICJI niekompletny
    # (placeholdery to-confirm-F0 czekaja na discovery), wiec egzekwowanie na nim bramki
    # ISC-63 i kompletu kluczy byloby falszywym alarmem. Sprawdzamy go tylko strukturalnie.
    is_locked = re.search(r"^#\s*Status:\s*LOCKED", text, re.MULTILINE | re.IGNORECASE)

    # 2. Placeholdery (ISC-63) — tylko dla LOCKED
    if is_locked:
        lower = text.lower()
        for marker in PLACEHOLDER_MARKERS:
            if marker in lower:
                errors.append(f"{path}: placeholder '{marker}' (ISC-63)")

    # 3. Wymagane klucze — komplet wymagany tylko od LOCKED; u kandydata sprawdzamy
    #    wylacznie poprawnosc typow sekcji, ktore juz istnieja.
    for section, keys in REQUIRED.items():
        if section not in data:
            if is_locked:
                errors.append(f"{path}: brak sekcji '{section}'")
            continue
        if not isinstance(data[section], dict):
            errors.append(f"{path}: sekcja '{section}' nie jest slownikiem")
            continue
        if is_locked:
            for key in keys:
                if key not in data[section]:
                    errors.append(f"{path}: brak klucza '{section}.{key}'")
        # rpm_sha256 musi miec przynajmniej jedna arch (gdy w ogole jest)
        if section == "proxysql" and isinstance(data[section].get("rpm_sha256"), dict):
            if not data[section]["rpm_sha256"]:
                errors.append(f"{path}: proxysql.rpm_sha256 pusty (brak arch)")
        if section == "minio" and "mc_image_digest" in data[section]:
            digest = str(data[section]["mc_image_digest"])
            if not re.match(r"^sha256:[a-f0-9]{64}$", digest):
                errors.append(f"{path}: minio.mc_image_digest '{digest}' ma niepoprawny format sha256")

    # 4. Spoijnosc: repo_setup_args ~ seria z mariadb.version
    mb = data.get("mariadb", {})
    version = mb.get("version", "")
    args = mb.get("repo_setup_args", "")
    if version and args:
        series = ".".join(str(version).split(".")[:2])
        if series not in str(args):
            errors.append(
                f"{path}: mariadb.repo_setup_args '{args}' nie pasuje do serii "
                f"'{series}' wynikajacej z mariadb.version '{version}'"
            )

    # 5. Proweniencja: pin musi wskazywac zrodlo, a zrodlo musi pasowac do pinu.
    #    Druga czesc lapie realny tryb bledu: podniesienie `version` bez zmiany
    #    `source`, czyli URL nadal opisujacy poprzednie wydanie.
    for section, version_key, source_key in PROVENANCE:
        block = data.get(section)
        if not isinstance(block, dict) or version_key not in block:
            continue
        source = str(block.get(source_key, "")).strip()
        if not source:
            if is_locked:
                errors.append(
                    f"{path}: brak '{section}.{source_key}' dla przypietej wersji "
                    f"'{block[version_key]}' — pin bez zapisanego zrodla"
                )
            continue
        if not re.match(r"^https?://", source):
            errors.append(f"{path}: '{section}.{source_key}' nie jest URL-em: {source}")
            continue
        version = str(block[version_key])
        series = ".".join(version.split(".")[:2])
        versions_in_url = re.findall(r"\d+\.\d+(?:\.\d+)*", source)
        if versions_in_url and version not in versions_in_url and series not in versions_in_url:
            errors.append(
                f"{path}: '{section}.{source_key}' wskazuje na {versions_in_url}, "
                f"a przypieta wersja to '{version}' — zrodlo nie zostalo "
                f"zaktualizowane po podniesieniu wersji"
            )

    return errors


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    rc = 0
    for arg in argv[1:]:
        path = Path(arg)
        if not path.is_file():
            print(f"FAIL: {path}: nie istnieje")
            rc = 1
            continue
        errs = validate(path)
        if errs:
            for e in errs:
                print(f"FAIL: {e}")
            rc = 1
        else:
            print(f"OK: {path}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
