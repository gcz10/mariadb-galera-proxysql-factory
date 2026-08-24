#!/usr/bin/env python3
"""ISC-39: backup must not degrade the active writer beyond threshold.

Runs the numbered workload through the VIP (real write load on the writer) and
triggers a full backup concurrently, then proves:
  - the cluster-wide Galera flow-control pause during the backup stays below
    threshold (the writer is not throttled),
  - the workload keeps committing during the backup (no long write stall).

Lab-only (writes to isa_test); refuses on production.
Requires APP_DB_PASSWORD (+ backup secrets inherited by the backup playbook).
"""

import os
import re
import subprocess
import sys
import tempfile
import time

import yaml

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/example-cluster/cluster.yml")
INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/example-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")
APP_PW = os.environ.get("APP_DB_PASSWORD", "")

FLOW_THRESHOLD_NS = 2_000_000_000     # 2s cumulative flow-control pause
COMMIT_GAP_THRESHOLD = 8.0            # max seconds writer may stall
WORKLOAD_LOCAL = "tests/lab/workload-numbered.sh"
# Klient obciazenia: preferuj dedykowany host aplikacyjny (fcapp), zeby workload
# nie konkurowal o CPU/IO z mariabackup na wezle scheduler. Fallback na galera[0]
# jesli inventory nie ma grupy `app`.
_inv = yaml.safe_load(open(INVENTORY))
_app = (_inv["all"]["children"].get("app") or {}).get("hosts") or {}
WORKLOAD_HOST = next(iter(_app)) if _app else list(_inv["all"]["children"]["galera"]["hosts"])[0]
CNF_REMOTE = "/root/.workload.cnf"
SCRIPT_REMOTE = "/tmp/workload-numbered.sh"
LOG_REMOTE = "/tmp/workload.log"

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER = yaml.safe_load(fh)
with open(INVENTORY, encoding="utf-8") as fh:
    INV = yaml.safe_load(fh)

ENVIRONMENT = CLUSTER["cluster"]["environment"]
VIP = CLUSTER["proxysql"]["endpoint"]["address"]
VIP_PORT = CLUSTER["proxysql"]["endpoint"]["port"]
APP_USER = CLUSTER.get("proxysql", {}).get("app_user", "app_user")
GALERA = list(INV["all"]["children"]["galera"]["hosts"])

def sh(node, script, timeout=60, check=False):
    r = subprocess.run(
        [ANSIBLE, node, "-i", INVENTORY, "-m", "ansible.builtin.shell", "-a", script],
        capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"ansible {node} failed: {r.stdout}\n{r.stderr}")
    return r


def body(node, out):
    m = re.search(rf'^{re.escape(node)}\s*\|\s*\w+\s*\|\s*rc=\d+\s*>>?\s*$', out, re.M)
    return out[m.end():].strip() if m else out.strip()


def flow_control_max():
    """Max wsrep_flow_control_paused_ns across all Galera nodes (cluster-wide)."""
    vals = []
    for n in GALERA:
        r = sh(n, "mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "
                  "\"SHOW STATUS LIKE 'wsrep_flow_control_paused_ns'\" | awk '{print $2}'")
        try:
            vals.append(int(body(n, r.stdout).strip() or 0))
        except ValueError:
            vals.append(0)
    return max(vals) if vals else 0


def committed_times():
    r = sh(WORKLOAD_HOST, f"cat {LOG_REMOTE} 2>/dev/null || true")
    times = []
    for line in body(WORKLOAD_HOST, r.stdout).splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                times.append(float(parts[0]))
            except ValueError:
                pass
    return times


def main():
    failures = []
    if ENVIRONMENT == "production":
        print("REFUSED: backup-impact writes test load and must not run on production")
        return 1
    if not APP_PW:
        print("FAIL: APP_DB_PASSWORD must be set")
        return 1

    local_cnf = None
    try:
        # Przygotowanie tabeli na wezle Galera (z TRUNCATE, zeby seq startowal od 1).
        sh(GALERA[0],
           'mariadb --socket=/var/lib/mysql/mysql.sock -e '
           '"CREATE DATABASE IF NOT EXISTS isa_test; '
           'CREATE TABLE IF NOT EXISTS isa_test.isa_failover '
           '(seq BIGINT PRIMARY KEY, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP); '
           'TRUNCATE isa_test.isa_failover;"', check=True)

        fd, local_cnf = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write(f"[client]\nuser={APP_USER}\npassword={APP_PW}\n")
        subprocess.run([ANSIBLE, WORKLOAD_HOST, "-i", INVENTORY, "-m", "copy",
                        "-a", f"src={local_cnf} dest={CNF_REMOTE} mode=0600 owner=root"],
                       capture_output=True, text=True, check=True)
        subprocess.run([ANSIBLE, WORKLOAD_HOST, "-i", INVENTORY, "-m", "copy",
                        "-a", f"src={WORKLOAD_LOCAL} dest={SCRIPT_REMOTE} mode=0755"],
                       capture_output=True, text=True, check=True)

        sh(WORKLOAD_HOST,
           f"touch /tmp/workload.run; nohup bash {SCRIPT_REMOTE} {VIP} {VIP_PORT} "
           f"{CNF_REMOTE} {LOG_REMOTE} >/tmp/workload.out 2>&1 & echo launched", check=True)
        time.sleep(5)  # establish write load

        fc_before = flow_control_max()
        backup_start = time.time()
        print(f"running backup under load (flow_control baseline={fc_before} ns)…")
        bkp = subprocess.run(
            [ANSIBLE.replace("ansible", "ansible-playbook") if ANSIBLE == "ansible" else "ansible-playbook",
             "playbooks/f10_backup.yml", "-i", INVENTORY,
             "-e", f"@{CONFIG_PATH}"],
            capture_output=True, text=True, timeout=300)
        backup_end = time.time()
        fc_after = flow_control_max()

        if bkp.returncode != 0:
            failures.append(f"backup failed during load test: {bkp.stdout[-400:]}")

        time.sleep(2)
        sh(WORKLOAD_HOST, "rm -f /tmp/workload.run", check=True)
        time.sleep(1)

        # Flow control induced during the backup window.
        fc_delta = fc_after - fc_before
        # Largest write stall that overlapped the backup window.
        times = [t for t in committed_times() if backup_start - 1 <= t <= backup_end + 1]
        gap = max((b - a for a, b in zip(times, times[1:])), default=0.0)

        if fc_delta >= FLOW_THRESHOLD_NS:
            failures.append(f"ISC-39 — flow control {fc_delta} ns during backup "
                            f"(>= {FLOW_THRESHOLD_NS} ns threshold)")
        if gap >= COMMIT_GAP_THRESHOLD:
            failures.append(f"ISC-39 — writer stalled {gap:.1f}s during backup "
                            f"(>= {COMMIT_GAP_THRESHOLD}s threshold)")
        if not times:
            failures.append("ISC-39 — no writes committed during backup window (workload not running?)")

    finally:
        sh(WORKLOAD_HOST, f"rm -f /tmp/workload.run {CNF_REMOTE}", timeout=30)
        if local_cnf and os.path.exists(local_cnf):
            os.unlink(local_cnf)

    if failures:
        print("FAIL: backup-impact test failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"PASS: backup did not degrade writer — flow control {fc_delta} ns "
        f"(< {FLOW_THRESHOLD_NS} ns), max write stall {gap:.2f}s "
        f"(< {COMMIT_GAP_THRESHOLD}s) across {len(times)} commits during backup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
