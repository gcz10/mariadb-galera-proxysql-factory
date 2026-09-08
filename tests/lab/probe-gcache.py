#!/usr/bin/env python3
"""ISC-68: gcache.size is computed from measured write rate × IST window.

Galera's gcache stores recent write-sets so a returning node can rejoin via IST
(incremental) instead of a full SST. The required size:

    gcache.size = write_rate_bytes_per_sec × ist_window_minutes × 60

This probe MEASURES a write rate (a short write workload → wsrep_replicated_bytes
delta), COMPUTES the required gcache for the target IST window, and verifies the
DEPLOYED gcache.size (in server.cnf) covers the requirement (and the 128M floor).

WHAT THE NUMBER IS, EXACTLY: a LOWER BOUND, not the cluster's production write
rate. The workload runs 500 INSERTs, each through a SEPARATE `mariadb` client
invocation, so most of the wall clock goes to connection setup rather than
replication. Two consequences, both stated here because the direction matters:

  * the rate is quantised (fixed payload / whole-second ELAPSED), so identical
    values across clusters are expected and are NOT evidence of a cached read;
  * an understated rate understates the REQUIRED gcache, so this probe passing
    is not proof that a busy cluster's gcache is large enough. It proves the
    deployed size covers at least this floor. Real sizing for a loaded cluster
    needs `write_rate` taken from production `wsrep_replicated_bytes` over an
    hour (`tests/validation/calc-gcache.py --write-rate`).

Falsifiable: if the deployed gcache is smaller than what the measured write rate
requires for the IST window, the probe FAILS (a node down for the window would
fall back to full SST instead of IST).
"""
import math
import os
import re
import secrets
import sys
import time
from _probe_common import ProbeContext, finish, require_hosts, run_ansible

CTX = ProbeContext()
GALERA = CTX.group_hosts("galera")
PROXYSQL = CTX.group_hosts("proxysql")
IST_WINDOW_MIN = int(os.environ.get("ISC68_IST_WINDOW_MIN", "30"))
WORKLOAD_SECONDS = int(os.environ.get("ISC68_WORKLOAD_SECONDS", "20"))
ALLOWED_ENVIRONMENTS = {"laboratory"}


def validate_environment(config: dict) -> tuple[bool, str]:
    env = (config.get("cluster") or {}).get("environment")
    if not env:
        return False, "missing cluster.environment (fail closed: requires documented lab environment)"
    if env not in ALLOWED_ENVIRONMENTS:
        return False, f"cluster.environment={env!r} is not an allowed lab environment (fail closed)"
    return True, str(env)

def generate_measurement_db_name() -> str:
    """Generate a unique measurement DB name distinct from pre-existing gcache_meas.

    MariaDB max database identifier length is 64 characters.
    """
    token = secrets.token_hex(6)
    ts = int(time.time())
    return f"gcache_meas_{ts}_{token}"


def build_workload_script(db_name: str, workload_seconds: int) -> str:
    """Build isolated workload script with exclusive DB creation and trap-based cleanup.

    Contract:
    - set -eo pipefail and traps EXIT TERM INT so errors cannot be hidden.
    - Splits CREATE DATABASE and CREATE TABLE; sets OWNED=1 immediately after DB success
      so table or insert failures trigger cleanup.
    - Trap checks OWNED: never drops colliding or pre-existing unowned databases.
    - Cleanup failure forces nonzero exit (no || true).
    """
    return rf'''
set -eo pipefail

SOCK=/var/lib/mysql/mysql.sock
DB="{db_name}"
OWNED=0

cleanup() {{
  local rc=$?
  trap - EXIT TERM INT
  if [ "$OWNED" -eq 1 ]; then
    if ! mariadb --socket=$SOCK -e "DROP DATABASE IF EXISTS \`$DB\`;"; then
      echo "ERROR: cleanup failed to drop measurement database $DB" >&2
      exit 2
    fi
  fi
  exit $rc
}}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# Exclusively create unique measurement DB (fails if DB already exists)
mariadb --socket=$SOCK -e "CREATE DATABASE \`$DB\`;"
OWNED=1

mariadb --socket=$SOCK -e "CREATE TABLE \`$DB\`.w (id INT PRIMARY KEY AUTO_INCREMENT, payload TEXT) ENGINE=InnoDB"

T0=$(mariadb --socket=$SOCK -N -B -e "SHOW STATUS LIKE 'wsrep_replicated_bytes'" | awk '{{print $2}}')
START=$(date +%s)
for i in $(seq 1 500); do
  mariadb --socket=$SOCK -e "INSERT INTO \`$DB\`.w (payload) VALUES (RPAD('x',1024,'x'))"
  now=$(date +%s); [ $((now-START)) -ge {workload_seconds} ] && break
done
END=$(date +%s)
T1=$(mariadb --socket=$SOCK -N -B -e "SHOW STATUS LIKE 'wsrep_replicated_bytes'" | awk '{{print $2}}')
ELAPSED=$((END-START)); DELTA=$((T1-T0))
echo "RATE_BPS=$(( (ELAPSED>0 ? DELTA/ELAPSED : 0) )) DELTA=$DELTA ELAPSED=${{ELAPSED}}s"
'''


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
    valid_env, env_msg = validate_environment(CTX.config)
    if not valid_env:
        failures.append(f"ISC-68 — probe-gcache writes synthetic workload and must not run: {env_msg}")
        return None

    db_name = generate_measurement_db_name()
    script = build_workload_script(db_name, WORKLOAD_SECONDS)
    result = run_ansible(
        CTX,
        writer,
        script,
        timeout=WORKLOAD_SECONDS + 90,
    )
    require_hosts(result, [writer], "write-rate", failures, undetermined)
    if writer not in result.bodies:
        return None

    body = result.body(writer)

    match = re.search(r"RATE_BPS=(\d+)", body)
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
    valid_env, env_msg = validate_environment(CTX.config)
    if not valid_env:
        print(f"REFUSED: probe-gcache writes synthetic workload and must not run: {env_msg}")
        return 1
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
