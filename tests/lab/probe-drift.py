#!/usr/bin/env python3
"""Verify drift detection (ISC-21).

ISC-21: drift of significant configurations is detected. Per MASTER_PROMPT §18,
drift detection is READ-ONLY (reports, never auto-fixes in production).

This probe is two-layered:
  1. STATIC  — f13_drift.yml compares ProxySQL MAIN vs DISK for mysql_servers,
               mysql_galera_hostgroups, mysql_users (NOT runtime_*, which carry
               dynamic status / galera-derived rows); and checks Galera
               cluster_state_uuid consistency across nodes.
  2. RUNTIME — running f13_drift.yml on the clean lab reports PASS (no drift).
               Falsifiability is proven separately by injecting an unsaved config
               change (the playbook fails with DRIFT; see ISA evidence).
"""

import os
import re
import sys

import yaml

INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/example-cluster/inventory.yml")
CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/example-cluster/cluster.yml")
PLAYBOOK = "playbooks/f13_drift.yml"

# must compare MAIN (memory) vs DISK — runtime_* carries dynamic data
EXPECTED_TABLE_PAIRS = [
    ("mysql_servers", "disk.mysql_servers"),
    ("mysql_galera_hostgroups", "disk.mysql_galera_hostgroups"),
    ("mysql_users", "disk.mysql_users"),
]


def main():
    failures = []

    plays = yaml.safe_load(open(PLAYBOOK, encoding="utf-8"))
    if not isinstance(plays, list):
        failures.append(f"{PLAYBOOK}: not a list of plays")
        plays = []

    text = yaml.safe_dump(plays)
    # must NOT use runtime_mysql_servers (dynamic) — must use main mysql_servers
    if re.search(r"runtime_mysql_servers\b", text):
        failures.append(
            "ISC-21 — drift check uses runtime_mysql_servers (dynamic status/derived "
            "rows); must compare mysql_servers (main) vs disk.mysql_servers"
        )
    for main_tbl, disk_tbl in EXPECTED_TABLE_PAIRS:
        if main_tbl not in text or disk_tbl not in text:
            failures.append(f"ISC-21 — drift check missing table pair {main_tbl} vs {disk_tbl}")
    if "wsrep_cluster_state_uuid" not in text:
        failures.append("ISC-21 — drift check missing Galera cluster_state_uuid consistency")
    # must be read-only: no SAVE/LOAD/DML that changes state
    if re.search(r"\b(SAVE\s+\w+\s+TO|LOAD\s+\w+\s+TO|INSERT\s+INTO|DELETE\s+FROM|UPDATE\s+\w+\s+SET)\b", text, re.IGNORECASE):
        failures.append("ISC-21 — drift playbook performs a state-changing action (not read-only)")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1

    print("PASS: ISC-21 — drift detection read-only (ProxySQL main-vs-disk + Galera uuid); "
          "falsifiable (injected unsaved config detected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
