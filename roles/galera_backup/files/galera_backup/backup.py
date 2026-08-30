"""Sciezka backupu: elekcja donora, zrzut fizyczny, publikacja, retencja.

PODSZCIEP MONOLITU (F1). `run_backup` rozwiazuje nazwy chronione testami
(`query_galera_vars`, `get_storage_backend`, `set_wsrep_desync`,
`wait_until_synced`) w GLOBALS TEGO modulu — testy przebiegow patchuja wiec
wlasnie ten modul (szczegoly: docstring `common.py` i
`tests/unit/galera_backup_testlib.py`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .common import (
    MetricsManager,
    _finalize_success_cleanup,
    _record_pre_lock_failure,
    get_storage_backend,
    publish_drill_freshness,
    set_module_redactor,
)
from .crypto import ENCRYPTION_METHOD_V2, encrypt_payload
from .config import RunConfig, load_run_config, load_secrets
from .errors import BackupError, combine_failures
from .fsutil import file_sha256_and_size, remove_sensitive_work_dir
from .locking import LockManager, resolve_lock_path
from .runner import CommandRunner, SecretRedactor
from .secrets import redactable_secret_values, sensitive_secret_values
from .state import EventManager, StateManager
from .storage.artifacts import ArtifactSet, drill_marker_unixtime
from .textutil import sanitize_cluster_name


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

    # Tozsamosc wezla, ktory FAKTYCZNIE wykonuje backup — nie tego, ktory jest
    # PREFEROWANY w cluster.yml. Po wprowadzeniu elekcji donora te dwie rzeczy
    # sie rozjezdzaja: gdy preferowany host zostal writerem, backup przejmuje
    # inny wezel. Trzymanie `scheduler_system_address` w tym zbiorze sprawialo,
    # ze wybrany donor oskarzal sam siebie o bycie writerem i backup padal
    # (E_WRITER) dokladnie w sytuacji, dla ktorej elekcja powstala.
    node_identity = str(getattr(cfg, "node_system_address", "") or "").strip()
    if not node_identity:
        # Konfiguracja sprzed elekcji: runner dzialal wylacznie na hoscie
        # wskazanym w cluster.yml, wiec preferencja BYLA tozsamoscia.
        node_identity = str(cfg.scheduler_system_address or "").strip()

    running_identities = {
        value.strip()
        for value in (node_identity, current_hostname)
        if value and value.strip()
    }
    if writers[0] in running_identities:
        raise BackupError(
            "E_WRITER",
            f"Backup donor '{current_hostname}' is the active ProxySQL writer; backup aborted",
        )


def elect_backup_donor(
    cfg: RunConfig,
    secrets: dict[str, str],
    runner: CommandRunner,
) -> str:
    """Zwraca adres wezla, ktory ma wykonac backup w TYM przebiegu.

    Zbior kandydatow bierzemy z backup hostgroup ProxySQL. Dokumentacja
    `mysql_galera_hostgroups` mowi, ze trafiaja tam wezly `read_only=0` ponad
    `max_writers` (czyli zdrowe, ale nie bedace aktywnym writerem), a wezly
    niezdrowe ida do `offline_hostgroup`. Nie musimy wiec sami liczyc zdrowia
    klastra ani prosic o uprawnienia SUPER — wystarczy konto read-only.

    `scheduler_system_address` z cluster.yml jest PREFERENCJA, nie warunkiem:
    gdy skonfigurowany wezel jest zdrowym nie-writerem, wygrywa; gdy zostal
    writerem, backup przechodzi na kolejnego kandydata zamiast padac trwale.
    """
    proxysql = cfg.proxysql
    admin_host = str(proxysql.get("admin_host", "")).strip()
    admin_port = int(proxysql.get("admin_port", 0) or 0)
    backup_hostgroup = int(proxysql.get("backup_hostgroup", 0) or 0)
    stats_user = secrets.get("GALERA_BACKUP_PROXYSQL_STATS_USER", "").strip()
    stats_password = secrets.get("GALERA_BACKUP_PROXYSQL_STATS_PASSWORD", "")

    if not admin_host or admin_port <= 0 or backup_hostgroup <= 0:
        raise BackupError(
            "E_CONFIG",
            "ProxySQL donor election configuration is missing or invalid",
        )
    if not stats_user or not stats_password:
        raise BackupError("E_SECRETS", "ProxySQL donor election credentials are missing")

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
            f"WHERE hostgroup={backup_hostgroup} AND status='ONLINE' "
            "ORDER BY srv_host"
        ),
    ]
    rc, stdout, stderr = runner.run(command, env={"MYSQL_PWD": stats_password}, timeout=20)
    if rc != 0:
        detail = (stderr or stdout or "unknown error").strip()
        raise BackupError("E_PROXYSQL", f"Cannot determine backup donor candidates: {detail}")

    # Jedna para ProxySQL obsluguje cala flote, wiec odsiewamy wezly innych
    # najemcow — dokladnie tak samo jak straznik writera.
    healthy = sorted(
        {line.strip() for line in stdout.splitlines() if line.strip()}
        & set(cfg.galera_nodes)
    )
    if not healthy:
        raise BackupError(
            "E_PROXYSQL",
            "No healthy non-writer node available for backup "
            f"(ProxySQL hostgroup {backup_hostgroup} has no ONLINE member of "
            f"{sorted(cfg.galera_nodes)})",
        )

    preferred = (cfg.scheduler_system_address or "").strip()
    return preferred if preferred in healthy else healthy[0]


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


def has_retention_credential(secrets: dict[str, str]) -> bool:
    """Czy TEN host jest koordynatorem retencji (ma klucz z prawem delete)."""
    return bool(secrets.get("GALERA_BACKUP_S3_PRUNE_ACCESS_KEY")) and bool(
        secrets.get("GALERA_BACKUP_S3_PRUNE_SECRET_KEY")
    )


def run_retention(
    cfg: RunConfig,
    secrets: dict[str, str],
    runner: Optional[CommandRunner],
    event_mgr: Any,
    backend_type: str,
    backup_backend: Any = None,
) -> None:
    """Skasuj wygasle kopie. JEDYNA sciezka runnera z prawem kasowania.

    Dla S3 buduje WLASNY backend na poswiadczeniu retencji — backend uzyty do
    publikacji nigdy nie dostaje prawa delete, wiec nie da sie przypadkiem
    skasowac historii kluczem lezacym na kazdym wezle Galery.

    Wezel bez poswiadczenia retencji konczy bez zdarzenia: dokladnie jeden host
    w klastrze jest koordynatorem, a dwa pozostale nie maja tu nic do roboty.

    Nigdy nie podnosi wyjatku. Kopia jest w tym momencie opublikowana i
    zweryfikowana; awaria retencji jest raportowana zdarzeniem i NIE degraduje
    udanego backupu (zachowanie sprzed rozdzielenia poswiadczen).
    """
    backend = backup_backend
    dedicated = None

    if backend_type == "s3":
        if not has_retention_credential(secrets):
            return
        try:
            dedicated = get_storage_backend(cfg, secrets, runner, purpose="retention")
            dedicated.preflight()
            backend = dedicated
        except BackupError as exc:
            event_mgr.emit(
                "retention.failure",
                {"error_code": exc.code, "message": exc.public_message},
            )
            return
        except Exception as exc:
            event_mgr.emit(
                "retention.failure", {"error_code": "E_STORAGE", "message": str(exc)}
            )
            return

    if backend is None:
        return

    try:
        deleted = backend.prune(datetime.now(timezone.utc), cfg.retention_days)
        event_mgr.emit("retention.success", {"deleted": int(deleted or 0)})
    except BackupError as exc:
        event_mgr.emit(
            "retention.failure",
            {"error_code": exc.code, "message": exc.public_message},
        )
    except Exception as exc:
        event_mgr.emit(
            "retention.failure", {"error_code": "E_STORAGE", "message": str(exc)}
        )
    finally:
        if dedicated is not None:
            try:
                dedicated.close()
            except Exception:
                pass


def run_backup(
    config_path: Optional[Path] = None,
    secrets_path: Optional[Path] = None,
    cluster_name: str = "",
) -> None:
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
    set_module_redactor(redactor)

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
        curr_host = socket.gethostname().split(".")[0]

        # Cron stoi na kazdym wezle Galery, ale backup w danym przebiegu robi
        # DOKLADNIE JEDEN — wybrany tu donor. Wezel niewybrany konczy sie rc=0
        # (to nie jest blad: jego zadaniem bylo sprawdzic, czy jest potrzebny).
        # Wczesniej host byl przypiety na stale, wiec failover NA niego zabieral
        # klastrowi backupy az do recznej zmiany cluster.yml.
        donor = elect_backup_donor(cfg, secrets, runner)
        me = (cfg.node_system_address or "").strip()
        if not me:
            # Fail-closed. Warunek `if me and donor != me` znaczyl przy pustej
            # tozsamosci "nigdy nie pomijaj", wiec kazdy wezel cronowy uznawal
            # sie za donora i backup ruszal rownolegle na calym klastrze —
            # blokady sa lokalne dla hosta i nie koordynuja wezlow.
            raise BackupError(
                "E_CONFIG",
                "node_system_address is empty: this node cannot tell whether it "
                "was elected backup donor",
            )
        if donor != me:
            event_mgr.emit("skipped.not_elected", {"donor": donor, "node": me})
            print(
                f"galera-backup: {curr_host} nie jest donorem w tym przebiegu "
                f"(wybrany: {donor}) — pomijam"
            )
            # Retencja nalezy do KOORDYNATORA, nie do donora: gdyby biegla tylko
            # w sciezce backupu, kazde przejecie backupu przez inny wezel
            # zatrzymywaloby kasowanie wygaslych kopii az do powrotu preferencji.
            run_retention(cfg, secrets, runner, event_mgr, b_type)
            return

        # Druga, niezalezna warstwa: nawet gdyby elekcja sie pomylila, backup
        # nigdy nie leci z aktywnego writera (ISC-39).
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

        # Encrypt in-process: AES-256-GCM (AEAD — integralnosc priorytetem
        # wrogiem magazynu; openssl enc nie umie AEAD). Klucz bez zmian.
        encrypt_payload(tar_file, payload_file, secrets["GALERA_BACKUP_ENCRYPTION_KEY"])

        # Remove unencrypted tar
        if tar_file.exists():
            tar_file.unlink()

        enc_sha, enc_size = file_sha256_and_size(payload_file)

        checksum_file.write_text(f"{enc_sha}  backup.tar.enc\n", encoding="utf-8")

        created_iso = datetime.now(timezone.utc).isoformat()
        meta = {
            "format_version": 2,
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
            "encryption_method": ENCRYPTION_METHOD_V2,
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

        # Retencja — jedyna sciezka z prawem kasowania. Dla S3 buduje wlasny
        # backend na poswiadczeniu koordynatora; backend publikacji (obecny na
        # kazdym wezle) nie ma prawa delete. Awarie sa raportowane zdarzeniem i
        # nigdy nie degraduja zapisanego wyzej sukcesu.
        run_retention(cfg, secrets, runner, event_mgr, b_type, backend)

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
