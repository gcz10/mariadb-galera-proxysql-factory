#!/usr/bin/env python3
"""Verify Galera cluster health across all nodes.

Checks: ISC-7 (one Primary), ISC-8 (cluster size),
ISC-9 (same state UUID), ISC-10 (all Synced/Ready),
ISC-14 (mariabackup SST), ISC-16 (no tables without PK).
"""

import os
import re
import subprocess
import sys
import yaml

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER_CONFIG = yaml.safe_load(fh)

EXPECTED_SIZE = int(CLUSTER_CONFIG["galera"]["nodes_expected"])


def run_ansible_query(nodes, query):
    """Run a MariaDB query on Galera nodes via ansible, return {node: {col: val}}."""
    cmd = [
        ANSIBLE, nodes, "-i", INVENTORY, "-m", "ansible.builtin.shell",
        "-a", f'mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "{query}"',
        "--fork", "5",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    data = {}
    current_host = None
    current_body = []
    for line in result.stdout.splitlines():
        header = re.match(r'^(\S+)\s*\|\s*\w+\s*\|\s*rc=\d+\s*>>?\s*$', line)
        if header:
            if current_host:
                data[current_host] = "\n".join(current_body).strip()
            current_host = header.group(1)
            current_body = []
        elif current_host:
            current_body.append(line)
    if current_host:
        data[current_host] = "\n".join(current_body).strip()
    return data


def check(condition, message, failures):
    if not condition:
        failures.append(message)


def main():
    failures = []

    # Query wsrep status from all galera nodes
    status_query = (
        "SHOW STATUS WHERE Variable_name IN "
        "('wsrep_cluster_size','wsrep_cluster_status','wsrep_cluster_state_uuid',"
        "'wsrep_local_state','wsrep_local_state_comment','wsrep_ready','wsrep_connected')"
    )
    status_raw = run_ansible_query("galera", status_query)

    if not status_raw:
        print("FAIL: no Galera nodes responded to status query")
        return 1

    # Parse TSV status per node
    node_status = {}
    for node, body in status_raw.items():
        status = {}
        for line in body.splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                status[parts[0]] = parts[1]
        node_status[node] = status

    # ISC-8: wsrep_cluster_size == nodes_expected on all nodes
    for node, status in node_status.items():
        size = int(status.get("wsrep_cluster_size", 0))
        check(
            size == EXPECTED_SIZE,
            f"{node}: wsrep_cluster_size={size}, expected {EXPECTED_SIZE}",
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
    sst_raw = run_ansible_query("galera", "SHOW VARIABLES LIKE 'wsrep_sst_method'")
    for node, body in sst_raw.items():
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
    pk_raw = run_ansible_query("galera[0]", pk_query)
    for node, body in pk_raw.items():
        tables = [line.strip() for line in body.splitlines() if line.strip()]
        check(
            not tables,
            f"{node}: tables without primary key: {tables}",
            failures,
        )

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    nodes_str = ", ".join(sorted(node_status.keys()))
    print(
        f"PASS: Galera cluster healthy — {len(node_status)} nodes ({nodes_str}), "
        f"all Primary/Synced/Ready, state UUID {uuids.pop() if uuids else 'unknown'}, "
        f"SST=mariabackup, no tables without PK"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
