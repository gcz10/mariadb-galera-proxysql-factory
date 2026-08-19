#!/usr/bin/env python3
"""Verify ProxySQL Galera routing on all ProxySQL nodes.

Checks: ISC-18 (exactly one active writer), ISC-19 (only healthy nodes in
active hostgroups / offline HG empty when cluster healthy), ISC-20 (Galera
monitor converged), ISC-21 (runtime == disk, no drift), ISC-22 (default
admin credentials rejected), ISC-23 (read/write split off — no query rules).

Requires `/etc/proxysql/admin-check.cnf` deployed by F7 on each ProxySQL node.
"""

from __future__ import annotations

import sys

from _probe_common import (
    ProbeContext,
    check,
    finish,
    require_hosts,
    run_ansible,
)


def admin_query(query: str, factory_default: bool = False) -> str:
    """Zbuduj polecenie administracyjne wykonywane przez wspolny parser."""
    env_prefix = "MYSQL_PWD=admin " if factory_default else ""
    auth = "" if factory_default else "--defaults-extra-file=/etc/proxysql/admin-check.cnf "
    return f'{env_prefix}mariadb {auth}-h127.0.0.1 -P6032 -uadmin -N -B -e "{query}"'


def main() -> int:
    failures: list[str] = []
    undetermined: list[str] = []
    ctx = ProbeContext()
    proxysql_hosts = ctx.group_hosts("proxysql")
    expected_backends = int(ctx.config["galera"]["nodes_expected"])
    hg_base = int(ctx.config.get("proxysql", {}).get("hostgroup_base", 10))
    writer_hg = hg_base + 0
    backup_hg = hg_base + 10
    reader_hg = hg_base + 20
    offline_hg = hg_base + 30

    # Runtime server distribution per ProxySQL node
    servers_res = run_ansible(
        ctx,
        "proxysql",
        admin_query(
            "SELECT hostgroup_id, hostname, status FROM runtime_mysql_servers "
            "ORDER BY hostgroup_id, hostname"
        ),
    )
    # Guard calej grupy: brak odpowiedzi hosta nie moze zniknac jako pusty pomiar.
    require_hosts(
        servers_res,
        proxysql_hosts,
        "ISC-18..20 runtime_mysql_servers",
        failures,
        undetermined,
    )

    servers_rows = {}
    for node in proxysql_hosts:
        if node not in servers_res.bodies:
            continue
        body = servers_res.body(node)
        rows = [ln.split("\t") for ln in body.splitlines() if "\t" in ln]
        servers_rows[node] = rows
        online_writers = [r for r in rows if r[0] == str(writer_hg) and r[2] == "ONLINE"]
        online_offline = [r for r in rows if r[0] == str(offline_hg) and r[2] == "ONLINE"]
        healthy_backends = {
            r[1]
            for r in rows
            if r[0] in (str(writer_hg), str(backup_hg)) and r[2] == "ONLINE"
        }

        # ISC-18: exactly one active (ONLINE) writer
        check(
            len(online_writers) == 1,
            f"{node}: {len(online_writers)} ONLINE writers in HG {writer_hg} (expected 1)",
            failures,
        )
        # ISC-19 steady state: no healthy node stuck in offline HG when cluster is healthy
        check(
            len(online_offline) == 0,
            f"{node}: {len(online_offline)} node(s) ONLINE in offline HG {offline_hg} "
            f"(expected 0 when cluster healthy)",
            failures,
        )
        # ISC-20: Galera monitor converged — all expected backends are healthy
        check(
            len(healthy_backends) == expected_backends,
            f"{node}: {len(healthy_backends)} healthy backends in active HGs "
            f"(expected {expected_backends})",
            failures,
        )

    # ISC-19 (rzeczywistosc, nie widok proxy): kazdy wezel ONLINE w aktywnej
    # hostgrupie MUSI byc w Primary Component i Synced wedlug monitora Galery.
    #
    # Bez tej kontroli sonda przechodzila na zielono w stanie, w ktorym aplikacja
    # dostawala smieci. Zmierzone na newclaude8-r9 przy utracie kworum (2 z 3
    # wezlow zgaszone): monitor ProxySQL raportowal primary_partition=NO oraz
    # wsrep_local_state=0, a mimo to wezel zostawal ONLINE w hostgrupie writera.
    # Klient przez VIP dostawal wtedy "ERROR 2027 Received malformed packet"
    # zamiast czystego "ERROR 1047 (08S01) WSREP has not yet prepared node",
    # ktory ten sam wezel zwracal przy polaczeniu bezposrednim.
    #
    # SPROSTOWANIE (n11, eksperyment rozrozniajacy). Zdanie "ProxySQL nie reaguje
    # na primary_partition=NO" bylo ZA SZEROKIE i jest nieprawdziwe. Gdy poza
    # kworum wypada JEDEN wezel (odciety port 4567, monitor nadal go widzi),
    # ProxySQL poprawnie przenosi go do offline_hostgroup i promuje innego
    # writera — zmierzone: .187 wyladowal w hg 680, .186 przejal role.
    # Nieruszony zostaje wylacznie OSTATNI wezel: przy calkowitej utracie kworum
    # .186/.187 trafily do 680, a .185 zostal ONLINE w 650 mimo primary_partition=NO.
    # To zachowanie typu "last man standing" — ProxySQL nie oprozni hostgrupy
    # writera do zera. Warunek ponizej i tak jest sluszny: routowanie do wezla
    # poza Primary Component to stan, o ktorym operator ma wiedziec.
    active_hgs = {str(writer_hg), str(backup_hg), str(reader_hg)}
    galera_res = run_ansible(
        ctx,
        "proxysql",
        admin_query(
            "SELECT g.hostname, g.primary_partition, g.wsrep_local_state "
            "FROM mysql_server_galera_log g JOIN (SELECT hostname, "
            "MAX(time_start_us) AS t FROM mysql_server_galera_log GROUP BY hostname) m "
            "ON g.hostname = m.hostname AND g.time_start_us = m.t"
        ),
    )
    require_hosts(
        galera_res,
        proxysql_hosts,
        "ISC-19 monitor Galery",
        failures,
        undetermined,
    )
    # Badany jest tylko host ProxySQL, dla ktorego obie sekcje maja odpowiedz.
    for node, rows in servers_rows.items():
        if node not in galera_res.bodies:
            continue
        routed = {r[1] for r in rows if r[0] in active_hgs and r[2] == "ONLINE"}
        monitor = {}
        for line in galera_res.body(node).splitlines():
            if "\t" in line:
                cols = line.split("\t")
                monitor[cols[0]] = (cols[1], cols[2])
        for host in sorted(routed):
            sample = monitor.get(host)
            if sample is None:
                check(
                    False,
                    f"{node}: {host} jest ONLINE w aktywnej hostgrupie, ale monitor "
                    f"Galery nie ma o nim probki (mysql_server_galera_log)",
                    failures,
                )
                continue
            primary, local_state = sample
            check(
                primary == "YES",
                f"{node}: {host} routowany mimo primary_partition={primary} "
                f"(wezel poza Primary Component — klient dostanie blad protokolu, "
                f"nie blad bazy)",
                failures,
            )
            check(
                local_state == "4",
                f"{node}: {host} routowany mimo wsrep_local_state={local_state} "
                f"(oczekiwane 4 = Synced)",
                failures,
            )

    # ISC-21: runtime galera_hostgroups == disk galera_hostgroups (no drift)
    hg_query = (
        "SELECT CONCAT_WS(',', writer_hostgroup, backup_writer_hostgroup, "
        "reader_hostgroup, offline_hostgroup, max_writers, writer_is_also_reader) "
        "FROM {table} WHERE writer_hostgroup={hg}"
    )
    runtime_res = run_ansible(
        ctx,
        "proxysql",
        admin_query(
            hg_query.format(table="runtime_mysql_galera_hostgroups", hg=writer_hg)
        ),
    )
    disk_res = run_ansible(
        ctx,
        "proxysql",
        admin_query(hg_query.format(table="mysql_galera_hostgroups", hg=writer_hg)),
    )
    require_hosts(
        runtime_res,
        proxysql_hosts,
        "ISC-21 runtime galera_hostgroups",
        failures,
        undetermined,
    )
    require_hosts(
        disk_res,
        proxysql_hosts,
        "ISC-21 disk galera_hostgroups",
        failures,
        undetermined,
    )
    for node in proxysql_hosts:
        if node not in runtime_res.bodies or node not in disk_res.bodies:
            continue
        runtime_value = runtime_res.body(node).strip()
        disk_value = disk_res.body(node).strip()
        check(
            runtime_value == disk_value and runtime_value != "",
            f"{node}: galera_hostgroups drift runtime='{runtime_value}' disk='{disk_value}'",
            failures,
        )

    # ISC-23: read/write split off — no query rules in runtime
    rules_res = run_ansible(
        ctx,
        "proxysql",
        admin_query("SELECT COUNT(*) FROM runtime_mysql_query_rules"),
    )
    require_hosts(
        rules_res,
        proxysql_hosts,
        "ISC-23 query rules",
        failures,
        undetermined,
    )
    for node in proxysql_hosts:
        if node not in rules_res.bodies:
            continue
        count = rules_res.body(node).strip()
        check(
            count == "0",
            f"{node}: {count} query rules present (expected 0 — R/W split must stay off)",
            failures,
        )

    # ISC-22: default admin:admin credentials must be rejected
    # Odmowa dostepu to oczekiwany wynik, ale ansible widzi rc!=0 jako FAILED.
    # Przekierowanie stdout+stderr i wymuszenie rc=0 zachowuje tresc odmowy;
    # prawdziwa awaria hosta nadal trafia do errors i daje UNDETERMINED.
    default_res = run_ansible(
        ctx,
        "proxysql",
        admin_query("SELECT 1", factory_default=True) + " 2>&1 || true",
    )
    require_hosts(
        default_res,
        proxysql_hosts,
        "ISC-22 default admin",
        failures,
        undetermined,
    )
    for node in proxysql_hosts:
        if node not in default_res.bodies:
            continue
        body = default_res.body(node)
        check(
            "Access denied" in body or "ERROR" in body,
            f"{node}: default admin:admin credentials accepted (must be rejected)",
            failures,
        )

    nodes = sorted(servers_rows)
    summary = (
        f"ProxySQL healthy — {len(nodes)} node(s) ({', '.join(nodes)}), "
        f"one active writer, {expected_backends} healthy backends, runtime==disk, "
        f"no query rules, default admin rejected"
    )
    return finish(failures, undetermined, summary)


if __name__ == "__main__":
    sys.exit(main())
