#!/usr/bin/env python3
"""ISC-68: gcache.size is computed from measured write rate × IST window.

Galera's gcache stores recent write-sets so a returning node can rejoin via IST
(incremental) instead of a full SST. The required size:

    gcache.size = write_rate_bytes_per_sec × ist_window_minutes × 60

This probe MEASURES a write rate (a short write workload → wsrep_replicated_bytes
delta), COMPUTES the required gcache for the target IST window, and verifies the
DEPLOYED gcache.size (in server.cnf) covers the requirement (and the 128M floor).

WHAT THE NUMBER IS, EXACTLY: the writer's SATURATION throughput for a synthetic
1 KiB write stream — an UPPER bound, not the tenant's production write rate.
Batches of 500 INSERTs share one client connection, so the window measures
replication, not logins.

History worth keeping, because the earlier version looked green while proving
nothing: it invoked the client ONCE PER INSERT, so connection setup consumed the
window and the "write rate" came out at 77-87 kB/s — login speed. An understated
rate understates the REQUIRED gcache, so the gate passed on an unmet criterion.
Measured correctly on 2026-09-08: 2.1 MB/s, i.e. 3645M required against 512M
deployed on both live tenants (FAIL, see ISA.md).

Sizing a real workload still belongs to production data
(`tests/validation/calc-gcache.py --write-rate` over an hour of
`wsrep_replicated_bytes`); this probe answers the worst case, not the average.

Falsifiable: if the deployed gcache is smaller than what the measured write rate
requires for the IST window, the probe FAILS (a node down for the window would
fall back to full SST instead of IST).

History, third chapter (2026-09-15): a SINGLE 20-second sample was not
reproducible enough to decide the verdict. The same probe on the same node with
the same deployed gcache measured 1948800 / 2436000 / 2401200 / 3508143 B/s,
i.e. required 3346M / 4182M / 4122M / 6023M against 4096M deployed — the verdict
flipped between PASS and FAIL with no change to the cluster. Fixed on both ends:
the workload now runs `ISC68_ROUNDS` windows (default 5) and the verdict uses the
WORST (max), which matches this probe's stated worst-case contract and is an
upper bound rather than a coin flip; the rate is computed from a nanosecond clock
and over the SAME span as the counter delta (previously integer seconds, ±5%, with
the delta window offset from the timer). Samples are printed (min/median/max) so a
reader sees the distribution instead of one number.

History, second chapter (2026-09-12): the deployed value was read from GALERA[0]
alone, yet the verdict printed and the PASS summary claimed the whole cluster. A
single-node read is not a fleet claim: with g1=512M and g2=g3=128M the probe
queried g1, saw 512M, and returned exit 0 while the two nodes that would fall back
to full SST were never looked at. The deployed value is now read from EVERY galera
host and judged by the SMALLEST of them; divergence names the lagging nodes.
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
ROUNDS = int(os.environ.get("ISC68_ROUNDS", "5"))
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


def build_workload_script(db_name: str, workload_seconds: int, rounds: int) -> str:
    """Build isolated workload script with exclusive DB creation and trap-based cleanup.

    Contract:
    - set -eo pipefail and traps EXIT TERM INT so errors cannot be hidden.
    - Splits CREATE DATABASE and CREATE TABLE; sets OWNED=1 immediately after DB success
      so table or insert failures trigger cleanup.
    - Trap checks OWNED: never drops colliding or pre-existing unowned databases.
    - Cleanup failure forces nonzero exit (no || true).
    - Runs `rounds` measurement windows and prints one `ROUND=<n> RATE_BPS=<n>` line
      each; the caller takes the worst (max), matching the probe's worst-case contract.
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

# JEDNO polaczenie na partie 500 zapisow, nie jedno na zapis. Wczesniejsza
# wersja wolala klienta w petli, wiec wieksza czesc okna zjadalo nawiazywanie
# polaczen: zmierzone 77-87 kB/s bylo predkoscia LOGOWANIA, nie replikacji, a
# zanizona stawka zaniza WYMAGANY gcache — blad w strone niebezpieczna.
BATCH=$(seq 1 500 | sed "s|.*|INSERT INTO \`$DB\`.w (payload) VALUES (RPAD('x',1024,'x'));|")

counter() {{
  mariadb --socket=$SOCK -N -B -e "SHOW STATUS LIKE 'wsrep_replicated_bytes'" | awk '{{print $2}}'
}}

# N rund; raportowana jest NAJWYZSZA. Sonda odpowiada na pytanie o NAJGORSZY
# przypadek (docstring), wiec statystyka jest MAKSIMUM, nie pojedyncza probka.
# ZMIERZONE 2026-09-15: pojedyncza probka tej samej sondy na tym samym wezle
# dawala 1948800-3508143 B/s, czyli wymagania 3346-6023M wobec wdrozonych 4096M
# — werdykt przelaczal sie miedzy PASS i FAIL bez zmiany stanu floty.
#
# Zegar w NANOSEKUNDACH, nie w calkowitych sekundach: poprzednia wersja liczyla
# ELAPSED z `date +%s` (±1 s = ±5% w 20-sekundowym oknie), a delte licznika
# brala z okna przesunietego wzgledem zegara (T0 czytane PRZED START).
# Teraz obie granice sa te same: B0/T0 przed pierwsza partia, B1/T1 po ostatniej.
for ROUND in $(seq 1 {rounds}); do
  T0=$(date +%s%N)
  B0=$(counter)
  while :; do
    printf '%s\n' "$BATCH" | mariadb --socket=$SOCK
    now=$(date +%s%N); [ $(( (now-T0)/1000000000 )) -ge {workload_seconds} ] && break
  done
  B1=$(counter)
  T1=$(date +%s%N)
  ELAPSED_NS=$((T1-T0))
  DELTA=$((B1-B0))
  echo "ROUND=$ROUND RATE_BPS=$(( ELAPSED_NS>0 ? DELTA*1000000000/ELAPSED_NS : 0 )) DELTA=$DELTA ELAPSED_NS=$ELAPSED_NS"
done
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


def select_worst_rate(body: str) -> tuple[int, list[int]]:
    """(najgorszy zmierzony rate, wszystkie probki) z wyjscia skryptu.

    Sonda odpowiada na pytanie o NAJGORSZY przypadek (docstring), wiec werdykt
    opiera sie na MAKSIMUM z rund. Bierze sie je dlatego, ze pojedyncza probka
    tej samej sondy na tym samym wezle zmierzyla 1948800-3508143 B/s, czyli
    wymagania 3346-6023M wobec wdrozonych 4096M — werdykt przelaczal sie miedzy
    PASS i FAIL bez zmiany stanu floty. Maksimum z N rund jest ograniczeniem
    gornym (rosnie monotonicznie z N), wiec PASS na nim jest twierdzeniem
    mocnym, a FAIL slabym — i taka asymetrie wlasnie chcemy przy doborze
    rozmiaru na najgorszy przypadek.
    """
    samples = [int(m) for m in re.findall(r"ROUND=\d+ RATE_BPS=(\d+)", body)]
    return (max(samples) if samples else 0), samples


def measure_write_rate(writer, failures, undetermined):
    """Run a write workload on the writer; return (worst bytes/sec, all samples)."""
    if writer is None:
        return None, []
    valid_env, env_msg = validate_environment(CTX.config)
    if not valid_env:
        failures.append(f"ISC-68 — probe-gcache writes synthetic workload and must not run: {env_msg}")
        return None, []

    db_name = generate_measurement_db_name()
    script = build_workload_script(db_name, WORKLOAD_SECONDS, ROUNDS)
    result = run_ansible(
        CTX,
        writer,
        script,
        timeout=ROUNDS * WORKLOAD_SECONDS + 90,
    )
    require_hosts(result, [writer], "write-rate", failures, undetermined)
    if writer not in result.bodies:
        return None, []

    return select_worst_rate(result.body(writer))

def parse_gcache(body: str) -> int:
    """Wartosc gcache.size z ciala odpowiedzi wezla; brak wpisu = 0.

    Zero (a nie "nie wiem") jest celowe: wezel bez ustawionego gcache.size nie
    zmiesci zadnego write-setu, wiec spada ponizej progu 128M i jest FAIL-em.
    """
    match = re.search(r"gcache.size=(\d+)([MG])", body, re.I)
    if not match:
        return 0
    val = int(match.group(1))
    return val * 1024 if match.group(2).upper() == "G" else val


def deployed_gcache(failures, undetermined):
    """Zwraca (najmniejsza wartosc w MB, {host: MB}) z CALEGO klastra Galery.

    Pojedynczy odczyt nie jest twierdzeniem o flocie: o wyniku decyduje
    NAJMNIEJSZA wartosc, bo od niej zalezy, czy kazdy wezel wroci po awarii
    przez IST. Host bez odpowiedzi trafia do `undetermined` przez
    `require_hosts` (exit 2), a nie do pomiaru na cichym podzbiorze.
    """
    if not GALERA:
        undetermined.append("gcache-deployed: inwentarz nie definiuje hostow galera")
        return None, {}
    result = run_ansible(
        CTX,
        "galera",
        "grep -ioE 'gcache.size=[0-9]+[MG]' /etc/my.cnf.d/server.cnf || true",
    )
    require_hosts(result, GALERA, "gcache-deployed", failures, undetermined)
    per_host = {}
    for host in GALERA:
        if host not in result.bodies:
            continue
        per_host[host] = parse_gcache(result.body(host))
    if not per_host:
        return None, {}
    return min(per_host.values()), per_host


def main():
    failures = []
    undetermined = []
    valid_env, env_msg = validate_environment(CTX.config)
    if not valid_env:
        print(f"REFUSED: probe-gcache writes synthetic workload and must not run: {env_msg}")
        return 1
    writer = find_writer(failures, undetermined)
    rate, samples = measure_write_rate(writer, failures, undetermined)
    if rate is None:
        required_mb = None
    elif rate <= 0:
        failures.append("ISC-68 — write rate not measurable (0): no workload data")
        required_mb = 128
    else:
        gcache_bytes = rate * IST_WINDOW_MIN * 60
        required_mb = max(math.ceil(gcache_bytes / (1024 * 1024)), 128)
    # O wyniku decyduje NAJMNIEJSZA wartosc we flocie: IST musi zmiescic sie na
    # kazdym wezle, nie tylko na tym najhojniej skonfigurowanym. Komunikat FAIL
    # wymienia wezly ponizej wymagania, zeby operator wiedzial, ktore poprawic.
    min_deployed_mb, per_host = deployed_gcache(failures, undetermined)
    deployed_note = ", ".join(f"{host}={per_host[host]}M" for host in sorted(per_host))
    print(
        f"writer={writer or 'unknown'} write_rate={rate if rate is not None else 'unknown'} "
        f"B/s  ist_window={IST_WINDOW_MIN}min"
    )
    if samples:
        s_sorted = sorted(samples)
        s_med = s_sorted[len(s_sorted) // 2]
        print(
            f"  proby ({len(samples)} rund, werdykt na MAKSIMUM — najgorszy przypadek): "
            f"min={s_sorted[0]} mediana={s_med} max={s_sorted[-1]} B/s  [{', '.join(str(s) for s in samples)}]"
        )
    print(
        f"required gcache={required_mb if required_mb is not None else 'unknown'}M "
        f"(min 128M)  deployed gcache="
        f"{min_deployed_mb if min_deployed_mb is not None else 'unknown'}M"
        + (f" (min z {len(per_host)} wezlow galera; per host: {deployed_note})" if per_host else "")
    )

    if min_deployed_mb is not None:
        if min_deployed_mb < 128:
            failures.append(
                f"ISC-68 — deployed gcache {min_deployed_mb}M below 128M floor "
                f"(per host: {deployed_note})"
            )
        if required_mb is not None and min_deployed_mb < required_mb:
            lagging = sorted(host for host, mb in per_host.items() if mb < required_mb)
            failures.append(
                f"ISC-68 — deployed gcache {min_deployed_mb}M < required {required_mb}M "
                f"(write_rate={rate}B/s × {IST_WINDOW_MIN}min); nodes below the requirement: "
                f"{', '.join(lagging)} (per host: {deployed_note}): those nodes would need "
                f"full SST after the IST window"
            )

    if min_deployed_mb is None:
        certified = "unknownM"
    elif min_deployed_mb == max(per_host.values()):
        certified = f"{min_deployed_mb}M on all {len(per_host)} galera nodes"
    else:
        certified = (
            f"min {min_deployed_mb}M of {max(per_host.values())}M across "
            f"{len(per_host)} galera nodes ({deployed_note})"
        )

    return finish(
        failures,
        undetermined,
        f"ISC-68 — gcache.size={certified} covers write_rate={rate}B/s "
        f"for {IST_WINDOW_MIN}min IST window (required {required_mb}M)",
    )


if __name__ == "__main__":
    sys.exit(main())
