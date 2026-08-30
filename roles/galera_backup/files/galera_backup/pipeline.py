"""Fasada runnera: main() + pelna powierzchnia publiczna backup/restore.

PODSZCIEP MONOLITU (F1). Dawny monolit (1498 linii) mieszka teraz w:

* `common.py`  — narzedzia wspolne: wsrep, backendy, metryki, redactor, sinki,
* `backup.py`  — elekcja donora, writer guard, zrzut, publikacja, retencja,
* `restore.py` — weryfikacja integralnosci, decrypt, copy-back, drill marker.

`galera-backup` pozostaje cienkim wrapperem wolajacym `main()` z tego modulu.

REGULA PATCHY (obowiazujaca od dekompozycji monolitu, patrz
`tests/unit/galera_backup_testlib.py`): `patch.object(modul, "nazwa")`
przechwytuje wylacznie wywolania rozwiazywane w globals TEGO modulu. Testy
przebiegow patchuja wiec `backup`/`restore`/`common`, nie te fasade. Ten modul
re-eksportuje powierzchnie uzywana BEZPOSREDNIO (atrybutowo) przez testy oraz
tests/live/probe-galera-backup-backends.py; re-eksport niczego nie przerywa,
bo bezposrenie odwolania nie wymagaja przechwytywania.
"""
import argparse
import shutil
import subprocess
import sys

from .backup import (
    assert_scheduler_is_not_writer,
    _flow_control_paused_ns,
    elect_backup_donor,
    has_retention_credential,
    perform_physical_backup,
    query_galera_vars,
    run_backup,
    run_retention,
    set_wsrep_desync,
    wait_until_synced,
)
from .common import (
    MetricsManager,
    _finalize_success_cleanup,
    _record_pre_lock_failure,
    get_module_redactor,
    get_storage_backend,
    publish_drill_freshness,
    restore_default_context,
    selinux_is_enabled,
)
from .config import RunConfig, load_run_config, load_secrets
from .errors import BackupError, combine_failures
from .fsutil import (
    atomic_write,
    file_sha256_and_size,
    remove_sensitive_work_dir,
)
from .locking import LockManager, resolve_lock_path
from .restore import run_restore
from .restore_helpers import (
    clear_datadir,
    is_mariadb_version_compatible,
    is_safe_tar_member,
    stop_standalone_server,
    verify_restored_database,
)
from .runner import CommandRunner, SecretRedactor
from .secrets import redactable_secret_values, sensitive_secret_values
from .state import EventManager, StateManager
from .storage.artifacts import (
    ArtifactSet,
    PublishedArtifact,
    build_drill_marker,
    drill_marker_unixtime,
)
from .storage.filesystem import FilesystemBackend, SMBBackend
from .storage.s3 import S3Backend
from .textutil import (
    escape_metric_label,
    quote_sql_identifier,
    sanitize_cluster_name,
    validate_smb_options,
)


# Fasada re-eksportuje cala powierzchnie uzywana BEZPOSREDNIO (atrybutowo)
# przez testy jednostkowe oraz tests/live/probe-galera-backup-backends.py.
# Pelna lista w __all__ jest tez kontraktem dla pyflakes (re-eksporty nie sa
# wtedy "imported but unused").
__all__ = [
    "ArtifactSet", "BackupError", "CommandRunner", "EventManager",
    "FilesystemBackend", "LockManager", "MetricsManager", "PublishedArtifact",
    "RunConfig", "S3Backend", "SMBBackend", "SecretRedactor", "EventManager",
    "StateManager", "assert_scheduler_is_not_writer", "atomic_write",
    "build_drill_marker", "clear_datadir", "combine_failures",
    "drill_marker_unixtime", "elect_backup_donor", "escape_metric_label",
    "file_sha256_and_size", "get_module_redactor", "get_storage_backend",
    "has_retention_credential", "is_mariadb_version_compatible",
    "is_safe_tar_member", "load_run_config", "load_secrets", "main",
    "perform_physical_backup", "publish_drill_freshness", "query_galera_vars",
    "quote_sql_identifier", "redactable_secret_values",
    "remove_sensitive_work_dir", "resolve_lock_path",
    "restore_default_context", "run_backup", "run_restore", "run_retention",
    "sensitive_secret_values", "selinux_is_enabled",
    "set_wsrep_desync", "shutil", "stop_standalone_server", "subprocess",
    "sanitize_cluster_name", "validate_smb_options", "verify_restored_database",
    "wait_until_synced", "_finalize_success_cleanup",
    "_flow_control_paused_ns", "_record_pre_lock_failure",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Galera cluster physical backup & restore runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="Run physical backup")
    backup_parser.add_argument("cluster", help="Logical cluster name")

    restore_parser = subparsers.add_parser("restore", help="Run physical restore")
    restore_parser.add_argument("cluster", help="Logical cluster name")
    restore_parser.add_argument("--confirm", action="store_true", help="Confirm destruction of restore host datadir")

    args = parser.parse_args()
    cluster_name = sanitize_cluster_name(args.cluster)

    if args.command == "backup":
        try:
            run_backup(cluster_name=cluster_name)
            return 0
        except BackupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            redactor = get_module_redactor()
            detail = redactor.redact(str(exc)) if redactor is not None else str(exc)
            print(f"ERROR: Unexpected error: {detail}", file=sys.stderr)
            return 1
    elif args.command == "restore":
        try:
            run_restore(cluster_name=cluster_name, confirm=args.confirm)
            return 0
        except BackupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            redactor = get_module_redactor()
            detail = redactor.redact(str(exc)) if redactor is not None else str(exc)
            print(f"ERROR: Unexpected error: {detail}", file=sys.stderr)
            return 1
    return 0
