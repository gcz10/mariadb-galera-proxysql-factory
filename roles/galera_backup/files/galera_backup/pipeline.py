"""Rdzen runnera: orkiestracja backupu i odtwarzania.

Ten modul jest tym, co laduje `tests/unit/galera_backup_testlib.py`. Powod jest
scisly, nie stylistyczny: testy podmieniaja szesc symboli przez
`patch.object(self.mod, ...)` — `query_galera_vars`, `get_storage_backend`,
`perform_physical_backup`, `assert_scheduler_is_not_writer`,
`restore_default_context`, `selinux_is_enabled`. Zeby mock cokolwiek
przechwycil, ich WYWOLUJACY (`run_backup`, `run_restore`, `MetricsManager.update`)
musza rozwiazywac te nazwy w TEJ SAMEJ przestrzeni nazw. Dlatego rdzen i
chronione symbole mieszkaja razem, a `galera-backup` jest juz tylko cienkim
wrapperem wolajacym `main()`.
"""

from __future__ import annotations

import os
import sys
import json
import time
import signal
import shutil
import hashlib
import tarfile
import tempfile
import argparse
import subprocess
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


from .errors import BackupError, combine_failures
from .textutil import (
    escape_metric_label,
    quote_sql_identifier,
    sanitize_cluster_name,
    validate_smb_options,
)
from .fsutil import (
    atomic_write,
    file_sha256_and_size,
    remove_sensitive_work_dir,
)
from .storage.artifacts import (
    ArtifactSet,
    PublishedArtifact,
    build_drill_marker,
    drill_marker_unixtime,
)
from .storage.filesystem import FilesystemBackend, SMBBackend
from .storage.s3 import S3Backend
from .secrets import redactable_secret_values, sensitive_secret_values
from .runner import CommandRunner, SecretRedactor
from .config import RunConfig, load_run_config, load_secrets
from .locking import LockManager, resolve_lock_path
from .state import EventManager, StateManager
from .restore_helpers import (
    clear_datadir,
    is_mariadb_version_compatible,
    is_safe_tar_member,
    stop_standalone_server,
    verify_restored_database,
)


# Te trzy nazwy nie sa uzywane WEWNATRZ tego modulu, ale sa czescia jego
# powierzchni publicznej: siegaja po nie testy jednostkowe oraz
# tests/live/probe-galera-backup-backends.py. Pozostale 15 re-eksportow bylo
# martwym spadkiem po monolicie i zostalo usuniete.
__all__ = ["PublishedArtifact", "quote_sql_identifier", "validate_smb_options"]


# Installed by run_backup/run_restore once secrets are loaded, so main() can

# redact unexpected exceptions before they reach journald. None until then —
# main() prints unchanged when no redactor has been installed yet.
_module_redactor: Optional["SecretRedactor"] = None




def selinux_is_enabled() -> bool:
    return Path("/sys/fs/selinux/enforce").exists()


def restore_default_context(target_path: Path) -> None:
    if not selinux_is_enabled():
        return
    restorecon = shutil.which("restorecon")
    if restorecon is None:
        raise BackupError(
            "E_METRICS",
            "SELinux is enabled but restorecon is unavailable",
        )
    result = subprocess.run(
        [restorecon, "-F", str(target_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise BackupError(
            "E_METRICS",
            f"Failed to restore the default security context on {target_path}: {detail}",
        )







def assert_scheduler_is_not_writer(
    cfg: RunConfig,
    secrets: dict[str, str],
    runner: CommandRunner,
    current_hostname: str,
) -> None:
    proxysql = cfg.proxysql
    admin_host = str(proxysql.get("admin_host", "")).strip()
    admin_port = int(proxysql.get("admin_port", 0) or 0)
    writer_hostgroup = int(proxysql.get("writer_hostgroup", 0) or 0)
    stats_user = secrets.get("GALERA_BACKUP_PROXYSQL_STATS_USER", "").strip()
    stats_password = secrets.get("GALERA_BACKUP_PROXYSQL_STATS_PASSWORD", "")

    if not admin_host or admin_port <= 0 or writer_hostgroup <= 0:
        raise BackupError(
            "E_CONFIG",
            "ProxySQL writer guard configuration is missing or invalid",
        )
    if not stats_user or not stats_password:
        raise BackupError(
            "E_SECRETS",
            "ProxySQL writer guard credentials are missing",
        )

    # `stats_mysql_connection_pool` zamiast `runtime_mysql_servers`: konto z
    # admin-stats_credentials NIE widzi schematu konfiguracyjnego (zmierzone na
    # ProxySQL 3.0: "ERROR 1045 no such table"), a obie tabele daja ten sam
    # obraz writera. Kolumny tez sa inne: hostgroup/srv_host, nie
    # hostgroup_id/hostname. Dzieki temu runner na wezle Galery trzyma
    # poswiadczenie, ktorym nie da sie nic zapisac.
    command = [
        "mariadb",
        "--protocol=tcp",
        "-h",
        admin_host,
        "-P",
        str(admin_port),
        "-u",
        stats_user,
        "-N",
        "-B",
        "-e",
        (
            "SELECT srv_host FROM stats_mysql_connection_pool "
            f"WHERE hostgroup={writer_hostgroup} AND status='ONLINE' "
            "ORDER BY srv_host"
        ),
    ]
    rc, stdout, stderr = runner.run(
        command,
        env={"MYSQL_PWD": stats_password},
        timeout=20,
    )
    if rc != 0:
        detail = (stderr or stdout or "unknown error").strip()
        raise BackupError(
            "E_PROXYSQL",
            f"Cannot determine active ProxySQL writer: {detail}",
        )

    writers = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(writers) != 1:
        raise BackupError(
            "E_PROXYSQL",
            f"Expected exactly one ONLINE ProxySQL writer, found {len(writers)}",
        )

    # The writer guard is only meaningful against this cluster's own ProxySQL.
    # If the configured node list is known, a writer outside it means we are
    # querying a foreign ProxySQL and the guard cannot be enforced.
    if cfg.galera_nodes and writers[0] not in cfg.galera_nodes:
        raise BackupError(
            "E_PROXYSQL",
            f"ProxySQL writer '{writers[0]}' is not one of this cluster's Galera nodes "
            f"{sorted(cfg.galera_nodes)}; the writer guard cannot be enforced against a foreign ProxySQL",
        )

    scheduler_identities = {
        value.strip()
        for value in (
            cfg.scheduler_system_address,
            cfg.scheduler_system_hostname,
            current_hostname,
        )
        if value and value.strip()
    }
    if writers[0] in scheduler_identities:
        raise BackupError(
            "E_WRITER",
            f"Backup scheduler '{current_hostname}' is the active ProxySQL writer; backup aborted",
        )



class MetricsManager:
    def __init__(
        self,
        metric_path: Path,
        cluster_label: str,
        logical_cluster: str,
        backend_label: str,
        metric_prefix: str = "galera_backup",
    ):
        self.metric_path = metric_path
        self.c_label = escape_metric_label(cluster_label)
        self.lc_label = escape_metric_label(logical_cluster)
        self.b_label = escape_metric_label(backend_label)
        self.prefix = metric_prefix

    def update(
        self,
        last_success_unixtime: int = 0,
        last_failure_unixtime: int = 0,
        last_run_success: int = 0,
        last_size_bytes: int = 0,
        last_duration_seconds: float = 0.0,
    ) -> None:
        labels = f'cluster="{self.c_label}",logical_cluster="{self.lc_label}",backend="{self.b_label}"'
        content = (
            f"{self.prefix}_last_success_unixtime{{{labels}}} {last_success_unixtime}\n"
            f"{self.prefix}_last_failure_unixtime{{{labels}}} {last_failure_unixtime}\n"
            f"{self.prefix}_last_run_success{{{labels}}} {last_run_success}\n"
            f"{self.prefix}_last_size_bytes{{{labels}}} {last_size_bytes}\n"
            f"{self.prefix}_last_duration_seconds{{{labels}}} {last_duration_seconds:.3f}\n"
        )
        atomic_write(self.metric_path, content, mode=0o644)
        restore_default_context(self.metric_path)


def publish_drill_freshness(
    metric_path: Path,
    cluster_label: str,
    logical_cluster: str,
    backend_label: str,
    last_success_unixtime: int,
) -> None:
    """Przepisz swiezosc restore drillu ze znacznika backendu do textfile collectora.

    Wolane z hosta SCHEDULERA podczas backupu, bo to on jest scrapowany. Sam drill
    biegnie na izolowanym hoscie `restore`, ktorego nikt nie odpytuje — bez tego
    mostka jego sukces nigdy nie dociera do alertu ISC-47.

    Nazwa metryki jest CELOWO ta sama, ktorej uzywa runner na hoscie restore
    (`galera_restore_last_success_unixtime`): regula alertu bierze `max` po serii,
    wiec obie sciezki uruchomienia — cron i Ansible — trafiaja w ten sam licznik.
    """
    labels = (
        f'cluster="{escape_metric_label(cluster_label)}",'
        f'logical_cluster="{escape_metric_label(logical_cluster)}",'
        f'backend="{escape_metric_label(backend_label)}"'
    )
    content = (
        "# Zrodlo: znacznik restore drill w backendzie kopii (patrz storage/artifacts.py).\n"
        "# Wartosc 0 oznacza brak potwierdzonego drillu dla tego klastra.\n"
        "# HELP galera_restore_last_success_unixtime Unix time of the last successful restore drill.\n"
        "# TYPE galera_restore_last_success_unixtime gauge\n"
        f"galera_restore_last_success_unixtime{{{labels}}} {int(last_success_unixtime)}\n"
    )
    atomic_write(metric_path, content, mode=0o644)
    restore_default_context(metric_path)


def query_galera_vars(socket_path: Path, runner: CommandRunner) -> dict[str, str]:
    cmd = ["mariadb", f"--socket={socket_path}", "-u", "root", "-e", "SHOW GLOBAL STATUS LIKE 'wsrep_%';"]
    code, out, err = runner.run(cmd)
    if code != 0:
        raise BackupError("E_GALERA", f"Failed to query Galera status via socket '{socket_path}': {err or out}")

    vars_dict: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 2:
            vars_dict[parts[0].lower()] = parts[1]
        else:
            parts_space = line.strip().split(maxsplit=1)
            if len(parts_space) == 2:
                vars_dict[parts_space[0].lower()] = parts_space[1]

    return vars_dict


def set_wsrep_desync(socket_path: Path, runner: CommandRunner, enable: bool) -> bool:
    """Przelacz wsrep_desync. Zwraca True TYLKO gdy stan zmienilo TO wywolanie.

    Wezel w stanie innym niz Synced (4) juz jest odsynchronizowany — przez SST
    do innego wezla albo przez operatora. Ustawienie desync=OFF w `finally`
    zdjeloby wtedy CUDZY desync i wciagnelo dawce z powrotem do przesylania
    zapisow w srodku transferu. Dlatego wlaczamy wylacznie ze stanu Synced,
    a wylaczamy wylacznie to, co sami wlaczylismy.
    """
    if enable:
        state = query_galera_vars(socket_path, runner).get("wsrep_local_state", "")
        if state != "4":
            return False
    value = "ON" if enable else "OFF"
    code, out, err = runner.run(
        ["mariadb", f"--socket={socket_path}", "-u", "root", "-e", f"SET GLOBAL wsrep_desync = {value};"]
    )
    if code != 0:
        raise BackupError("E_GALERA", f"SET GLOBAL wsrep_desync={value} failed: {err or out}")
    return True


def _flow_control_paused_ns(gal_vars: dict[str, str]) -> int:
    """Odczytaj wsrep_flow_control_paused_ns; brak zmiennej to jawny E_GALERA.

    Domysl "0" przy braku zmiennej wylaczal zabezpieczenie flow control po
    cichu: SHOW GLOBAL STATUS bez tej pozycji znaczy "nie wiemy, czy backup
    nie zatrzymal klastra", a nie "klastra na pewno nie zatrzymal".
    """
    raw = gal_vars.get("wsrep_flow_control_paused_ns")
    if raw is None or str(raw).strip() == "":
        raise BackupError(
            "E_GALERA",
            "Brak wsrep_flow_control_paused_ns w SHOW GLOBAL STATUS; odmawiam backupu bez straznika flow control",
        )
    return int(raw)


def wait_until_synced(socket_path: Path, runner: CommandRunner, timeout_s: int = 900) -> None:
    """Czekaj az wezel wroci do Synced po wsrep_desync=OFF.

    Powrot nie jest natychmiastowy: wezel musi nadrobic kolejke zapisow
    zgromadzona w czasie backupu. Bez tego oczekiwania runner konczy sie
    sukcesem, zostawiajac wezel w Donor/Desynced — ProxySQL trzyma go poza
    ruchem, a nastepna brama zdrowia odrzuca caly klaster.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        state = query_galera_vars(socket_path, runner).get("wsrep_local_state_comment", "")
        if state == "Synced":
            return
        if time.monotonic() >= deadline:
            raise BackupError("E_GALERA", f"wezel utknal w stanie '{state}' po wsrep_desync=OFF (limit {timeout_s}s)")
        time.sleep(5)


def get_storage_backend(cfg: RunConfig, secrets: dict[str, str], runner: Optional["CommandRunner"] = None) -> Any:
    b_type = str(cfg.backend.get("type", cfg.backend.get("destination", ""))).lower()
    if b_type == "s3":
        return S3Backend(
            endpoint=cfg.backend["endpoint"],
            bucket=cfg.backend["bucket"],
            secure=cfg.backend.get("secure", False),
            access_key=secrets["GALERA_BACKUP_S3_ACCESS_KEY"],
            secret_key=secrets["GALERA_BACKUP_S3_SECRET_KEY"],
            cluster_name=cfg.cluster_name,
        )
    elif b_type == "smb":
        return SMBBackend(
            source=cfg.backend["source"],
            mount_point=cfg.backend["mount_point"],
            options=cfg.backend.get("options", []),
            username=secrets["GALERA_BACKUP_SMB_USERNAME"],
            password=secrets["GALERA_BACKUP_SMB_PASSWORD"],
            domain=secrets.get("GALERA_BACKUP_SMB_DOMAIN"),
            cluster_name=cfg.cluster_name,
            runner=runner,
        )
    elif b_type == "filesystem":
        return FilesystemBackend(
            mount_point=cfg.backend["mount_point"],
            expected_fstype=cfg.backend.get("expected_fstype", ""),
            cluster_name=cfg.cluster_name,
        )
    else:
        raise BackupError("E_CONFIG", f"Unknown backend type '{b_type}'")


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


def _record_pre_lock_failure(
    cluster_dir: Path,
    cluster_name: str,
    command: str,
    exc: Exception,
    redactor: SecretRedactor,
    metrics_mgr: Optional["MetricsManager"] = None,
) -> None:
    """Best-effort state/event/metric sink for failures raised before the lock.

    Without this, an E_CONFIG/E_SECRETS/E_SECRETS_PERM abort leaves the previous
    night's last_run_success=1 in the textfile and every dashboard stays green.
    Sink nigdy nie moze zastapic oryginalnego bledu: kazdy sink jest chroniony
    przed dowolnym zwyklym wyjatkiem (nie tylko OSError), a wywolujacy ponownie
    rzuca oryginalny wyjatek.
    """
    err_code = exc.code if isinstance(exc, BackupError) else "E_STORAGE"
    err_msg = exc.public_message if isinstance(exc, BackupError) else str(exc)
    now_ts = int(time.time())
    try:
        StateManager(cluster_name, cluster_dir / "state.json").update_failure(
            command, now_ts, err_code, redactor.redact(err_msg)
        )
    except Exception:
        pass
    try:
        EventManager(cluster_dir / "events.jsonl", redactor).emit(
            "state.failure",
            {"error_code": err_code, "error_message": redactor.redact(err_msg)},
        )
    except Exception:
        pass
    if metrics_mgr is not None:
        try:
            last_succ_time = 0
            try:
                last_succ = StateManager(cluster_name, cluster_dir / "state.json").read().get("last_success")
                last_succ_time = last_succ.get("unixtime", 0) if last_succ else 0
            except Exception:
                pass
            metrics_mgr.update(
                last_success_unixtime=last_succ_time,
                last_failure_unixtime=now_ts,
                last_run_success=0,
            )
        except Exception:
            pass


def _finalize_success_cleanup(
    event_mgr: EventManager,
    backend: Any,
    work_dir: Optional[Path],
    work_dir_error_code: str,
) -> None:
    """Epilog po zapisanym sukcesie backupu albo restore, wykonywany najlepszym wysilkiem.

    Zamkniecie backendu i usuniecie workdir sa porzadkowaniem po zweryfikowanym
    sukcesie. Ich porazka nie moze nadpisac state.success ani metryki sukcesu:
    zapisujemy osobne cleanup.failure z faza, a sam epilog nie rzuca wyjatku.
    Sink zdarzen tez jest najlepszym wysilkiem, zeby jego awaria nie reaktywowala
    problemu, ktory ten helper ma usunac.
    """

    def report(phase: str, cleanup_exc: Exception, default_code: str) -> None:
        error_code = cleanup_exc.code if isinstance(cleanup_exc, BackupError) else default_code
        message = cleanup_exc.public_message if isinstance(cleanup_exc, BackupError) else str(cleanup_exc)
        try:
            event_mgr.emit(
                "cleanup.failure",
                {"error_code": error_code, "message": message, "phase": phase},
            )
        except Exception:
            # Awaria sinka zdarzen tez nie moze zdegradowac zapisanego sukcesu.
            pass

    if backend is not None:
        try:
            backend.close()
        except Exception as cleanup_exc:
            report("backend.close", cleanup_exc, "E_STORAGE")
    if work_dir is not None:
        try:
            remove_sensitive_work_dir(work_dir, work_dir_error_code)
        except Exception as cleanup_exc:
            report("workdir.remove", cleanup_exc, work_dir_error_code)


def run_backup(
    config_path: Optional[Path] = None,
    secrets_path: Optional[Path] = None,
    cluster_name: str = "",
) -> None:
    global _module_redactor
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
            config_path.parent, cluster_name, "backup", exc, SecretRedactor([])
        )
        raise

    b_type = str(cfg.backend.get("type", cfg.backend.get("destination", ""))).lower()
    try:
        secrets = load_secrets(
            secrets_path,
            backend_type=b_type,
            enforce_permissions=True,
            require_writer_credentials=bool(cfg.proxysql),
        )
    except Exception as exc:
        _record_pre_lock_failure(
            cfg.paths.cluster_dir,
            cluster_name,
            "backup",
            exc,
            SecretRedactor([]),
            MetricsManager(cfg.paths.metric_file, cfg.metric_cluster_label, cluster_name, b_type),
        )
        raise

    redactor = SecretRedactor(redactable_secret_values(secrets))
    runner = CommandRunner(sensitive_secret_values(secrets), redactor=redactor)
    _module_redactor = redactor

    state_mgr = StateManager(cluster_name, cfg.paths.cluster_dir / "state.json")
    event_mgr = EventManager(cfg.paths.cluster_dir / "events.jsonl", redactor)
    metrics_mgr = MetricsManager(cfg.paths.metric_file, cfg.metric_cluster_label, cluster_name, b_type)

    lock_mgr = LockManager(resolve_lock_path(cluster_name, cfg.paths.cluster_dir))
    now_ts = int(time.time())

    try:
        lock_mgr.acquire()
    except BackupError as exc:
        state_mgr.update_locked("backup", now_ts)
        event_mgr.emit("locked", {"error_code": exc.code, "message": exc.public_message})
        last_succ_ts = (state_mgr.read().get("last_success") or {}).get("unixtime", 0)
        metrics_mgr.update(last_success_unixtime=last_succ_ts, last_failure_unixtime=now_ts, last_run_success=0)
        raise
    start_time = time.time()
    work_dir: Optional[Path] = None
    backend = None

    def _sig_handler(signum: int, frame: Any) -> None:
        raise BackupError("E_STORAGE", f"Backup process interrupted by signal {signum}")

    old_term = signal.signal(signal.SIGTERM, _sig_handler)
    old_int = signal.signal(signal.SIGINT, _sig_handler)
    try:
        state_mgr.read()
        # Check hostname
        curr_host = socket.gethostname().split(".")[0]
        sched_host = cfg.scheduler_system_hostname.split(".")[0] if cfg.scheduler_system_hostname else ""
        if sched_host and curr_host != sched_host:
            raise BackupError(
                "E_GALERA",
                f"Current hostname '{curr_host}' does not match configured scheduler hostname '{sched_host}'"
            )
        assert_scheduler_is_not_writer(cfg, secrets, runner, curr_host)

        backend = get_storage_backend(cfg, secrets, runner)
        event_mgr.emit("backend.preflight", {"backend": b_type})
        backend.preflight()

        # Check Galera status
        gal_vars = query_galera_vars(cfg.paths.socket, runner)
        state_comment = gal_vars.get("wsrep_local_state_comment", "")
        cluster_status = gal_vars.get("wsrep_cluster_status", "")
        ready = gal_vars.get("wsrep_ready", "")
        connected = gal_vars.get("wsrep_connected", "")
        cluster_size = int(gal_vars.get("wsrep_cluster_size", "0"))

        if (
            state_comment != "Synced"
            or cluster_status != "Primary"
            or ready != "ON"
            or connected != "ON"
            or cluster_size != cfg.galera_nodes_expected
        ):
            raise BackupError(
                "E_GALERA",
                f"Galera is not fully healthy for backup: Synced={state_comment}, Primary={cluster_status}, "
                f"ready={ready}, connected={connected}, size={cluster_size}/{cfg.galera_nodes_expected}"
            )

        fc_initial = _flow_control_paused_ns(gal_vars)

        # Free space check
        cfg.paths.staging_root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(cfg.paths.staging_root)
        if usage.free < 500 * 1024 * 1024:
            raise BackupError("E_STORAGE", f"Insufficient free disk space on staging root {cfg.paths.staging_root}")

        backup_name = f"galera-{cluster_name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        work_dir = Path(tempfile.mkdtemp(prefix=f"{backup_name}-", dir=str(cfg.paths.staging_root)))
        os.chmod(work_dir, 0o700)

        event_mgr.emit("mariadb-backup.backup", {"backup_name": backup_name})
        # Odsynchronizowanie na czas zrzutu fizycznego. Bez tego mariadb-backup
        # nasyca dysk wezla, kolejka aplikacyjna rosnie i Galera wlacza flow
        # control dla CALEGO klastra — writer stojacy na innym wezle przestaje
        # commitowac. Runner widzial ten skutek dopiero po fakcie (fc_delta
        # nizej) i odrzucal gotowa kopie: luka RPO zamiast zapobiegania.
        desynced = set_wsrep_desync(cfg.paths.socket, runner, True)
        event_mgr.emit("galera.desync", {"applied": desynced})
        try:
            wsrep_uuid, wsrep_seqno = perform_physical_backup(work_dir, cfg.paths.datadir, cfg.paths.socket, runner)
        finally:
            if desynced:
                set_wsrep_desync(cfg.paths.socket, runner, False)
                wait_until_synced(cfg.paths.socket, runner)
                event_mgr.emit("galera.resync", {"state": "Synced"})

        raw_dir = work_dir / "raw"
        tar_file = work_dir / "backup.tar"
        payload_file = work_dir / "backup.tar.enc"
        checksum_file = work_dir / "backup.sha256"
        metadata_file = work_dir / "metadata.json"

        # Create tar and compute plaintext sha. stdout is streamed to disk so the
        # archive is never buffered in memory; stderr must still be drained or a
        # full 64 KiB pipe buffer deadlocks tar while we hold the flock, failing
        # every later cron run with E_LOCKED.
        # Create tar: stream stdout directly to disk, drain stderr in communicate()
        with open(tar_file, "wb") as f_tar:
            tar_proc: Optional[subprocess.Popen] = None
            try:
                tar_proc = subprocess.Popen(
                    ["tar", "-cf", "-", "-C", str(raw_dir), "."],
                    stdout=f_tar,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                _, tar_err = tar_proc.communicate(timeout=1800)
                if tar_proc.returncode != 0:
                    raise BackupError(
                        "E_STORAGE",
                        f"tar compression failed: {redactor.redact((tar_err or b'').decode('utf-8', 'replace'))}",
                    )
            except subprocess.TimeoutExpired:
                if tar_proc is not None:
                    try:
                        os.killpg(os.getpgid(tar_proc.pid), signal.SIGKILL)
                    except Exception:
                        tar_proc.kill()
                    tar_proc.communicate()
                raise BackupError("E_STORAGE", "tar archiving timed out after 1800s")
            except BaseException:
                if tar_proc is not None and tar_proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(tar_proc.pid), signal.SIGKILL)
                    except Exception:
                        tar_proc.kill()
                    tar_proc.wait()
                raise
        hasher = hashlib.sha256()
        with open(tar_file, "rb") as f_tar:
            while True:
                chunk = f_tar.read(65536)
                if not chunk:
                    break
                hasher.update(chunk)
        plaintext_sha = hasher.hexdigest()

        # Encrypt with OpenSSL
        cmd_enc = [
            "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
            "-md", "sha256", "-salt",
            "-in", str(tar_file),
            "-out", str(payload_file),
            "-pass", "env:GALERA_BACKUP_ENCRYPTION_KEY",
        ]
        code, out, err = runner.run(cmd_enc, env={"GALERA_BACKUP_ENCRYPTION_KEY": secrets["GALERA_BACKUP_ENCRYPTION_KEY"]})
        if code != 0:
            raise BackupError("E_STORAGE", f"OpenSSL encryption failed: {err or out}")

        # Remove unencrypted tar
        if tar_file.exists():
            tar_file.unlink()

        enc_sha, enc_size = file_sha256_and_size(payload_file)

        checksum_file.write_text(f"{enc_sha}  backup.tar.enc\n", encoding="utf-8")

        created_iso = datetime.now(timezone.utc).isoformat()
        meta = {
            "format_version": 1,
            "cluster_name": cluster_name,
            "backup_name": backup_name,
            "source_host": curr_host,
            "created_at": created_iso,
            "created_at_utc": created_iso,
            "created_unixtime": int(time.time()),
            "mariadb_version": cfg.mariadb_version,
            "wsrep_uuid": wsrep_uuid,
            "wsrep_seqno": wsrep_seqno,
            "sha256_plaintext": plaintext_sha,
            "plaintext_sha256": plaintext_sha,
            "sha256_encrypted": enc_sha,
            "encrypted_sha256": enc_sha,
            "size_bytes": enc_size,
            "encrypted_size_bytes": enc_size,
            "encryption_method": "aes-256-cbc-pbkdf2-iter200k-sha256",
            "backend": b_type,
            "backend_type": b_type,
        }
        metadata_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        # Check flow control delta
        gal_vars_final = query_galera_vars(cfg.paths.socket, runner)
        fc_final = _flow_control_paused_ns(gal_vars_final)
        fc_delta = fc_final - fc_initial

        if fc_delta > cfg.flow_control_threshold_ns:
            raise BackupError(
                "E_FLOW_CONTROL",
                f"Excessive flow control pause delta ({fc_delta} ns > threshold {cfg.flow_control_threshold_ns} ns); backup aborted before publication"
            )

        # Publish
        art_set = ArtifactSet(
            backup_name=backup_name,
            payload_path=payload_file,
            checksum_path=checksum_file,
            metadata_path=metadata_file,
        )
        event_mgr.emit("backend.publish", {"backup_name": backup_name})
        backend.publish(art_set)
        event_mgr.emit("backend.verify", {"backup_name": backup_name})

        # The backup is published and verified at this point: record success
        # BEFORE retention. A prune failure (e.g. one corrupt metadata.json)
        # must not downgrade a verified, published backup to failed.
        duration = time.time() - start_time
        state_mgr.update_success("backup", int(time.time()), artifact=backup_name)
        event_mgr.emit("state.success", {"backup_name": backup_name, "duration_seconds": duration, "size_bytes": enc_size})
        metrics_mgr.update(
            last_success_unixtime=int(time.time()),
            last_failure_unixtime=state_mgr.read().get("last_failure", {}).get("unixtime", 0) if state_mgr.read().get("last_failure") else 0,
            last_run_success=1,
            last_size_bytes=enc_size,
            last_duration_seconds=duration,
        )

        # Prune — retention housekeeping; failures are reported but never
        # downgrade the successful run recorded above.
        try:
            backend.prune(datetime.now(timezone.utc), cfg.retention_days)
        except BackupError as prune_exc:
            event_mgr.emit(
                "retention.failure",
                {"error_code": prune_exc.code, "message": prune_exc.public_message},
            )
        except Exception as prune_exc:
            event_mgr.emit(
                "retention.failure",
                {"error_code": "E_STORAGE", "message": str(prune_exc)},
            )

        # Most swiezosci restore drillu: host schedulera JEST scrapowany, izolowany
        # host `restore` nie jest. Przepisujemy tu znacznik zostawiony przez drill
        # w backendzie, zeby alert ISC-47 widzial realne wykonanie drillu, a nie
        # date ostatniego uruchomienia Ansible. Jak retencja wyzej: awaria mostka
        # jest raportowana zdarzeniem i NIGDY nie degraduje udanego backupu.
        try:
            marker = backend.read_drill_marker()
            publish_drill_freshness(
                cfg.paths.metric_file.parent / f"galera_restore_drill-{cluster_name}.prom",
                cfg.metric_cluster_label,
                cluster_name,
                b_type,
                drill_marker_unixtime(marker, cluster_name, "backend drill marker") if marker else 0,
            )
        except BackupError as drill_exc:
            event_mgr.emit(
                "drill_freshness.failure",
                {"error_code": drill_exc.code, "message": drill_exc.public_message},
            )
        except Exception as drill_exc:
            event_mgr.emit(
                "drill_freshness.failure",
                {"error_code": "E_STORAGE", "message": str(drill_exc)},
            )

        # Po zapisanym state.success cleanup jest best-effort: jego porazka
        # dostaje cleanup.failure i nie moze zdegradowac zweryfikowanego
        # backupu do state.failure.
        completed_backend = backend
        backend = None
        completed_work_dir = work_dir
        work_dir = None
        _finalize_success_cleanup(event_mgr, completed_backend, completed_work_dir, "E_STORAGE")
        print(f"galera-backup backup for {cluster_name} completed successfully ({backup_name}, {enc_size} bytes)")
    except Exception as exc:
        duration = time.time() - start_time
        failure = exc
        if backend:
            try:
                backend.close()
            except Exception as cleanup_exc:
                failure = combine_failures(failure, cleanup_exc, "E_STORAGE")
            backend = None

        try:
            remove_sensitive_work_dir(work_dir, "E_STORAGE")
            work_dir = None
        except BackupError as cleanup_exc:
            failure = combine_failures(failure, cleanup_exc, "E_STORAGE")

        err_code = failure.code if isinstance(failure, BackupError) else "E_STORAGE"
        err_msg = failure.public_message if isinstance(failure, BackupError) else str(failure)
        if err_code == "E_STATE":
            event_mgr.emit(
                "state.failure",
                {"error_code": err_code, "error_message": redactor.redact(err_msg)},
            )
            last_succ_ts = (state_mgr.read().get("last_success") or {}).get("unixtime", 0)
            metrics_mgr.update(
                last_success_unixtime=last_succ_ts,
                last_failure_unixtime=int(time.time()),
                last_run_success=0,
                last_duration_seconds=duration,
            )
            if failure is exc:
                raise
            raise failure

        state_mgr.update_failure("backup", int(time.time()), err_code, redactor.redact(err_msg))
        event_mgr.emit("state.failure", {"error_code": err_code, "error_message": redactor.redact(err_msg)})

        last_succ = state_mgr.read().get("last_success", {})
        last_succ_time = last_succ.get("unixtime", 0) if last_succ else 0
        try:
            metrics_mgr.update(
                last_success_unixtime=last_succ_time,
                last_failure_unixtime=int(time.time()),
                last_run_success=0,
                last_size_bytes=0,
                last_duration_seconds=duration,
            )
        except Exception as metrics_exc:
            # A metric write failure (e.g. restorecon E_METRICS) must not replace
            # the real diagnostic: record it and re-raise the ORIGINAL failure.
            event_mgr.emit(
                "metrics.write_failure",
                {"error_code": "E_METRICS", "message": str(metrics_exc)},
            )
        if failure is exc:
            raise
        raise failure
    finally:
        try:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
        except Exception:
            pass
        try:
            if backend:
                backend.close()
        finally:
            lock_mgr.release()
def run_restore(
    config_path: Optional[Path] = None,
    secrets_path: Optional[Path] = None,
    cluster_name: str = "",
    confirm: bool = False,
) -> None:
    global _module_redactor
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
    _module_redactor = redactor

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
        if meta.get("format_version") != 1 or meta.get("cluster_name") != cluster_name:
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

        # 3. Decrypt payload
        tar_path = work_dir / "backup.tar"
        cmd_dec = [
            "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
            "-md", "sha256",
            "-in", str(art_set.payload_path),
            "-out", str(tar_path),
            "-pass", "env:GALERA_BACKUP_ENCRYPTION_KEY",
        ]
        code, out, err = runner.run(cmd_dec, env={"GALERA_BACKUP_ENCRYPTION_KEY": secrets["GALERA_BACKUP_ENCRYPTION_KEY"]})
        if code != 0:
            raise BackupError("E_INTEGRITY", f"Decryption failed: {err or out}")

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
            detail = _module_redactor.redact(str(exc)) if _module_redactor is not None else str(exc)
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
            detail = _module_redactor.redact(str(exc)) if _module_redactor is not None else str(exc)
            print(f"ERROR: Unexpected error: {detail}", file=sys.stderr)
            return 1
    return 0
