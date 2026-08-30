"""Strazniki bezpieczenstwa backupu: writer-guard i elekcja donora.

Wydzielone z pipeline.py (refaktor strukturalny, zachowanie 1:1).
pipeline.py pozostaje facade: re-eksportuje te nazwy, zeby testy
(patch.object(pipeline, ...)) i tests/live dalej rozwiazywaly je w jednym
namespace — to jest kontrakt udokumentowany w docstringu pipeline.py.
"""

from __future__ import annotations

from pathlib import Path

from .errors import BackupError
from .runner import CommandRunner
from .config import RunConfig


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


