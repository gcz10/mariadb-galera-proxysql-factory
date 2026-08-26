"""Pomocnicy odtwarzania: zgodnosc wersji, bezpieczenstwo tar, czyszczenie datadir.

Czyste pomocniki procesu odtwarzania uzywane przez `pipeline.run_restore`:
bezpieczenstwo archiwum tar, inspekcja metadanych kopii, czyszczenie datadir,
przygotowanie bazy z mariabackup i kontrolowany start weryfikacyjny.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any, Optional

from .errors import BackupError
from .runner import CommandRunner
from .state import EventManager
from .textutil import quote_sql_identifier

def is_mariadb_version_compatible(backup_ver: str, target_ver: str) -> bool:
    def parse_ver(v_str: str) -> Optional[tuple[int, int]]:
        parts = v_str.strip().split(".")
        if len(parts) < 2:
            return None
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None

    backup_major_minor = parse_ver(backup_ver)
    target_major_minor = parse_ver(target_ver)
    if backup_major_minor is None or target_major_minor is None:
        return False
    return backup_major_minor <= target_major_minor


def is_safe_tar_member(member: Any) -> bool:
    name = getattr(member, "name", "")
    if not name or name.startswith("/"):
        return False

    parts = Path(name).parts
    if ".." in parts:
        return False

    is_sym = bool(getattr(member, "issym", lambda: False)()) or bool(getattr(member, "islnk", lambda: False)())
    is_fifo = bool(getattr(member, "isfifo", lambda: False)())
    is_chr = bool(getattr(member, "ischr", lambda: False)())
    is_blk = bool(getattr(member, "isblk", lambda: False)())

    if is_sym or is_fifo or is_chr or is_blk:
        return False

    is_reg = bool(getattr(member, "isreg", lambda: False)()) or bool(getattr(member, "isfile", lambda: False)())
    is_dir = bool(getattr(member, "isdir", lambda: False)())

    return is_reg or is_dir


def clear_datadir(datadir: Path) -> None:
    resolved = datadir.resolve()
    protected_roots = {
        Path("/"), Path("/bin"), Path("/boot"), Path("/dev"), Path("/etc"),
        Path("/home"), Path("/lib"), Path("/lib64"), Path("/media"), Path("/mnt"),
        Path("/opt"), Path("/proc"), Path("/root"), Path("/run"), Path("/sbin"),
        Path("/srv"), Path("/sys"), Path("/tmp"), Path("/usr"), Path("/var")
    }

    if resolved in protected_roots or not resolved.is_absolute():
        raise BackupError("E_INTEGRITY", f"Datadir '{resolved}' is a protected system directory; clearing rejected")

    if resolved.exists():
        for child in resolved.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError as exc:
                raise BackupError(
                    "E_INTEGRITY",
                    f"Failed to clear datadir entry '{child}': {exc}",
                ) from exc
    else:
        resolved.mkdir(parents=True, exist_ok=True)


def verify_restored_database(socket_path: Path, runner: CommandRunner) -> tuple[int, int, int]:
    connection_args = [f"--socket={socket_path}", "-u", "root"]
    check_code, check_out, check_err = runner.run(
        ["mariadb-check", *connection_args, "--all-databases"]
    )
    if check_code != 0:
        raise BackupError(
            "E_INTEGRITY",
            f"mariadb-check failed for restored databases: {check_err or check_out}",
        )

    db_code, db_out, db_err = runner.run(
        ["mariadb", *connection_args, "-B", "-N", "-e", "SHOW DATABASES;"]
    )
    if db_code != 0:
        raise BackupError(
            "E_INTEGRITY",
            f"Failed to list databases from restored standalone MariaDB: {db_err or db_out}",
        )

    system_databases = {"mysql", "information_schema", "performance_schema", "sys"}
    user_databases = [
        database.strip()
        for database in db_out.splitlines()
        if database.strip() and database.strip() not in system_databases
    ]
    if not user_databases:
        raise BackupError("E_INTEGRITY", "Restored database contains zero user databases")

    table_count = 0
    row_count = 0
    for database in user_databases:
        database_identifier = quote_sql_identifier(database)
        table_code, table_out, table_err = runner.run(
            [
                "mariadb",
                *connection_args,
                "-B",
                "-N",
                "-e",
                f"SHOW TABLES FROM {database_identifier};",
            ]
        )
        if table_code != 0:
            raise BackupError(
                "E_INTEGRITY",
                f"Failed to list tables in restored database '{database}': "
                f"{table_err or table_out}",
            )

        for table in (name.strip() for name in table_out.splitlines() if name.strip()):
            table_identifier = quote_sql_identifier(table)
            count_code, count_out, count_err = runner.run(
                [
                    "mariadb",
                    *connection_args,
                    "-B",
                    "-N",
                    "-e",
                    f"SELECT COUNT(*) FROM {database_identifier}.{table_identifier};",
                ]
            )
            if count_code != 0 or not count_out.strip().isdigit():
                raise BackupError(
                    "E_INTEGRITY",
                    f"Failed to count rows in restored table '{database}.{table}': "
                    f"{count_err or count_out or 'non-numeric row count'}",
                )
            table_count += 1
            row_count += int(count_out.strip())

    if table_count == 0:
        raise BackupError("E_INTEGRITY", "Restored database contains zero user tables")

    return len(user_databases), table_count, row_count


def stop_standalone_server(server_proc: Any, event_mgr: "EventManager") -> None:
    """Stop the verification-only mariadbd that this process started.

    SIGNAL it; never delegate the stop to `mariadb-admin shutdown`. mariadbd
    treats SIGTERM as a normal, InnoDB-consistent shutdown, and only this
    process can reap the child. Asking an external client to stop it deadlocks:
    mariadb-admin waits for the server PID to leave the process table, while the
    PID stays <defunct> precisely because we are blocked on mariadb-admin —
    observed in the field as a restore drill hung for 50 minutes.

    A teardown problem must never replace an in-flight error (for example
    E_INTEGRITY raised by verification), so every outcome is recorded as a
    `restore.shutdown_failure` event and swallowed.
    """
    try:
        if server_proc.poll() is None:
            server_proc.terminate()
        shutdown_code = server_proc.wait(timeout=60)
        if shutdown_code not in (0, -signal.SIGTERM):
            event_mgr.emit(
                "restore.shutdown_failure",
                {"error_code": "E_INTEGRITY", "message": f"standalone mariadbd exit {shutdown_code}"},
            )
    except subprocess.TimeoutExpired:
        event_mgr.emit(
            "restore.shutdown_failure",
            {"error_code": "E_INTEGRITY", "message": "standalone mariadbd ignored SIGTERM within 60s; killed"},
        )
        try:
            server_proc.kill()
            server_proc.wait(timeout=10)
        except Exception as kill_exc:
            event_mgr.emit(
                "restore.shutdown_failure",
                {"error_code": "E_INTEGRITY", "message": f"kill failed: {kill_exc}"},
            )
    except Exception as shutdown_exc:
        event_mgr.emit(
            "restore.shutdown_failure",
            {"error_code": "E_INTEGRITY", "message": str(shutdown_exc)},
        )
