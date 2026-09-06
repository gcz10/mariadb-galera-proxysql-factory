#!/usr/bin/env python3
"""ISC-39: backup must not degrade the active writer beyond threshold.

Runs the numbered workload through the VIP (real write load on the writer) and
triggers a full backup concurrently, then proves:
  - the worst-node Galera flow-control pause induced during the backup stays
    below threshold (per-node after-before delta; a dominant baseline on one
    node must not mask a real pause on another),
  - the workload keeps committing during the backup (no long write stall).

Lab-only (writes to isa_test); refuses on production.
Requires APP_DB_PASSWORD (+ backup secrets inherited by the backup playbook).
"""

import json
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

FLOW_THRESHOLD_NS = 2_000_000_000     # 2s worst-node cumulative flow-control pause
COMMIT_GAP_THRESHOLD = 8.0            # max seconds writer may stall
WORKLOAD_LOCAL = "tests/lab/workload-numbered.sh"
# Klient obciazenia: preferuj dedykowany host aplikacyjny (grupa `app`), zeby
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
CLUSTER_NAME = CLUSTER["cluster"]["name"]
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


class MeasurementError(RuntimeError):
    """Zla probka pomiaru — bramka fail-closed, nigdy nie domysla sie zera."""


FLOW_ROW_RE = re.compile(r"wsrep_flow_control_paused_ns\s+(\d+)")


def parse_flow_counter(node, result):
    """Parsuje SUROWY wynik SHOW STATUS dla jednego wezla (bez awka).

    Poprzednio `| awk '{print $2}'` maskowal blad mariadb: puste wyjscie
    dawalo int("" or 0) -> 0 i bramka przechodzila. Teraz kazda zla probka
    (rc != 0, puste/malformed wyjscie, ujemny licznik) jest jawnym bledem.
    """
    if result.returncode != 0:
        detail = ((result.stdout or "") + (result.stderr or "")).strip()[-200:]
        raise MeasurementError(
            f"flow control: SHOW STATUS failed on {node} (rc={result.returncode}): {detail}")
    payload = body(node, result.stdout).strip()
    if not payload:
        raise MeasurementError(
            f"flow control: empty wsrep_flow_control_paused_ns output on {node}")
    rows = [line.strip() for line in payload.splitlines() if line.strip()]
    if len(rows) != 1:
        raise MeasurementError(
            f"flow control: expected one status row on {node}, got {len(rows)}: {payload[:200]!r}")
    match = FLOW_ROW_RE.fullmatch(rows[0])
    if not match:
        raise MeasurementError(
            f"flow control: malformed wsrep_flow_control_paused_ns row on {node}: {rows[0]!r}")
    return int(match.group(1))


def sample_flow_counters():
    """Node -> wsrep_flow_control_paused_ns dla KAZDEGO oczekiwanego wezla.

    Zwraca (counters, errors): counters zawiera tylko poprawne probki,
    errors to czytelne opisy kazdej zlej (rc, puste wyjscie, malformed).
    """
    counters, errors = {}, []
    for node in GALERA:
        result = sh(node, "mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "
                          "\"SHOW STATUS LIKE 'wsrep_flow_control_paused_ns'\"")
        try:
            counters[node] = parse_flow_counter(node, result)
        except MeasurementError as exc:
            errors.append(str(exc))
    return counters, errors


def flow_gate_failures(before, after, threshold=FLOW_THRESHOLD_NS):
    """ISC-39 bramka flow control: najgorszy wezel, fail-closed.

    Delta liczona PER WEZEL (after - before) — dominujaca wartosc bazowa na
    jednym wezle nie moze zamaskowac wzrostu na innym. Zanik probki lub reset
    licznika (after < before) konczy sie bledem, nigdy delta = 0.
    Zwraca (failures, deltas): failures z nazwa wezla i liczbami, deltas to
    pelna mapa per-node do czytelnego wydruku.
    """
    if not GALERA:
        return ["flow control: no expected Galera nodes in inventory"], {}
    failures, deltas = [], {}
    for node in GALERA:
        base = before.get(node)
        post = after.get(node)
        if base is None or post is None:
            missing = "/".join(name for name, sample in
                               (("baseline", base), ("post-backup", post))
                               if sample is None)
            failures.append(f"flow control: missing {missing} sample for {node}")
        elif post < base:
            failures.append(
                f"flow control: counter on {node} decreased {base} -> {post} "
                "(reset; backup window unusable)")
        else:
            deltas[node] = post - base
    if deltas:
        worst_node, worst = max(deltas.items(), key=lambda item: item[1])
        if worst >= threshold:
            failures.append(
                f"ISC-39 — flow control {worst} ns on {worst_node} during backup "
                f"(>= {threshold} ns threshold); per-node deltas: "
                + ", ".join(f"{node}={delta}" for node, delta in sorted(deltas.items())))
    return failures, deltas


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


def last_backup_success_unixtime():
    """Najnowszy `last_success` backupu w calym klastrze.

    Donora wybiera runner przy starcie, wiec stan moze byc na dowolnym wezle
    Galery — bierzemy maksimum. Sluzy do udowodnienia, ze w oknie pomiaru
    NAPRAWDE powstala kopia; bez tego zerowy flow control niczego nie dowodzi.
    """
    newest = 0
    for node in GALERA:
        result = sh(node, f"cat /opt/galera-backup/clusters/{CLUSTER_NAME}/state.json 2>/dev/null || true")
        payload = body(node, result.stdout).strip()
        if not payload:
            continue
        try:
            state = json.loads(payload)
        except ValueError:
            continue
        success = state.get("last_success") or {}
        if success.get("command") == "backup":
            newest = max(newest, int(success.get("unixtime") or 0))
    return newest


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

        fc_before, fc_before_errors = sample_flow_counters()
        success_before = last_backup_success_unixtime()
        backup_start = time.time()
        print(f"running backup under load (flow_control baseline={fc_before})…")
        # `galera_backup_action=run` jest OBOWIAZKOWE. f10_backup.yml defaultuje
        # akcje na `configure` (linia 11), a play wykonujacy backup ma bramke
        # `when: galera_backup_action == 'run'` (linia 137). Bez tego przelacznika
        # ta sonda mierzyla okno BEZ backupu: flow control i stall wychodzily
        # zerowe z konstrukcji pomiaru, wiec bramka nie mogla sie zapalic na
        # czerwono. Forma identyczna z Makefile (cel `cluster-backup`).
        bkp = subprocess.run(
            [ANSIBLE.replace("ansible", "ansible-playbook") if ANSIBLE == "ansible" else "ansible-playbook",
             "playbooks/f10_backup.yml", "-i", INVENTORY,
             "-e", f"@{CONFIG_PATH}", "-e", "galera_backup_action=run"],
            capture_output=True, text=True, timeout=900)
        backup_end = time.time()
        fc_after, fc_after_errors = sample_flow_counters()
        success_after = last_backup_success_unixtime()

        if bkp.returncode != 0:
            failures.append(f"backup failed during load test: {bkp.stdout[-400:]}")

        time.sleep(2)
        sh(WORKLOAD_HOST, "rm -f /tmp/workload.run", check=True)
        time.sleep(1)

        # Flow control induced during the backup window — najgorszy wezel:
        # per-node delta; dominujaca baseline na innym wezle nie maskuje wzrostu.
        failures.extend(fc_before_errors)
        failures.extend(fc_after_errors)
        fc_failures, fc_deltas = flow_gate_failures(fc_before, fc_after)
        failures.extend(fc_failures)
        # Largest write stall that overlapped the backup window.
        times = [t for t in committed_times() if backup_start - 1 <= t <= backup_end + 1]
        gap = max((b - a for a, b in zip(times, times[1:])), default=0.0)

        if gap >= COMMIT_GAP_THRESHOLD:
            failures.append(f"ISC-39 — writer stalled {gap:.1f}s during backup "
                            f"(>= {COMMIT_GAP_THRESHOLD}s threshold)")
        if not times:
            failures.append("ISC-39 — no writes committed during backup window (workload not running?)")
        if success_after <= success_before:
            failures.append(
                "ISC-39 — w oknie pomiaru nie powstala nowa kopia "
                f"(last_success {success_before} -> {success_after}); bez wykonanego "
                "backupu zerowy flow control nie jest dowodem")

    finally:
        sh(WORKLOAD_HOST, f"rm -f /tmp/workload.run {CNF_REMOTE}", timeout=30)
        if local_cnf and os.path.exists(local_cnf):
            os.unlink(local_cnf)

    if failures:
        print("FAIL: backup-impact test failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    worst_node, worst_delta = max(fc_deltas.items(), key=lambda item: item[1])
    print(
        f"PASS: backup did not degrade writer — worst-node flow control "
        f"{worst_delta} ns on {worst_node} (per-node {fc_deltas}; "
        f"< {FLOW_THRESHOLD_NS} ns), max write stall {gap:.2f}s "
        f"(< {COMMIT_GAP_THRESHOLD}s) across {len(times)} commits during backup; "
        f"kopia wykonana w oknie (last_success {success_before} -> {success_after})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
