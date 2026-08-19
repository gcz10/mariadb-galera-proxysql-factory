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

import math
import os
import re
import sys

from _probe_common import ProbeContext, finish, require_hosts, run_ansible

CTX = ProbeContext()
GALERA = CTX.group_hosts("galera")
PROXYSQL = CTX.group_hosts("proxysql")
IST_WINDOW_MIN = int(os.environ.get("ISC68_IST_WINDOW_MIN", "30"))
WORKLOAD_SECONDS = int(os.environ.get("ISC68_WORKLOAD_SECONDS", "20"))


def find_writer(failures, undetermined):
    if not PROXYSQL:
        undetermined.append("writer-lookup: inwentarz nie definiuje hostow proxysql")
        return None
    if not GALERA:
        undetermined.append("writer-lookup: inwentarz nie definiuje hostow galera")
        return None

    writer_hg = int(CTX.config.get("proxysql", {}).get("hostgroup_base", 10))
    query = (
        "mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf "
        "-h127.0.0.1 -P6032 -uadmin -N -B -e "
        f"\"SELECT hostname FROM runtime_mysql_servers "
        f"WHERE hostgroup_id={writer_hg} AND status='ONLINE'\""
    )
    result = run_ansible(CTX, PROXYSQL[0], query)
    require_hosts(result, [PROXYSQL[0]], "writer-lookup", failures, undetermined)
    if PROXYSQL[0] not in result.bodies:
        return None

    addresses = {
        CTX.host_address(host, "galera"): host
        for host in GALERA
        if CTX.host_address(host, "galera")
    }
    for line in result.body(PROXYSQL[0]).splitlines():
        writer = addresses.get(line.strip())
        if writer:
            return writer
    undetermined.append(
        "writer-lookup: ProxySQL nie zwrocil adresu znanego wezla Galery"
    )
    return None


def measure_write_rate(writer, failures, undetermined):
    """Run a write workload on the writer; return bytes/sec from wsrep delta."""
    if writer is None:
        return None
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
    result = run_ansible(
        CTX,
        writer,
        script,
        timeout=WORKLOAD_SECONDS + 90,
    )
    require_hosts(result, [writer], "write-rate", failures, undetermined)
    if writer not in result.bodies:
        return None
    match = re.search(r"RATE_BPS=(\d+)", result.body(writer))
    return int(match.group(1)) if match else 0


def deployed_gcache(failures, undetermined):
    if not GALERA:
        undetermined.append("gcache-deployed: inwentarz nie definiuje hostow galera")
        return None
    result = run_ansible(
        CTX,
        GALERA[0],
        "grep -ioE 'gcache.size=[0-9]+[MG]' /etc/my.cnf.d/server.cnf || true",
    )
    require_hosts(result, [GALERA[0]], "gcache-deployed", failures, undetermined)
    if GALERA[0] not in result.bodies:
        return None
    match = re.search(r"gcache.size=(\d+)([MG])", result.body(GALERA[0]), re.I)
    if not match:
        return 0
    val = int(match.group(1))
    return val * 1024 if match.group(2).upper() == "G" else val


def main():
    failures = []
    undetermined = []
    writer = find_writer(failures, undetermined)
    rate = measure_write_rate(writer, failures, undetermined)
    if rate is None:
        required_mb = None
    elif rate <= 0:
        failures.append("ISC-68 — write rate not measurable (0): no workload data")
        required_mb = 128
    else:
        gcache_bytes = rate * IST_WINDOW_MIN * 60
        required_mb = max(math.ceil(gcache_bytes / (1024 * 1024)), 128)
    deployed_mb = deployed_gcache(failures, undetermined)
    print(
        f"writer={writer or 'unknown'} write_rate={rate if rate is not None else 'unknown'} "
        f"B/s  ist_window={IST_WINDOW_MIN}min"
    )
    print(
        f"required gcache={required_mb if required_mb is not None else 'unknown'}M "
        f"(min 128M)  deployed gcache="
        f"{deployed_mb if deployed_mb is not None else 'unknown'}M"
    )

    if deployed_mb is not None:
        if deployed_mb < 128:
            failures.append(f"ISC-68 — deployed gcache {deployed_mb}M below 128M floor")
        if required_mb is not None and deployed_mb < required_mb:
            failures.append(
                f"ISC-68 — deployed gcache {deployed_mb}M < required {required_mb}M "
                f"(write_rate={rate}B/s × {IST_WINDOW_MIN}min): node would need full SST "
                f"after the IST window"
            )

    return finish(
        failures,
        undetermined,
        f"ISC-68 — gcache.size={deployed_mb}M covers write_rate={rate}B/s "
        f"for {IST_WINDOW_MIN}min IST window (required {required_mb}M)",
    )


if __name__ == "__main__":
    sys.exit(main())
