"""Fizyczny backup mariadb-backup --backup + --prepare i odczyt galera-info.

Wydzielone z pipeline.py (refaktor strukturalny, zachowanie 1:1).
pipeline.py pozostaje facade: re-eksportuje te nazwy, zeby testy
(patch.object(pipeline, ...)) i tests/live dalej rozwiazywaly je w jednym
namespace — to jest kontrakt udokumentowany w docstringu pipeline.py.
"""

from __future__ import annotations

from pathlib import Path

from .errors import BackupError
from .runner import CommandRunner


def perform_physical_backup(work_dir: Path, datadir: Path, socket: Path, runner: CommandRunner) -> tuple[str, str]:
    raw_dir = work_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # 1. mariadb-backup --backup --galera-info
    cmd_backup = [
        "mariadb-backup",
        "--backup",
        "--galera-info",
        f"--target-dir={raw_dir}",
        f"--socket={socket}",
        "--user=root",
    ]
    code, out, err = runner.run(cmd_backup)
    if code != 0:
        raise BackupError("E_GALERA", f"mariadb-backup --backup failed: {err or out}")

    # 2. mariadb-backup --prepare
    cmd_prepare = ["mariadb-backup", "--prepare", f"--target-dir={raw_dir}"]
    code, out, err = runner.run(cmd_prepare)
    if code != 0:
        raise BackupError("E_GALERA", f"mariadb-backup --prepare failed: {err or out}")

    # 3. Read wsrep UUID & seqno
    info_file = raw_dir / "mariadb_backup_galera_info"
    if not info_file.exists():
        raise BackupError("E_GALERA", f"mariadb_backup_galera_info file missing at '{info_file}' after backup")

    info_content = info_file.read_text(encoding="utf-8").strip()
    parts = info_content.split()
    if len(parts) < 2:
        raise BackupError("E_GALERA", f"Malformed mariadb_backup_galera_info content: '{info_content}'")

    wsrep_uuid, wsrep_seqno = parts[0], parts[1]
    return wsrep_uuid, wsrep_seqno
