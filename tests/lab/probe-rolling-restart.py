#!/usr/bin/env python3
"""Verify the Galera rolling-restart capability (ISC-50, ISC-51).

ISC-50: the rolling-restart playbook cycles mariadbd with `serial: 1`, so nodes
        are restarted one at a time (never a simultaneous mass restart).
ISC-51: a health gate (wsrep_local_state=4 Synced + Primary + full cluster size)
        blocks the next node until the previous one has fully rejoined.

This probe is two-layered:
  1. STATIC  — parse f12_rolling_restart.yml: every play that targets multiple
               Galera hosts and cycles mariadbd MUST set serial:1, and MUST poll
               wsrep_local_state/cluster_status/cluster_size as a health gate.
  2. RUNTIME — every Galera node reports Synced (state 4), Primary, wsrep_ready=ON
               and the expected cluster size (no node stranded after a restart).

Requires APP/PROXYSQL secrets only if you also run the playbook; the probe itself
is read-only (queries wsrep status via the local MariaDB socket).
"""

import glob
import os
import re
import subprocess
import sys

import yaml

INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")
PLAYBOOK = "playbooks/f12_rolling_restart.yml"

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER = yaml.safe_load(fh)
with open(INVENTORY, encoding="utf-8") as fh:
    INV = yaml.safe_load(fh)

EXPECTED_SIZE = str(CLUSTER["galera"]["nodes_expected"])
GALERA = list(INV["all"]["children"]["galera"]["hosts"])

LIFECYCLE = re.compile(
    r"--wsrep-new-cluster"
    r"|mariadbd\s+--user"
    r"|pkill[^\n]*\bmariadbd\b"
    r"|kill\s+-\d+[^\n]*\bmariadbd\b"
    r"|mariadb-admin\s+shutdown",
    re.IGNORECASE,
)


def targets_multiple_galera(hosts):
    s = str(hosts)
    if "galera" not in s and s.strip() != "all":
        return False
    if re.search(r"galera\[\d+\]\s*$", s):
        return False
    return True


def sh(node, script, timeout=60):
    cmd = [ANSIBLE, node, "-i", INVENTORY, "-m", "ansible.builtin.shell", "-a", script]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def body(node, result):
    out = result.stdout
    m = re.search(rf'^{re.escape(node)}\s*\|\s*\w+\s*\|\s*rc=\d+\s*>>?\s*$', out, re.M)
    return out[m.end():].strip() if m else out.strip()


def wsrep_status(node):
    q = ("SHOW STATUS WHERE Variable_name IN "
         "('wsrep_local_state','wsrep_cluster_status','wsrep_ready','wsrep_cluster_size')")
    r = sh(node, f'mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "{q}"')
    text = body(node, r)
    status = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            status[parts[0]] = parts[1]
    return status


def main():
    failures = []

    # === STATIC: serial:1 + health gate in the rolling-restart playbook ===
    plays = yaml.safe_load(open(PLAYBOOK, encoding="utf-8"))
    if not isinstance(plays, list):
        failures.append(f"{PLAYBOOK}: not a list of plays")
        plays = []

    serial_checked = False
    for play in plays:
        if not isinstance(play, dict) or "hosts" not in play:
            continue
        text = yaml.safe_dump(play)
        if targets_multiple_galera(play["hosts"]) and LIFECYCLE.search(text):
            serial = play.get("serial")
            if str(serial) != "1":
                failures.append(
                    f"ISC-50 — play '{play.get('name', '?')}' cycles mariadbd "
                    f"but serial={serial!r} (must be 1)"
                )
            # health gate must poll the three wsrep markers
            gate_ok = (
                "wsrep_local_state" in text
                and "wsrep_cluster_status" in text
                and "wsrep_cluster_size" in text
                and "until" in text
            )
            if not gate_ok:
                failures.append(
                    f"ISC-51 — play '{play.get('name', '?')}' lacks a health gate "
                    f"(until + wsrep_local_state/cluster_status/cluster_size)"
                )
            serial_checked = True

    if not serial_checked:
        failures.append("ISC-50 — no serial:1 Galera lifecycle play found in "
                        f"{PLAYBOOK}")

    # === RUNTIME: every node Synced + Primary + full size + ready ===
    for node in GALERA:
        status = wsrep_status(node)
        if not status:
            failures.append(f"ISC-51 — {node}: no wsrep status (MariaDB down?)")
            continue
        checks = {
            "wsrep_local_state": ("4", "Synced"),
            "wsrep_cluster_status": ("Primary", "Primary"),
            "wsrep_cluster_size": (EXPECTED_SIZE, "full size"),
            "wsrep_ready": ("ON", "ready"),
        }
        for var, (expected, label) in checks.items():
            actual = status.get(var)
            if actual != expected:
                failures.append(
                    f"ISC-51 — {node}.{var}={actual!r} (expected {expected!r} {label})"
                )

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1

    print(f"PASS: ISC-50/51 — rolling restart serial:1 + health gate verified; "
          f"{len(GALERA)}/{len(GALERA)} Galera nodes Synced/Primary/{EXPECTED_SIZE}/ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
