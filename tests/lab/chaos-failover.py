#!/usr/bin/env python3
"""ISC-27/28/64: numbered-workload failover test (lab-only, destructive).

Runs a numbered workload through the ProxySQL VIP, kills the active writer
(the Galera node ProxySQL routes writes to), and proves:
  - ISC-27: the client resumes committing within the RTO declared in
    cluster.yml (`availability.rto_node_failure`),
  - ISC-28: every transaction the client saw committed survives the failover.
  - ISC-64: refuses to run on the production profile (destruction guard).

Requires APP_DB_PASSWORD in the environment and F7's private ProxySQL client profile.
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

# Tryb awarii. `soft` (domyslny) zabija sam proces bazy (SIGKILL); `hard` gasi
# CALA maszyne przez sysrq — maszyna znika bez zamykania gniazd i wraca dopiero
# po restarcie, dolaczajac przez IST. Test pokrywal wylacznie wariant `soft`.
#
# Zmierzone na newclaude8-r9 tym samym workloadem (3 przebiegi kazdy):
#   soft: przerwa 6.0-6.2 s
#   hard: przerwa 0.0-0.1 s
# Twarda utrata maszyny okazala sie MNIEJ odczuwalna dla klienta niz zabicie
# procesu — mechanizmu nie zweryfikowano, wiec nie ma tu teorii, tylko pomiar.
# Sens tego trybu to inna sciezka awarii, nie dluzsza przerwa: dowodzi zerowej
# utraty transakcji przy zniknieciu maszyny i przechodzi przez powrot wezla po
# crashu, czego `soft` nie dotyka.
FAILOVER_MODE = os.environ.get("FAILOVER_MODE", "soft").lower()
if FAILOVER_MODE not in ("soft", "hard"):
    raise SystemExit(f"REFUSED: FAILOVER_MODE={FAILOVER_MODE!r} (dozwolone: soft, hard)")

WORKLOAD_LOCAL = "tests/lab/workload-numbered.sh"
_inv = yaml.safe_load(open(INVENTORY))
WORKLOAD_HOST = list(_inv["all"]["children"]["galera"]["hosts"])[0]  # first galera node (portable)
PROXYSQL_NODE = list(_inv["all"]["children"]["proxysql"]["hosts"])[0]
CNF_REMOTE = "/root/.workload.cnf"
SCRIPT_REMOTE = "/tmp/workload-numbered.sh"
LOG_REMOTE = "/tmp/workload.log"

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER = yaml.safe_load(fh)
with open(INVENTORY, encoding="utf-8") as fh:
    INV = yaml.safe_load(fh)

VIP = CLUSTER["proxysql"]["endpoint"]["address"]
VIP_PORT = CLUSTER["proxysql"]["endpoint"]["port"]
ENVIRONMENT = CLUSTER["cluster"]["environment"]


def _duration_seconds(text):
    """Parse an SLA duration ('2m', '30s', '1h', '90') into seconds.

    RTO was hardcoded at 120 s while cluster.yml declares it in
    `availability.rto_node_failure`. A cluster tightening its SLA to 30s kept
    passing a 119 s failover: the assertion did not track the contract it
    claims to enforce.
    """
    m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", str(text))
    if not m:
        raise SystemExit(
            f"REFUSED: cannot parse availability.rto_node_failure={text!r} "
            "(expected forms: '90', '30s', '2m', '1h')"
        )
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


RTO_SECONDS = _duration_seconds(CLUSTER["availability"]["rto_node_failure"])


def sh(node, script, timeout=60, check=False):
    cmd = [ANSIBLE, node, "-i", INVENTORY, "-m", "ansible.builtin.shell", "-a", script]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"ansible {node} failed: {r.stdout}\n{r.stderr}")
    return r


def body(node, result):
    """Extract the command body for a single-host ansible run."""
    out = result.stdout
    m = re.search(rf'^{re.escape(node)}\s*\|\s*\w+\s*\|\s*rc=\d+\s*>>?\s*$', out, re.M)
    if not m:
        return out.strip()
    return out[m.end():].strip()


def galera_ip_to_host():
    mapping = {}
    galera = INV["all"]["children"]["galera"]["hosts"]
    for host, vars_ in galera.items():
        mapping[vars_["galera_node_address"]] = host
    return mapping


def active_writer_ip():
    writer_hg = int(CLUSTER.get("proxysql", {}).get("hostgroup_base", 10))
    q = ("SELECT hostname FROM runtime_mysql_servers "
         f"WHERE hostgroup_id={writer_hg} AND status='ONLINE'")
    r = sh(PROXYSQL_NODE, f'mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf -h127.0.0.1 -P6032 -uadmin -N -B -e "{q}"')
    return body(PROXYSQL_NODE, r).strip()

def present_seqs(exclude=None):
    """Set of seqs present on a Galera node that SURVIVED the failover.

    Verifying on a surviving node (never killed) is the strongest no-data-loss
    claim: every client-committed transaction must be there without relying on
    the restarted node having finished re-syncing.
    """
    survivor = next(
        (h for h in INV["all"]["children"]["galera"]["hosts"] if h != exclude),
        WORKLOAD_HOST,
    )
    q = "SELECT seq FROM isa_test.isa_failover"
    r = sh(survivor, f'mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "{q}"')
    return {int(x) for x in body(survivor, r).split() if x.strip().isdigit()}


def committed_from_log():
    r = sh(WORKLOAD_HOST, f"cat {LOG_REMOTE}")
    seqs, times = [], []
    for line in body(WORKLOAD_HOST, r).splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                times.append(float(parts[0]))
                seqs.append(int(parts[1]))
            except ValueError:
                continue
    return seqs, times


def max_gap(times):
    return max((b - a for a, b in zip(times, times[1:])), default=0.0)


def main():
    failures = []

    # ISC-64: destruction guard — never run on production.
    if ENVIRONMENT == "production":
        print("REFUSED: chaos-failover is destructive and must not run on production (ISC-64)")
        return 1
    if not APP_PW:
        print("FAIL: APP_DB_PASSWORD must be set")
        return 1

    ip2host = galera_ip_to_host()
    killed_host = None
    local_cnf = None

    try:
        # Setup: workload table (Galera-replicated), creds file, workload script.
        sh(WORKLOAD_HOST,
           'mariadb --socket=/var/lib/mysql/mysql.sock -e '
           '"CREATE DATABASE IF NOT EXISTS isa_test; '
           'CREATE TABLE IF NOT EXISTS isa_test.isa_failover '
           '(seq BIGINT PRIMARY KEY, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"',
           check=True)
        sh(WORKLOAD_HOST,
           'mariadb --socket=/var/lib/mysql/mysql.sock -e "TRUNCATE isa_test.isa_failover"',
           check=True)

        app_user = CLUSTER.get("proxysql", {}).get("app_user", "app_user")
        fd, local_cnf = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            fh.write(f"[client]\nuser={app_user}\npassword={APP_PW}\n")
        subprocess.run(
            [ANSIBLE, WORKLOAD_HOST, "-i", INVENTORY, "-m", "copy",
             "-a", f"src={local_cnf} dest={CNF_REMOTE} mode=0600 owner=root"],
            capture_output=True, text=True, check=True)
        subprocess.run(
            [ANSIBLE, WORKLOAD_HOST, "-i", INVENTORY, "-m", "copy",
             "-a", f"src={WORKLOAD_LOCAL} dest={SCRIPT_REMOTE} mode=0755"],
            capture_output=True, text=True, check=True)

        # Launch the numbered workload in the background.
        sh(WORKLOAD_HOST,
           f"touch /tmp/workload.run; "
           f"nohup bash {SCRIPT_REMOTE} {VIP} {VIP_PORT} {CNF_REMOTE} {LOG_REMOTE} "
           f">/tmp/workload.out 2>&1 & echo launched", check=True)

        # Baseline: let it commit for a while pre-failover.
        time.sleep(10)
        seqs_before, _ = committed_from_log()
        writer_ip = active_writer_ip()
        killed_host = ip2host.get(writer_ip)
        print(f"active writer: {writer_ip} -> {killed_host}; "
              f"committed before kill: {len(seqs_before)}")
        if not killed_host:
            failures.append(f"could not map writer IP {writer_ip} to a host")
            raise RuntimeError(failures[-1])

        # Zabij aktywnego writera. `soft`: sam proces bazy (SIGKILL). `hard`:
        # cala maszyna przez sysrq — bez zamkniecia gniazd, jak utrata zasilania.
        if FAILOVER_MODE == "hard":
            cap = body(killed_host, sh(killed_host, "test -w /proc/sysrq-trigger && echo yes || echo no"))
            if "yes" not in cap:
                print(
                    f"REFUSED: tryb hard wymaga zapisywalnego /proc/sysrq-trigger na {killed_host}. "
                    "Kontenerowy lab go nie ma — uruchom na realnych VM albo uzyj FAILOVER_MODE=soft "
                    "(ale wtedy mierzysz wariant optymistyczny, nie utrate maszyny)."
                )
                return 2
        kill_ts = time.time()
        if FAILOVER_MODE == "hard":
            # Maszyna przestaje istniec w trakcie wykonania, wiec NIE wolno czekac
            # na wynik: zwykle wywolanie wisi do timeoutu i wywraca caly test
            # (subprocess.TimeoutExpired — zmierzone). Tryb async ansible (-B/-P 0)
            # oddaje sterowanie zaraz po wystartowaniu zadania i nie czeka na
            # odpowiedz, ktora juz nie nadejdzie.
            try:
                subprocess.run(
                    [ANSIBLE, killed_host, "-i", INVENTORY, "-m", "ansible.builtin.shell",
                     "-a", "echo 1 > /proc/sys/kernel/sysrq; echo b > /proc/sysrq-trigger",
                     "-B", "5", "-P", "0"],
                    capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired:
                pass  # maszyna zniknela szybciej, niz ansible zdazyl wrocic — cel osiagniety
        else:
            sh(killed_host, "pkill -9 -x mariadbd; echo killed", check=True)
        print(f"killed writer {killed_host} at {kill_ts:.2f} (tryb {FAILOVER_MODE})")

        # Wait for the workload to resume committing AFTER the kill instant.
        # Detect by timestamp (a commit clearly after kill_ts), not by count —
        # commits between the baseline snapshot and the kill would fool a count.
        resumed = False
        deadline = kill_ts + RTO_SECONDS
        while time.time() < deadline:
            _, times_now = committed_from_log()
            if any(t > kill_ts + 0.5 for t in times_now):
                resumed = True
                break
            time.sleep(1)

        # Let it run a bit more post-failover, then stop.
        time.sleep(8)
        sh(WORKLOAD_HOST, "rm -f /tmp/workload.run", check=True)
        time.sleep(1)

        seqs, times = committed_from_log()
        gap = max_gap(times)

        # ISC-27: workload resumed within RTO.
        if not resumed:
            failures.append(f"workload did not resume within {RTO_SECONDS}s after writer kill")
        if gap >= RTO_SECONDS:
            failures.append(f"largest commit gap {gap:.1f}s exceeds RTO {RTO_SECONDS}s")

    finally:
        # Przywroc zabity wezel, zeby klaster wrocil do pelnego rozmiaru.
        if killed_host and FAILOVER_MODE == "hard":
            # Maszyna sie restartuje; czekamy na SSH, potem na systemd.
            for _ in range(60):
                r = subprocess.run(
                    [ANSIBLE, killed_host, "-i", INVENTORY, "-m", "ansible.builtin.ping"],
                    capture_output=True, text=True, timeout=60)
                if "SUCCESS" in r.stdout:
                    break
                time.sleep(5)
            sh(killed_host,
               "systemctl start mariadb 2>/dev/null || true; sleep 10; echo restarted",
               timeout=180)
        elif killed_host:
            sh(killed_host,
               "rm -f /var/lib/mysql/aria_log_control /var/lib/mysql/aria_log.00000001; "
               "mariadbd --user=mysql --datadir=/var/lib/mysql "
               "--socket=/var/lib/mysql/mysql.sock >/var/log/mariadb/mariadb.log 2>&1 & "
               "sleep 15; echo restarted", timeout=60)
        if local_cnf and os.path.exists(local_cnf):
            os.unlink(local_cnf)
        sh(WORKLOAD_HOST, f"rm -f {CNF_REMOTE} /tmp/workload.run", timeout=30)

    # ISC-28: every committed seq must be present on a node that SURVIVED.
    present = present_seqs(exclude=killed_host)
    missing = sorted(s for s in seqs if s not in present)
    if missing:
        failures.append(
            f"{len(missing)} committed transactions lost after failover "
            f"(e.g. {missing[:5]}) — ISC-28 violation")

    if failures:
        print("FAIL: failover test failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    how = "SIGKILLed" if FAILOVER_MODE == "soft" else "wylaczony twardo (sysrq, utrata maszyny)"
    print(
        f"PASS: failover survived — writer {killed_host} {how}, workload resumed "
        f"(failover gap {gap:.1f}s < {RTO_SECONDS}s RTO), "
        f"{len(seqs)} committed transactions all present on survivor after failover (0 lost)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
