#!/usr/bin/env python3
"""Verify MariaDB hardening: ISC-40, ISC-41, ISC-42."""

import os
import re
import subprocess
import sys
import yaml

INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")
_inv = yaml.safe_load(open(INVENTORY))
GALERA_NODE = list(_inv["all"]["children"]["galera"]["hosts"])[0]


def run_query(node, query):
    """Run a MariaDB query on a node via ansible, return raw stdout."""
    cmd = [
        ANSIBLE, node, "-i", INVENTORY, "-m", "ansible.builtin.shell",
        "-a", f'mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "{query}"',
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

    # ISC-40: no anonymous users
    anon = run_query(GALERA_NODE, "SELECT CONCAT(user,'@',host) FROM mysql.user WHERE user=''")
    anon_users = anon.get(GALERA_NODE, "").strip()
    check(not anon_users, f"ISC-40: anonymous users exist: {anon_users}", failures)

    # ISC-40: no test database
    testdb = run_query(GALERA_NODE, "SHOW DATABASES LIKE 'test'")
    test_result = testdb.get(GALERA_NODE, "").strip()
    check(not test_result, f"ISC-40: test database exists: {test_result}", failures)

    # ISC-40: no empty passwords for non-system accounts
    empty_pw = run_query(
        GALERA_NODE,
        "SELECT CONCAT(user,'@',host) FROM mysql.user "
        "WHERE (authentication_string='' OR authentication_string IS NULL) "
        "AND user NOT IN ('mariadb.sys','mysql','PUBLIC') AND user != ''"
    )
    empty_users = empty_pw.get(GALERA_NODE, "").strip()
    check(not empty_users, f"ISC-40: empty password accounts: {empty_users}", failures)

    # ISC-41: root localhost-only
    remote_root = run_query(
        GALERA_NODE,
        "SELECT CONCAT(user,'@',host) FROM mysql.user "
        "WHERE user='root' AND host NOT IN ('localhost','127.0.0.1','::1')"
    )
    remote_root_result = remote_root.get(GALERA_NODE, "").strip()
    check(
        not remote_root_result,
        f"ISC-41: root has remote access: {remote_root_result}",
        failures,
    )

    # ISC-42: sst_user least privilege
    sst_grants = run_query(GALERA_NODE, "SHOW GRANTS FOR 'sst_user'@'localhost'")
    sst_result = sst_grants.get(GALERA_NODE, "")
    check(
        "RELOAD" in sst_result and "PROCESS" in sst_result and "LOCK TABLES" in sst_result,
        f"ISC-42: sst_user missing required grants: {sst_result}",
        failures,
    )

    # ISC-42: pmm_monitor least privilege
    pmm_grants = run_query(GALERA_NODE, "SHOW GRANTS FOR 'pmm_monitor'@'%'")
    pmm_result = pmm_grants.get(GALERA_NODE, "")
    check(
        "PROCESS" in pmm_result and "SELECT" in pmm_result,
        f"ISC-42: pmm_monitor missing required grants: {pmm_result}",
        failures,
    )

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print("PASS: MariaDB hardening verified — no anon/test/empty-pw, root localhost-only, least privilege SST+monitor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
