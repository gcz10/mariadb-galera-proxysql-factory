#!/usr/bin/env python3
"""Verify ProxySQL Galera routing on all ProxySQL nodes.

Checks: ISC-18 (exactly one active writer), ISC-19 (only healthy nodes in
active hostgroups / offline HG empty when cluster healthy), ISC-20 (Galera
monitor converged), ISC-21 (runtime == disk, no drift), ISC-22 (default
admin credentials rejected), ISC-23 (read/write split off — no query rules).

Requires `/etc/proxysql/admin-check.cnf` deployed by F7 on each ProxySQL node.
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

EXPECTED_BACKENDS = int(CLUSTER_CONFIG["galera"]["nodes_expected"])
HG_BASE = int(CLUSTER_CONFIG.get("proxysql", {}).get("hostgroup_base", 10))
WRITER_HG = HG_BASE + 0
BACKUP_HG = HG_BASE + 10
READER_HG = HG_BASE + 20
OFFLINE_HG = HG_BASE + 30
# ProxySQL factory-default admin credential — asserted to be REJECTED (ISC-22).


def run_admin_query(query, factory_default=False):
    """Run a ProxySQL admin query on all proxysql nodes, return {node: body}."""
    env_prefix = "MYSQL_PWD=admin " if factory_default else ""
    auth = "" if factory_default else "--defaults-extra-file=/etc/proxysql/admin-check.cnf "
    cmd = [
        ANSIBLE, "proxysql", "-i", INVENTORY, "-m", "ansible.builtin.shell",
        "-a", f'{env_prefix}mariadb {auth}-h127.0.0.1 -P6032 -uadmin -N -B -e "{query}"',
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


    # Runtime server distribution per ProxySQL node
    servers_raw = run_admin_query(
        "SELECT hostgroup_id, hostname, status FROM runtime_mysql_servers "
        "ORDER BY hostgroup_id, hostname"
    )
    if not servers_raw:
        print("FAIL: no ProxySQL nodes responded to admin query")
        return 1

    for node, body in servers_raw.items():
        rows = [ln.split("\t") for ln in body.splitlines() if "\t" in ln]
        online_writers = [r for r in rows if r[0] == str(WRITER_HG) and r[2] == "ONLINE"]
        online_offline = [r for r in rows if r[0] == str(OFFLINE_HG) and r[2] == "ONLINE"]
        healthy_backends = {
            r[1] for r in rows
            if r[0] in (str(WRITER_HG), str(BACKUP_HG)) and r[2] == "ONLINE"
        }

        # ISC-18: exactly one active (ONLINE) writer
        check(
            len(online_writers) == 1,
            f"{node}: {len(online_writers)} ONLINE writers in HG {WRITER_HG} (expected 1)",
            failures,
        )
        # ISC-19 steady state: no healthy node stuck in offline HG when cluster is healthy
        check(
            len(online_offline) == 0,
            f"{node}: {len(online_offline)} node(s) ONLINE in offline HG {OFFLINE_HG} "
            f"(expected 0 when cluster healthy)",
            failures,
        )
        # ISC-20: Galera monitor converged — all expected backends are healthy
        check(
            len(healthy_backends) == EXPECTED_BACKENDS,
            f"{node}: {len(healthy_backends)} healthy backends in active HGs "
            f"(expected {EXPECTED_BACKENDS})",
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
    # ktory ten sam wezel zwracal przy polaczeniu bezposrednim. Warunek
    # "dokladnie jeden ONLINE writer" byl spelniony przez cala awarie.
    active_hgs = {str(WRITER_HG), str(BACKUP_HG), str(READER_HG)}
    galera_raw = run_admin_query(
        "SELECT g.hostname, g.primary_partition, g.wsrep_local_state "
        "FROM mysql_server_galera_log g JOIN (SELECT hostname, "
        "MAX(time_start_us) AS t FROM mysql_server_galera_log GROUP BY hostname) m "
        "ON g.hostname = m.hostname AND g.time_start_us = m.t"
    )
    for node, body in servers_raw.items():
        rows = [ln.split("\t") for ln in body.splitlines() if "\t" in ln]
        routed = {r[1] for r in rows if r[0] in active_hgs and r[2] == "ONLINE"}
        monitor = {}
        for line in galera_raw.get(node, "").splitlines():
            if "\t" in line:
                cols = line.split("\t")
                monitor[cols[0]] = (cols[1], cols[2])
        for host in sorted(routed):
            sample = monitor.get(host)
            if sample is None:
                failures.append(
                    f"{node}: {host} jest ONLINE w aktywnej hostgrupie, ale monitor "
                    f"Galery nie ma o nim probki (mysql_server_galera_log)"
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
    runtime_hg = run_admin_query(hg_query.format(table="runtime_mysql_galera_hostgroups", hg=WRITER_HG))
    disk_hg = run_admin_query(hg_query.format(table="mysql_galera_hostgroups", hg=WRITER_HG))
    for node in runtime_hg:
        r = runtime_hg.get(node, "").strip()
        d = disk_hg.get(node, "").strip()
        check(
            r == d and r != "",
            f"{node}: galera_hostgroups drift runtime='{r}' disk='{d}'",
            failures,
        )

    # ISC-23: read/write split off — no query rules in runtime
    rules_raw = run_admin_query("SELECT COUNT(*) FROM runtime_mysql_query_rules")
    for node, body in rules_raw.items():
        count = body.strip()
        check(
            count == "0",
            f"{node}: {count} query rules present (expected 0 — R/W split must stay off)",
            failures,
        )

    # ISC-22: default admin:admin credentials must be rejected
    default_raw = run_admin_query("SELECT 1", factory_default=True)
    for node, body in default_raw.items():
        check(
            "Access denied" in body or "ERROR" in body,
            f"{node}: default admin:admin credentials accepted (must be rejected)",
            failures,
        )

    if failures:
        print("FAIL: ProxySQL routing checks failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    nodes = sorted(servers_raw.keys())
    print(
        f"PASS: ProxySQL healthy — {len(nodes)} node(s) ({', '.join(nodes)}), "
        f"one active writer, {EXPECTED_BACKENDS} healthy backends, runtime==disk, "
        f"no query rules, default admin rejected"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
