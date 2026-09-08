#!/usr/bin/env python3
"""Walidacja lockfile'a platformy (ISC-63 + dual-platform).

Sprawdza:
  1. Plik jest poprawnym YAML.
  2. Brak placeholderow (to-confirm-F0 / to-verify / TODO / FIXME / XXX) — bramka ISC-63.
  3. Obecne sa wszystkie klucze konsumowane przez playbooki (lista nizej).
  4. Spojnosc wewnetrzna: mariadb.repo_setup_args pasuje do serii wynikajacej
     z mariadb.version (asercja, ktora f2_preflight egzekwuje w runtime).
  5. Proweniencja pinow (wskazanie zrodla dla kazdej wersji).
  6. Zgodnosc rocky_linux_major: deklaracja w cluster.yml / platform.yml
     musi zgadzac sie z rocky_linux.major w przypietym lockfile.

Uzycie:
  validate-lockfile.py [<lockfile.yml> ...]
  validate-lockfile.py --declaration <cluster.yml|platform.yml> [...]
Kod wyjscia != 0, gdy ktorykolwiek lockfile lub deklaracja nie przeszla.
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
        "python_pip_package", "encryption_package", "crypto_package",
        "archive_package", "cron_package", "cifs_userspace_package",
    ],
    "pmm": ["version", "image", "image_digest"],
    # P1-A (audyt 2026-09): lancuch dostaw percona-release musi miec piny
    # integralnosci — bez nich agent_install.yml nie ma czym zweryfikowac
    # pobieranego RPM i klucza GPG.
    "pmm_client": [
        "version", "package", "repo_component", "release_rpm_version",
        "release_rpm", "release_rpm_sha256", "gpg_key_url",
        "gpg_key_sha256", "gpg_fingerprint",
    ],
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

REPO_ROOT = Path(__file__).resolve().parents[2]


def declaration_references(repo_root: Path = REPO_ROOT) -> list[tuple[Path, int | None, Path, str]]:
    """Zwraca (decl_path, decl_major, resolved_lock_path, rel_lock_path) ze wszystkich definicji."""
    decls = []
    for pattern in ("clusters/*/cluster.yml", "platform/*/platform.yml"):
        for decl_path in sorted(repo_root.glob(pattern)):
            try:
                cfg = yaml.safe_load(decl_path.read_text(encoding="utf-8")) or {}
            except (yaml.YAMLError, OSError):
                continue
            if not isinstance(cfg, dict):
                continue
            lock_rel = ((cfg.get("versions") or {}) if isinstance(cfg.get("versions"), dict) else {}).get("lock_file")
            if not lock_rel:
                continue
            platform_block = cfg.get("platform") or {}
            decl_major = platform_block.get("rocky_linux_major") if isinstance(platform_block, dict) else None
            try:
                decl_major_int = int(decl_major) if decl_major is not None else None
            except (ValueError, TypeError):
                decl_major_int = None
            decls.append((decl_path, decl_major_int, (repo_root / str(lock_rel)).resolve(), str(lock_rel)))
    return decls


def referenced_lockfiles(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Lockfile'e WSKAZANE przez definicje klastrow i platformy (`versions.lock_file`)."""
    return {lock_resolved for _, _, lock_resolved, _ in declaration_references(repo_root)}


def compare_major_agreement(decl_path: Path, decl_major: int | None, lock_path: Path, lock_data: dict) -> list[str]:
    """Weryfikuje, czy decl_major jest zgodny z rocky_linux.major w lock_data."""
    if decl_major is None:
        return []
    rl_block = lock_data.get("rocky_linux") or {}
    if not isinstance(rl_block, dict):
        return []
    lock_major = rl_block.get("major")
    if lock_major is None:
        return []
    try:
        lock_major_int = int(lock_major)
    except (ValueError, TypeError):
        return []
    if decl_major != lock_major_int:
        return [
            f"{decl_path}: platform.rocky_linux_major ({decl_major}) "
            f"nie zgadza sie z rocky_linux.major ({lock_major_int}) w lockfile '{lock_path}'"
        ]
    return []


def validate_declaration(decl_path: Path, repo_root: Path = REPO_ROOT) -> list[str]:
    """Weryfikuje wskazywany lockfile i zgodnosc rocky_linux_major dla pojedynczej deklaracji."""
    if not decl_path.is_file():
        return [f"{decl_path}: plik nie istnieje"]
    try:
        cfg = yaml.safe_load(decl_path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        return [f"{decl_path}: nie da sie wczytac ({exc})"]
    if not isinstance(cfg, dict):
        return [f"{decl_path}: korzen nie jest slownikiem"]

    lock_rel = ((cfg.get("versions") or {}) if isinstance(cfg.get("versions"), dict) else {}).get("lock_file")
    if not lock_rel:
        return [f"{decl_path}: brak versions.lock_file"]

    lock_path = (repo_root / str(lock_rel)).resolve()
    if not lock_path.is_file():
        return [f"{decl_path}: lock_file '{lock_rel}' nie istnieje"]

    referenced = referenced_lockfiles(repo_root) | {lock_path}
    errors = [f"{decl_path} -> {e}" for e in validate(lock_path, referenced=referenced, repo_root=repo_root)]

    platform_block = cfg.get("platform") or {}
    decl_major = platform_block.get("rocky_linux_major") if isinstance(platform_block, dict) else None
    try:
        decl_major_int = int(decl_major) if decl_major is not None else None
    except (ValueError, TypeError):
        decl_major_int = None

    try:
        lock_data = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
        if isinstance(lock_data, dict):
            errors.extend(compare_major_agreement(decl_path, decl_major_int, lock_path, lock_data))
    except (yaml.YAMLError, OSError) as exc:
        errors.append(f"{decl_path}: blad odczytu lockfile '{lock_rel}': {exc}")

    return errors


def validate(path: Path, referenced: set[Path] | None = None, repo_root: Path | None = None) -> list[str]:
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

    # Rygor pelny obowiazuje, gdy plik SAM deklaruje dojrzalosc ("# Status: LOCKED")
    # ALBO gdy wskazuje go jakikolwiek `cluster.yml`. Kandydat, ktorego nikt nie
    # wskazuje, jest z definicji niekompletny (placeholdery czekaja na discovery)
    # i sprawdzamy go tylko strukturalnie — ale kandydat, ktorym ktos BUDUJE
    # klaster, przestaje byc szkicem i podlega bramce ISC-63 jak kazdy inny.
    #
    # Sama etykieta w stopce NIE jest bledem: bramka pilnuje TRESCI pliku, nie
    # jego nazwy dojrzalosci. Kompletny kandydat wskazany przez jedna definicje
    # (promocja pinow testowana na jednym klastrze) przechodzi — niekompletny
    # nie przejdzie, i o to w tej bramce chodzi.
    if referenced is None:
        referenced = referenced_lockfiles()
    is_locked = bool(re.search(r"^#\s*Status:\s*LOCKED", text, re.MULTILINE | re.IGNORECASE))
    is_locked = is_locked or path.resolve() in referenced

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
        # P1-A: piny integralnosci lancucha percona-release musza byc wazne,
        # zanim agent_install uzylby ich jako checksum/fingerprint.
        if section == "pmm_client" and is_locked:
            block = data[section]
            for key in ("release_rpm_sha256", "gpg_key_sha256"):
                value = str(block.get(key, ""))
                if value and not re.match(r"^[a-f0-9]{64}$", value):
                    errors.append(f"{path}: pmm_client.{key} '{value}' ma niepoprawny format sha256")
            fp = str(block.get("gpg_fingerprint", ""))
            if fp and not re.match(r"^[A-F0-9]{40}$", fp):
                errors.append(f"{path}: pmm_client.gpg_fingerprint '{fp}' nie jest 40-znakowym odciskiem GPG")

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

    # 6. Zgodnosc rocky_linux_major z deklaracjami klastrow i platformy
    root = repo_root
    if root is None:
        if (path.parent.parent / "clusters").is_dir() or (path.parent.parent / "platform").is_dir():
            root = path.parent.parent
        else:
            root = REPO_ROOT
    resolved_path = path.resolve()
    for decl_path, decl_major, lock_resolved, _ in declaration_references(root):
        if lock_resolved == resolved_path:
            errors.extend(compare_major_agreement(decl_path, decl_major, path, data))

    return errors


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        rc = 0
        referenced = referenced_lockfiles()
        if not referenced:
            print("FAIL: zaden klaster ani platforma nie wskazuje lockfile")
            return 1
        for path in sorted(referenced):
            if not path.is_file():
                print(f"FAIL: {path}: nie istnieje")
                rc = 1
                continue
            errs = validate(path, referenced)
            if errs:
                for e in errs:
                    print(f"FAIL: {e}")
                rc = 1
            else:
                print(f"OK: {path}")
        return rc

    if argv[1] == "--declaration":
        if len(argv) < 3:
            print(f"Uzycie: {argv[0]} --declaration <cluster.yml|platform.yml> [...]", file=sys.stderr)
            return 2
        rc = 0
        for decl_arg in argv[2:]:
            decl_path = Path(decl_arg)
            errs = validate_declaration(decl_path)
            if errs:
                for e in errs:
                    print(f"FAIL: {e}")
                rc = 1
            else:
                print(f"OK: {decl_path} (lockfile & rocky_linux_major)")
        return rc

    rc = 0
    referenced = referenced_lockfiles()
    for arg in argv[1:]:
        path = Path(arg)
        if not path.is_file():
            print(f"FAIL: {path}: nie istnieje")
            rc = 1
            continue
        errs = validate(path, referenced)
        if errs:
            for e in errs:
                print(f"FAIL: {e}")
            rc = 1
        else:
            print(f"OK: {path}")
    return rc

if __name__ == "__main__":
    sys.exit(main(sys.argv))
