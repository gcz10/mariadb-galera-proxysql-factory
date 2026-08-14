#!/usr/bin/env python3
"""ISC-68: gcache.size is computed from measured write rate × IST window.

Galera's gcache stores recent write-sets so a returning node can rejoin via IST
(incremental) instead of a full SST. The required size:

    gcache.size = write_rate_bytes_per_sec × ist_window_minutes × 60

This probe MEASURES the real write rate (a short write workload → wsrep_replicated_bytes
delta), COMPUTES the required gcache for the target IST window, and verifies the
DEPLOYED gcache.size (in server.cnf) covers the requirement (and the 128M floor).

Falsifiable: if the deployed gcache is smaller than what the measured write rate
requires for the IST window, the probe FAILS (a node down for the window would
fall back to full SST instead of IST).
"""

import os
import re
import subprocess
import sys
import math

import yaml

INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")
_inv = yaml.safe_load(open(INVENTORY))
GALERA_NODE = list(_inv["all"]["children"]["galera"]["hosts"])[0]
PROXYSQL_NODE = list(_inv["all"]["children"]["proxysql"]["hosts"])[0]
IST_WINDOW_MIN = int(os.environ.get("ISC68_IST_WINDOW_MIN", "30"))
WORKLOAD_SECONDS = int(os.environ.get("ISC68_WORKLOAD_SECONDS", "20"))


def sh(node, script, timeout=120):
    cmd = [ANSIBLE, node, "-i", INVENTORY, "-m", "ansible.builtin.shell", "-a", script]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def body(node, result):
    out = result.stdout
    m = re.search(rf'^{re.escape(node)}\s*\|\s*\w+\s*\|\s*rc=\d+\s*>>?\s*$', out, re.M)
    return out[m.end():].strip() if m else out.strip()


def find_writer():
    cfg = yaml.safe_load(open(CONFIG_PATH, encoding="utf-8"))
    writer_hg = int(cfg.get("proxysql", {}).get("hostgroup_base", 10))
    r = sh(
        PROXYSQL_NODE,
        "mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf "
        f"-h127.0.0.1 -P6032 -uadmin -N -B -e "
        f"\"SELECT hostname FROM runtime_mysql_servers WHERE hostgroup_id={writer_hg} AND status='ONLINE'\"",
    )
    ip = body(PROXYSQL_NODE, r).strip()
    inv = yaml.safe_load(open(INVENTORY))
    galera = inv["all"]["children"]["galera"]["hosts"]
    for host, v in galera.items():
        if v.get("galera_node_address") == ip:
            return host
    return next(iter(galera))


def measure_write_rate(writer):
    """Run a write workload on the writer; return bytes/sec from wsrep_replicated_bytes delta."""
    script = f'''
SOCK=/var/lib/mysql/mysql.sock
mariadb --socket=$SOCK -e "CREATE DATABASE IF NOT EXISTS gcache_meas; CREATE TABLE IF NOT EXISTS gcache_meas.w (id INT PRIMARY KEY AUTO_INCREMENT, payload TEXT) ENGINE=InnoDB" 2>/dev/null
T0=$(mariadb --socket=$SOCK -N -B -e "SHOW STATUS LIKE 'wsrep_replicated_bytes'" | awk '{{print $2}}')
START=$(date +%s)
for i in $(seq 1 500); do
  mariadb --socket=$SOCK -e "INSERT INTO gcache_meas.w (payload) VALUES (RPAD('x',1024,'x'))" 2>/dev/null
  now=$(date +%s); [ $((now-START)) -ge {WORKLOAD_SECONDS} ] && break
done
END=$(date +%s)
T1=$(mariadb --socket=$SOCK -N -B -e "SHOW STATUS LIKE 'wsrep_replicated_bytes'" | awk '{{print $2}}')
ELAPSED=$((END-START)); DELTA=$((T1-T0))
echo "RATE_BPS=$(( (ELAPSED>0 ? DELTA/ELAPSED : 0) )) DELTA=$DELTA ELAPSED=${{ELAPSED}}s"
'''
    r = sh(writer, script, timeout=WORKLOAD_SECONDS + 90)
    m = re.search(r'RATE_BPS=(\d+)', body(writer, r))
    return int(m.group(1)) if m else 0


def deployed_gcache():
    r = sh(GALERA_NODE, "grep -ioE 'gcache.size=[0-9]+[MG]' /etc/my.cnf.d/server.cnf")
    m = re.search(r'gcache.size=(\d+)([MG])', body(GALERA_NODE, r), re.I)
    if not m:
        return 0
    val = int(m.group(1))
    return val * 1024 if m.group(2).upper() == "G" else val


def main():
    failures = []
    writer = find_writer()
    rate = measure_write_rate(writer)
    if rate <= 0:
        failures.append("ISC-68 — write rate not measurable (0): no workload data")
        required_mb = 128
    else:
        gcache_bytes = rate * IST_WINDOW_MIN * 60
        required_mb = max(math.ceil(gcache_bytes / (1024 * 1024)), 128)
    deployed_mb = deployed_gcache()
    print(f"writer={writer} write_rate={rate} B/s  ist_window={IST_WINDOW_MIN}min")
    print(f"required gcache={required_mb}M (min 128M)  deployed gcache={deployed_mb}M")

    if deployed_mb < 128:
        failures.append(f"ISC-68 — deployed gcache {deployed_mb}M below 128M floor")
    if deployed_mb < required_mb:
        failures.append(
            f"ISC-68 — deployed gcache {deployed_mb}M < required {required_mb}M "
            f"(write_rate={rate}B/s × {IST_WINDOW_MIN}min): node would need full SST "
            f"after the IST window"
        )

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1

    print(f"PASS: ISC-68 — gcache.size={deployed_mb}M covers write_rate={rate}B/s "
          f"for {IST_WINDOW_MIN}min IST window (required {required_mb}M)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
