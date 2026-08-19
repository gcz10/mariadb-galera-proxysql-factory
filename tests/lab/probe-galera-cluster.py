#!/usr/bin/env python3
"""Verify Galera cluster health across all nodes.

Checks: ISC-7 (one Primary), ISC-8 (cluster size),
ISC-9 (same state UUID), ISC-10 (all Synced/Ready),
ISC-14 (mariabackup SST), ISC-16 (no tables without PK).

Sonda jest fail-closed (tests/lab/_probe_common.py): wezel Galery, ktory nie
odpowiedzial na sekcje pomiaru, daje UNDETERMINED — nigdy nie znika z wyniku.
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


def mariadb_query(query: str) -> str:
    """Polecenie pomiarowe: query przez socket lokalny MariaDB."""
    return f'mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "{query}"'


def main() -> int:
    failures: list[str] = []
    undetermined: list[str] = []
    ctx = ProbeContext()
    galera_hosts = ctx.group_hosts("galera")
    expected_size = int(ctx.config["galera"]["nodes_expected"])

    # Query wsrep status from all galera nodes
    status_query = (
        "SHOW STATUS WHERE Variable_name IN "
        "('wsrep_cluster_size','wsrep_cluster_status','wsrep_cluster_state_uuid',"
        "'wsrep_local_state','wsrep_local_state_comment','wsrep_ready','wsrep_connected')"
    )
    status_res = run_ansible(ctx, "galera", mariadb_query(status_query))
    # Guard calej grupy: bez kompletu odpowiedzi nie ma mowy o PASS.
    require_hosts(status_res, galera_hosts, "ISC-7..10 wsrep status", failures, undetermined)

    # Parse TSV status per node — tylko wezly, ktore naprawde odpowiedzialy
    # (braki sa juz wpisane do undetermined przez require_hosts).
    node_status = {}
    for node in galera_hosts:
        if node not in status_res.bodies:
            continue
        status = {}
        for line in status_res.body(node).splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                status[parts[0]] = parts[1]
        node_status[node] = status

    # ISC-8: wsrep_cluster_size == nodes_expected on all nodes
    for node, status in node_status.items():
        size = int(status.get("wsrep_cluster_size", 0))
        check(
            size == expected_size,
            f"{node}: wsrep_cluster_size={size}, expected {expected_size}",
            failures,
        )

    # ISC-7: exactly one Primary Component (all report Primary)
    for node, status in node_status.items():
        check(
            status.get("wsrep_cluster_status") == "Primary",
            f"{node}: wsrep_cluster_status={status.get('wsrep_cluster_status')} (expected Primary)",
            failures,
        )

    # ISC-9: identical state UUID on all nodes
    uuids = {status.get("wsrep_cluster_state_uuid") for status in node_status.values()}
    if node_status:
        check(
            len(uuids) == 1 and None not in uuids,
            f"cluster state UUID differs across nodes: {uuids}",
            failures,
        )

    # ISC-10: all nodes Synced, Ready, Connected
    for node, status in node_status.items():
        check(
            status.get("wsrep_local_state") == "4",
            f"{node}: wsrep_local_state={status.get('wsrep_local_state')} (expected 4/Synced)",
            failures,
        )
        check(
            status.get("wsrep_ready") == "ON",
            f"{node}: wsrep_ready={status.get('wsrep_ready')} (expected ON)",
            failures,
        )
        check(
            status.get("wsrep_connected") == "ON",
            f"{node}: wsrep_connected={status.get('wsrep_connected')} (expected ON)",
            failures,
        )

    # ISC-14: wsrep_sst_method = mariabackup
    sst_res = run_ansible(ctx, "galera", mariadb_query("SHOW VARIABLES LIKE 'wsrep_sst_method'"))
    require_hosts(sst_res, galera_hosts, "ISC-14 sst_method", failures, undetermined)
    for node in galera_hosts:
        if node not in sst_res.bodies:
            continue
        body = sst_res.body(node)
        check(
            "mariabackup" in body,
            f"{node}: wsrep_sst_method is not mariabackup: {body}",
            failures,
        )

    # ISC-16: no tables without primary key in user databases
    pk_query = (
        "SELECT t.TABLE_SCHEMA, t.TABLE_NAME "
        "FROM information_schema.TABLES t "
        "LEFT JOIN information_schema.TABLE_CONSTRAINTS tc "
        "ON t.TABLE_SCHEMA=tc.TABLE_SCHEMA AND t.TABLE_NAME=tc.TABLE_NAME "
        "AND tc.CONSTRAINT_TYPE='PRIMARY KEY' "
        "WHERE t.TABLE_TYPE='BASE TABLE' AND tc.CONSTRAINT_NAME IS NULL "
        "AND t.TABLE_SCHEMA NOT IN ('information_schema','performance_schema','mysql','sys')"
    )
    # Dowolny wezel Galera — schemat jest replikowany, wiec pytamy grupe, a nie
    # zaszyta nazwe "gnode1" (klaster moze miec wezly nazwane inaczej).
    pk_res = run_ansible(ctx, "galera[0]", mariadb_query(pk_query))
    require_hosts(pk_res, galera_hosts[:1], "ISC-16 tables without PK", failures, undetermined)
    for node in galera_hosts[:1]:
        if node not in pk_res.bodies:
            continue
        tables = [line.strip() for line in pk_res.body(node).splitlines() if line.strip()]
        check(
            not tables,
            f"{node}: tables without primary key: {tables}",
            failures,
        )

    nodes_str = ", ".join(sorted(node_status))
    summary = (
        f"Galera cluster healthy — {len(node_status)} nodes ({nodes_str}), "
        f"all Primary/Synced/Ready, state UUID {uuids.pop() if uuids else 'unknown'}, "
        f"SST=mariabackup, no tables without PK"
    )
    return finish(failures, undetermined, summary)


if __name__ == "__main__":
    sys.exit(main())
