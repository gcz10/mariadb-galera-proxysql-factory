"""Sciezka odtwarzania: weryfikacja integralnosci, decrypt, copy-back, drill.

PODSZCIEP MONOLITU (F1). `run_restore` rozwiazuje nazwy chronione testami
(`get_storage_backend`) w GLOBALS TEGO modulu — testy restore patchuja wiec
wlasnie ten modul (szczegoly: docstring `common.py` i
`tests/unit/galera_backup_testlib.py`).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Optional
from .crypto import SUPPORTED_FORMAT_VERSIONS, decrypt_payload

from .common import (
    MetricsManager,
    _finalize_success_cleanup,
    _record_pre_lock_failure,
    get_storage_backend,
    set_module_redactor,
)
from .config import load_run_config, load_secrets
from .errors import BackupError, combine_failures
from .fsutil import file_sha256_and_size, remove_sensitive_work_dir
from .locking import LockManager, resolve_lock_path
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
from .storage.artifacts import build_drill_marker
from .textutil import sanitize_cluster_name


def run_restore(
    config_path: Optional[Path] = None,
    secrets_path: Optional[Path] = None,
    cluster_name: str = "",
    confirm: bool = False,
) -> None:
    if not confirm:
        raise BackupError("E_RESTORE_CONFIRM", "Restore operation requires --confirm flag")

    # The cluster name is untrusted input; there is no safe path to write state
    # before it validates, so a failure here stays unrecorded by design.
    cluster_name = sanitize_cluster_name(cluster_name)

    if config_path is None:
        config_path = Path("/opt/galera-backup/clusters") / cluster_name / "config.json"
    if secrets_path is None:
        secrets_path = Path("/opt/galera-backup/clusters") / cluster_name / "secrets.env"

    try:
        cfg = load_run_config(config_path, cluster_name)
    except Exception as exc:
        # metric_cluster_label is unknown without the config, so no metric is
        # written here; the textfile-freeze alert covers this case.
        _record_pre_lock_failure(
            config_path.parent, cluster_name, "restore", exc, SecretRedactor([])
        )
        raise

    # Validate local_role and hostname disjointness
    if cfg.local_role != "restore":
        raise BackupError("E_RESTORE_CONFIRM", f"Restore host local_role must be 'restore', got '{cfg.local_role}'")

    curr_host = socket.gethostname().split(".")[0]
    sched_host = cfg.scheduler_system_hostname.split(".")[0] if cfg.scheduler_system_hostname else ""
    if sched_host and curr_host == sched_host:
        raise BackupError("E_RESTORE_CONFIRM", f"Restore cannot run on configured scheduler host '{curr_host}'")

    b_type = str(cfg.backend.get("type", cfg.backend.get("destination", ""))).lower()
    try:
        secrets = load_secrets(secrets_path, backend_type=b_type, enforce_permissions=True)
    except Exception as exc:
        _record_pre_lock_failure(
            cfg.paths.cluster_dir,
            cluster_name,
            "restore",
            exc,
            SecretRedactor([]),
            MetricsManager(cfg.paths.metric_file, cfg.metric_cluster_label, cluster_name, b_type),
        )
        raise

    redactor = SecretRedactor(redactable_secret_values(secrets))
    runner = CommandRunner(sensitive_secret_values(secrets), redactor=redactor)
    set_module_redactor(redactor)

    state_mgr = StateManager(cluster_name, cfg.paths.cluster_dir / "state.json")
    event_mgr = EventManager(cfg.paths.cluster_dir / "events.jsonl", redactor)
    metrics_mgr = MetricsManager(
        cfg.paths.metric_file.parent / f"galera_restore-{cluster_name}.prom",
        cfg.metric_cluster_label,
        cluster_name,
        b_type,
        metric_prefix="galera_restore",
    )

    lock_mgr = LockManager(resolve_lock_path(cluster_name, cfg.paths.cluster_dir))
    now_ts = int(time.time())
    try:
        lock_mgr.acquire()
    except BackupError as exc:
        state_mgr.update_locked("restore", now_ts)
        event_mgr.emit("locked", {"error_code": exc.code, "message": exc.public_message})
        raise

    backend = None
    work_dir: Optional[Path] = None
    try:
        state_mgr.read()
        backend = get_storage_backend(cfg, secrets, runner)
        event_mgr.emit("restore.preflight", {"backend": b_type})
        backend.preflight()

        cfg.paths.staging_root.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix=f"restore-{cluster_name}-", dir=str(cfg.paths.staging_root)))
        os.chmod(work_dir, 0o700)
        event_mgr.emit("restore.fetch_latest", {})
        art_set = backend.fetch_latest(work_dir)

        # 1. Check metadata
        meta = json.loads(art_set.metadata_path.read_text(encoding="utf-8"))
        if meta.get("format_version") not in SUPPORTED_FORMAT_VERSIONS or meta.get("cluster_name") != cluster_name:
            raise BackupError("E_INTEGRITY", "Metadata format or cluster mismatch in fetched backup")

        b_ver = meta.get("mariadb_version", "")
        if b_ver and cfg.mariadb_version:
            if not is_mariadb_version_compatible(b_ver, cfg.mariadb_version):
                raise BackupError(
                    "E_INTEGRITY",
                    f"Backup MariaDB version '{b_ver}' is newer than restore host '{cfg.mariadb_version}'"
                )

        # 2. Check encrypted size and SHA-256 without loading the backup into RAM.
        enc_sha, enc_size = file_sha256_and_size(art_set.payload_path)
        expected_enc_sha = str(meta.get("encrypted_sha256", ""))
        try:
            expected_enc_size = int(meta["encrypted_size_bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupError(
                "E_INTEGRITY",
                f"Backup metadata has an invalid encrypted_size_bytes value: {exc}",
            ) from exc
        if enc_size != expected_enc_size:
            raise BackupError(
                "E_INTEGRITY",
                f"Encrypted payload size mismatch: expected {expected_enc_size}, got {enc_size}",
            )
        if enc_sha != expected_enc_sha:
            raise BackupError(
                "E_INTEGRITY",
                f"Encrypted payload SHA-256 mismatch: expected {expected_enc_sha}, got {enc_sha}",
            )

        # 3. Decrypt payload — v3 (strumieniowy GCM), v2 (GCM one-shot)
        # albo legacy v1 (CBC przez openssl, kopie sprzed migracji).
        tar_path = work_dir / "backup.tar"
        decrypt_payload(
            art_set.payload_path,
            tar_path,
            secrets["GALERA_BACKUP_ENCRYPTION_KEY"],
            runner,
        )

        # 4. Check plaintext SHA-256 without loading the tar archive into RAM.
        plain_sha, _ = file_sha256_and_size(tar_path)
        expected_plain_sha = meta.get("plaintext_sha256", "")
        if plain_sha != expected_plain_sha:
            raise BackupError(
                "E_INTEGRITY",
                f"Plaintext SHA-256 mismatch: expected {expected_plain_sha}, got {plain_sha}"
            )

        # 5. Inspect tar safety and extract
        extracted_dir = work_dir / "extracted"
        extracted_dir.mkdir(parents=True, exist_ok=True)

        with tarfile.open(tar_path) as tf:
            for member in tf.getmembers():
                if not is_safe_tar_member(member):
                    raise BackupError("E_INTEGRITY", f"Unsafe tar member '{member.name}' rejected")
            if sys.version_info >= (3, 12):
                tf.extractall(extracted_dir, filter="data")
            else:
                tf.extractall(extracted_dir)

        # The plaintext archive has served its purpose; remove it immediately,
        # mirroring the backup path, so decrypted data never lingers on disk.
        if tar_path.exists():
            tar_path.unlink()

        # 6. Clear datadir & copy-back
        clear_datadir(cfg.paths.datadir)

        cmd_cb = ["mariadb-backup", "--copy-back", f"--target-dir={extracted_dir}", f"--datadir={cfg.paths.datadir}"]
        code, out, err = runner.run(cmd_cb)
        if code != 0:
            raise BackupError("E_INTEGRITY", f"mariadb-backup --copy-back failed: {err or out}")

        # Restore ownership before starting the isolated verification server.
        if os.geteuid() == 0:
            try:
                shutil.chown(cfg.paths.datadir, user="mysql", group="mysql")
                for root, dirs, files in os.walk(cfg.paths.datadir):
                    for directory in dirs:
                        shutil.chown(Path(root) / directory, user="mysql", group="mysql")
                    for file_name in files:
                        shutil.chown(Path(root) / file_name, user="mysql", group="mysql")
            except Exception as exc:
                raise BackupError(
                    "E_INTEGRITY",
                    f"Failed to set mysql ownership on restored datadir '{cfg.paths.datadir}': {exc}",
                ) from exc

        # 7. Start standalone MariaDB
        standalone_log = work_dir / "standalone.log"
        standalone_pid_file = work_dir / "standalone.pid"

        if os.geteuid() == 0:
            try:
                # Grant mysql traverse-only access to the work directory (it
                # must reach the pid/log files below) without handing over the
                # whole directory, then own just those two files.
                os.chmod(work_dir, 0o711)
                for standalone_file in (standalone_log, standalone_pid_file):
                    standalone_file.touch()
                    shutil.chown(standalone_file, user="mysql", group="mysql")
            except Exception as exc:
                raise BackupError(
                    "E_INTEGRITY",
                    f"Failed to prepare mysql-owned standalone files in '{work_dir}': {exc}",
                ) from exc
        server_bin = "mariadbd"
        if not shutil.which("mariadbd") and shutil.which("mysqld"):
            server_bin = "mysqld"

        cmd_server = [
            server_bin,
            f"--datadir={cfg.paths.datadir}",
            f"--socket={cfg.paths.socket}",
            f"--pid-file={standalone_pid_file}",
            f"--log-error={standalone_log}",
            "--wsrep-provider=none",
            "--skip-networking",
        ]
        if os.geteuid() == 0:
            cmd_server.append("--user=mysql")

        server_proc = None
        total_rows = 0
        try:
            server_proc = subprocess.Popen(cmd_server, start_new_session=True)
            # Wait for socket
            start_wait = time.time()
            connected = False
            while time.time() - start_wait < 30:
                if cfg.paths.socket.exists():
                    # Test connection with mariadb CLI
                    c_code, c_out, _ = runner.run(["mariadb", f"--socket={cfg.paths.socket}", "-u", "root", "-e", "SELECT 1;"])
                    if c_code == 0:
                        connected = True
                        break
                time.sleep(0.5)

            if not connected:
                log_tail = standalone_log.read_text() if standalone_log.exists() else ""
                raise BackupError("E_INTEGRITY", f"Standalone MariaDB failed to produce socket within 30s. Log: {log_tail[-500:]}")

            # 8. Verify all schemas/tables, then execute a query against every
            # user table. Empty tables are valid; missing user tables are not.
            database_count, table_count, total_rows = verify_restored_database(
                cfg.paths.socket,
                runner,
            )

        finally:
            if server_proc:
                stop_standalone_server(server_proc, event_mgr)

        # Znacznik drillu MUSI powstac, zanim zamkniemy backend — to jedyny kanal
        # laczacy izolowany host `restore` ze scrapowanym hostem schedulera.
        # Odtwarzanie juz sie UDALO i zostalo zweryfikowane, wiec awaria samego
        # znacznika nie moze zdegradowac drillu do porazki: raportujemy ja
        # zdarzeniem, dokladnie jak robi to retencja po udanym backupie.
        drill_unixtime = int(time.time())
        completed_backend = backend
        backend = None
        try:
            completed_backend.write_drill_marker(
                build_drill_marker(
                    cluster_name=cluster_name,
                    last_success_unixtime=drill_unixtime,
                    backup_name=art_set.backup_name,
                    rows_verified=total_rows,
                )
            )
        except BackupError as marker_exc:
            event_mgr.emit(
                "drill_marker.failure",
                {"error_code": marker_exc.code, "message": marker_exc.public_message},
            )
        except Exception as marker_exc:
            event_mgr.emit(
                "drill_marker.failure",
                {"error_code": "E_STORAGE", "message": str(marker_exc)},
            )
        completed_work_dir = work_dir
        work_dir = None
        # Wspolny epilog backupu i restore: porazka close/usuniecia workdir
        # jest osobnym cleanup.failure, a nie porazka zweryfikowanego drillu.
        _finalize_success_cleanup(event_mgr, completed_backend, completed_work_dir, "E_INTEGRITY")

        state_mgr.update_success(
            "restore",
            int(time.time()),
            artifact={
                "backup_name": art_set.backup_name,
                "databases_verified": database_count,
                "tables_verified": table_count,
                "rows_verified": total_rows,
            },
        )
        event_mgr.emit(
            "state.restore_success",
            {
                "backup_name": art_set.backup_name,
                "databases_verified": database_count,
                "tables_verified": table_count,
                "rows_verified": total_rows,
            },
        )
        metrics_mgr.update(
            last_success_unixtime=int(time.time()),
            last_run_success=1,
        )
        print(
            f"galera-backup restore for {cluster_name} completed successfully "
            f"({art_set.backup_name}, {database_count} databases, "
            f"{table_count} tables, {total_rows} rows checked)"
        )

    except Exception as exc:
        failure = exc
        if backend:
            try:
                backend.close()
            except Exception as cleanup_exc:
                failure = combine_failures(failure, cleanup_exc, "E_INTEGRITY")
            backend = None

        try:
            remove_sensitive_work_dir(work_dir, "E_INTEGRITY")
            work_dir = None
        except BackupError as cleanup_exc:
            failure = combine_failures(failure, cleanup_exc, "E_INTEGRITY")

        if isinstance(failure, BackupError):
            err_code = failure.code
            err_msg = failure.public_message
        else:
            err_code = "E_INTEGRITY"
            err_msg = str(failure)

        if err_code == "E_STATE":
            event_mgr.emit(
                "state.restore_failure",
                {"error_code": err_code, "error_message": redactor.redact(err_msg)},
            )
            if failure is exc:
                raise
            raise failure

        state_mgr.update_failure("restore", int(time.time()), err_code, redactor.redact(err_msg))
        event_mgr.emit("state.restore_failure", {"error_code": err_code, "error_message": redactor.redact(err_msg)})
        last_succ = state_mgr.read().get("last_success", {})
        last_succ_time = last_succ.get("unixtime", 0) if last_succ else 0
        try:
            metrics_mgr.update(
                last_success_unixtime=last_succ_time,
                last_failure_unixtime=int(time.time()),
                last_run_success=0,
            )
        except Exception as metrics_exc:
            # A metric write failure must not replace the real diagnostic:
            # record it and re-raise the ORIGINAL failure below.
            event_mgr.emit(
                "metrics.write_failure",
                {"error_code": "E_METRICS", "message": str(metrics_exc)},
            )
        if failure is exc:
            raise
        raise failure
    finally:
        try:
            if backend:
                backend.close()
        finally:
            lock_mgr.release()
