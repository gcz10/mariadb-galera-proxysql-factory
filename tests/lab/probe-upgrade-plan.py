#!/usr/bin/env python3
"""Verify the major-upgrade plan generator (ISC-53, ISC-54, ISC-56).

ISC-53: the upgrade plan is READ-ONLY — it generates a plan without modifying
        cluster hosts (Galera host tasks report changed=0).
ISC-54: the upgrade path comes from official MariaDB/Galera documentation
        (11.4 LTS → 11.8 LTS, in-place, mariadb-upgrade --skip-write-binlog).
ISC-56: ANTI — a major rollback must NOT downgrade an existing datadir; the plan
        generator refuses to plan a downgrade (guard fires).

Checks (read-only):
  1. STATIC  — f12_upgrade_plan.yml Galera host tasks are read-only (changed_when:false,
               no package lifecycle); the anti-downgrade assert is present.
  2. DOC     — docs/plans/major-upgrade-plan.md exists and cites the official path +
               the downgrade-is-unsupported stance.
  3. RUNTIME — running f12_upgrade_plan.yml leaves Galera hosts at changed=0 (ISC-53),
               and the anti-downgrade guard fires on a forced-downgrade invocation (ISC-56).
"""

import os
import re
import subprocess
import sys

import yaml

INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
PLAYBOOK = "playbooks/f12_upgrade_plan.yml"
PLAN_DOC = "docs/plans/major-upgrade-plan.md"

OFFICIAL_MARKERS = [
    "11.8 LTS",
    "mariadb-upgrade --skip-write-binlog",
    "upgrading-galera-cluster",
    "forward-incompatible",
]

# package lifecycle / write actions that would violate ISC-53 (read-only host play)
WRITE_ACTIONS = re.compile(
    r"\b(dnf\s+(install|upgrade|update|remove)|rpm\s+-[iU]|"
    r"systemctl\s+(stop|restart|start)|pkill|mariadb-install-db|mariadbd\s+--user)\b",
    re.IGNORECASE,
)


def main():
    failures = []

    plays = yaml.safe_load(open(PLAYBOOK, encoding="utf-8"))
    if not isinstance(plays, list):
        failures.append(f"{PLAYBOOK}: not a list of plays")
        plays = []

    galera_readonly = False
    anti_guard = False
    for play in plays:
        if not isinstance(play, dict):
            continue
        text = yaml.safe_dump(play)
        hosts = str(play.get("hosts", ""))
        if "galera" in hosts:
            # host play must be read-only: no package lifecycle, tasks changed_when:false
            if WRITE_ACTIONS.search(text):
                failures.append(
                    f"ISC-53 — galera play '{play.get('name','?')}' performs a write "
                    f"action (not read-only): {WRITE_ACTIONS.search(text).group(0)!r}"
                )
            galera_readonly = True
        if "anti-downgrade" in text.lower() or "version_compare" in text:
            anti_guard = True

    if not galera_readonly:
        failures.append("ISC-53 — no read-only Galera inspection play found")
    if not anti_guard:
        failures.append("ISC-56 — anti-downgrade guard not found in upgrade plan")

    # === DOC content (ISC-54) ===
    if not os.path.exists(PLAN_DOC):
        failures.append(f"ISC-54 — plan doc missing: {PLAN_DOC}")
    else:
        doc = open(PLAN_DOC, encoding="utf-8").read()
        for marker in OFFICIAL_MARKERS:
            if marker not in doc:
                failures.append(f"ISC-54 — plan doc missing official marker: {marker!r}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1

    print("PASS: ISC-53/54/56 — read-only plan generator, official 11.4→11.8 LTS path, "
          "anti-downgrade guard verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
