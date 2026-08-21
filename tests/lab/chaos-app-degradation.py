#!/usr/bin/env python3
"""Co widzi APLIKACJA, gdy klaster traci kworum (lab-only, destrukcyjny).

Sondy stanu ustalonego mowia, ze wszystko dziala, dopoki wszystko dziala. Ta
sprawdza zachowanie w awarii — z hosta aplikacyjnego, przez VIP, tak jak
zobaczylaby to aplikacja.

KONTRAKT, KTOREGO PILNUJE:
  1. BEZPIECZENSTWO: gdy klaster traci kworum, zapis aplikacji MUSI zostac
     odrzucony. Przyjecie zapisu przez wezel bez kworum to utrata danych i
     rozjazd stanu (ISC-30).
  2. DIAGNOZOWALNOSC: aplikacja musi dostac blad, po ktorym da sie zareagowac.
     Wezel bezposrednio zwraca "ERROR 1047 (08S01) WSREP has not yet prepared
     node for application use" — SQLSTATE 08S01 to standardowy blad polaczenia,
     na ktory sterowniki i pule maja gotowa obsluge (retry, odswiezenie puli).
     Przez ProxySQL ta sama sytuacja daje dzis "ERROR 2027 (HY000) Received
     malformed packet" — blad PROTOKOLU, nieodrozniqlny od uszkodzonej sieci
     czy buga w kliencie. Zmierzone na tej flocie.
  3. POWROT: po przywroceniu wezlow aplikacja musi zaczac dzialac bez interwencji.

Punkt 2 jest dzis ZLAMANY i nie da sie tego naprawic w tym repo. Przyczyna
ustalona przez ELIMINACJE (n11), nie przez domysl:
  * NIE TLS — plaintext daje ten sam ERROR 2027,
  * NIE routing — `SELECT 1` przez VIP w tym samym momencie PRZECHODZI,
  * NIE ponowienia — `mysql-query_retries_on_failure=0` nic nie zmienia,
  * NIE brak wiedzy ProxySQL — jego log zawiera dokladnie
    "Error during query on (650,...): 1047, WSREP has not yet prepared node".
ProxySQL ZNA poprawny blad i gubi go dopiero przy kodowaniu odpowiedzi do
klienta. Ta sama sciezka (MySQL_Result_to_MySQL_wire) byla zrodlem upstreamowego
crasha przy 1047 (sysown/proxysql#1596, naprawiony w 1.4.9); na 3.0.10 nie ma
juz crasha, zostal uszkodzony pakiet.

Osobno sprostowane: ProxySQL POPRAWNIE przenosi wezel poza kworum do
offline_hostgroup, gdy jest kogo promowac (zmierzone: pojedynczy wezel odciety
od klastra ladowal w hg offline). Nietkniety zostaje tylko OSTATNI wezel —
"last man standing". Dlatego oczekiwanie jest STEROWANE flaga
APP_QUORUM_ERROR_CONTRACT:
  * "degraded" (domyslnie) — wiemy o zlamaniu; sonda pada, jesli stan sie zmieni
    W DOWOLNA STRONE, zeby naprawa nie przeszla niezauwazona,
  * "clean" — wymagamy bledu bazodanowego (1047/08S01 lub czysty blad polaczenia).

Wymaga APP_DB_PASSWORD. Odmawia uruchomienia na profilu produkcyjnym (ISC-64).
"""

import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import yaml

from _quorum_evidence import (
    LOCAL_ACCEPTANCE,
    OUTCOME_CLEAN,
    OUTCOME_DEGRADED,
    OUTCOME_UNRESOLVED,
    acceptance_failures,
    classify_outcome,
    option_file_quote,
    parse_client_error,
    parse_tsv,
    proxy_log_proves_backend_error,
    recovery_complete,
)

__all__ = (
    "LOCAL_ACCEPTANCE",
    "OUTCOME_CLEAN",
    "OUTCOME_DEGRADED",
    "OUTCOME_UNRESOLVED",
    "acceptance_failures",
    "arm_node",
    "classify_outcome",
    "cleanup_nodes",
    "datetime",
    "json",
    "new_record",
    "parse_client_error",
    "proxy_log_proves_backend_error",
    "recovery_complete",
    "require_ok",
    "run_measurement",
    "write_artifact",
)

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")
APP_PW = os.environ.get("APP_DB_PASSWORD", "")
CONTRACT = os.environ.get("APP_QUORUM_ERROR_CONTRACT", "degraded")

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER = yaml.safe_load(fh)
with open(INVENTORY, encoding="utf-8") as fh:
    INV = yaml.safe_load(fh)

VIP = CLUSTER["proxysql"]["endpoint"]["address"]
VIP_PORT = CLUSTER["proxysql"]["endpoint"]["port"]
APP_USER = CLUSTER.get("proxysql", {}).get("app_user", "app_user")
ENVIRONMENT = CLUSTER["cluster"]["environment"]

GALERA = list(INV["all"]["children"]["galera"]["hosts"].keys())
_app = (INV["all"]["children"].get("app") or {}).get("hosts") or {}
APP_HOST = next(iter(_app)) if _app else None

CLUSTER_NAME = CLUSTER["cluster"]["name"]
NODES_EXPECTED = int(CLUSTER["galera"]["nodes_expected"])
PROXYSQL_HOSTS = list(((INV["all"]["children"].get("proxysql") or {}).get("hosts") or {}))
GALERA_ADDR = {
    host: (values or {}).get("ansible_host", host)
    for host, values in INV["all"]["children"]["galera"]["hosts"].items()
}
PROXYSQL_ADDR = {
    host: (values or {}).get("ansible_host", host)
    for host, values in ((INV["all"]["children"].get("proxysql") or {}).get("hosts") or {}).items()
}
HG_BASE = int(CLUSTER["proxysql"]["hostgroup_base"])
WRITER_HG = HG_BASE
OFFLINE_HG = HG_BASE + 30

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_CLUSTER = "newclaude16-r9"
EXPECTED_CONFIG = (REPO_ROOT / "clusters/newclaude16-r9/cluster.yml").resolve()
EXPECTED_INVENTORY = (REPO_ROOT / "clusters/newclaude16-r9/inventory.yml").resolve()
EXPECTED_GALERA = {"n16g1", "n16g2", "n16g3"}
EXPECTED_PROXYSQL = {"fcp1", "fcp2"}
EXPECTED_APP = {"fcapp"}
APP_CNF = "/run/isa-app-degradation.cnf"
PROXYSQL_LOG = "/var/lib/proxysql/proxysql.log"
ADMIN_CLIENT = ("mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf "
                "-h127.0.0.1 -P6032 -uadmin -N -B")
RUN_ID = os.environ.get("QUORUM_RUN_ID", "")

# Gasimy WSZYSTKIE poza jednym: zostaje mniejszosc 1 z N.
#
# MUSI to byc wyjscie NAGLE (SIGKILL), nie `systemctl stop`. Przy lagodnym
# zamknieciu wezel zglasza odejscie, Galera przelicza sklad i ocalaly zostaje
# Primary — kworum nie ginie, a test mierzy cos innego, niz deklaruje.
# Zmierzone: po `systemctl stop` na dwoch wezlach trzeci raportowal Primary
# i normalnie przyjmowal zapisy. Awaria zasilania czy panika jadra nie wysyla
# pozegnania — i to wlasnie odtwarzamy.
#
# Zabijamy proces, nie maszyne: powrot przez `systemctl start` jest szybki i nie
# wymaga API hypervisora, wiec sonda dziala takze tam, gdzie go nie ma.
SURVIVOR = GALERA[0]
STOPPED = GALERA[1:]

# Drop-in zdejmujacy systemd polityke restartu NA CZAS testu. Nazwa `zz-` daje
# pewnosc, ze wchodzi po innych drop-inach (np. TimeoutStartSec z F5).
DROPIN_DIR = "/etc/systemd/system/mariadb.service.d"
DROPIN = f"{DROPIN_DIR}/zz-chaos-norestart.conf"


class EvidenceError(RuntimeError):
    pass


def validate_target(cluster_name, config_path, inventory_path, galera_hosts, proxy_hosts, app_hosts):
    errors = []
    if cluster_name != EXPECTED_CLUSTER:
        errors.append(f"cluster name must be {EXPECTED_CLUSTER}, got {cluster_name}")
    if Path(config_path).resolve() != EXPECTED_CONFIG:
        errors.append(f"config path must resolve to {EXPECTED_CONFIG}")
    if Path(inventory_path).resolve() != EXPECTED_INVENTORY:
        errors.append(f"inventory path must resolve to {EXPECTED_INVENTORY}")
    if set(galera_hosts) != EXPECTED_GALERA:
        errors.append(f"Galera hosts must be {sorted(EXPECTED_GALERA)}")
    if set(proxy_hosts) != EXPECTED_PROXYSQL:
        errors.append(f"ProxySQL hosts must be {sorted(EXPECTED_PROXYSQL)}")
    if set(app_hosts) != EXPECTED_APP:
        errors.append(f"app hosts must be {sorted(EXPECTED_APP)}")
    if ENVIRONMENT != "laboratory":
        errors.append(f"environment must be laboratory, got {ENVIRONMENT!r}")
    if not re.fullmatch(r"[0-9a-f]{32}", RUN_ID):
        errors.append("QUORUM_RUN_ID must be exactly 32 lowercase hex characters")
    return errors


def sh(host, script, timeout=120):
    cmd = [ANSIBLE, host, "-i", INVENTORY, "-m", "ansible.builtin.shell", "-a", script]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = r.stdout
    m = re.search(rf'^{re.escape(host)}\s*\|\s*\w+\s*\|\s*rc=(\d+)\s*>>?\s*$', out, re.M)
    if not m:
        return 1, (out + r.stderr).strip()
    return int(m.group(1)), out[m.end():].strip()


def safe_sh(host, script, timeout=120):
    try:
        rc, output = sh(host, script, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "rc": None, "output": "", "error": f"timeout after {exc.timeout}s"}
    except Exception as exc:
        return {"ok": False, "rc": None, "output": "", "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": rc == 0,
        "rc": rc,
        "output": output.strip(),
        "error": "" if rc == 0 else output.strip()[:300],
    }


def must_output(host, script, label, timeout=120):
    result = safe_sh(host, script, timeout=timeout)
    if not result["ok"]:
        raise EvidenceError(f"{label} on {host}: {result['error']}")
    return result["output"]


def install_app_profile():
    fd, local_path = tempfile.mkstemp(prefix="isa-app-degradation-", suffix=".cnf", text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                "[client]\n"
                f"user={option_file_quote(APP_USER)}\n"
                f"password={option_file_quote(APP_PW)}\n"
            )
        command = [
            ANSIBLE, APP_HOST, "-i", INVENTORY,
            "-m", "ansible.builtin.copy",
            "-a", f"src={local_path} dest={APP_CNF} owner=root group=root mode=0600",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise EvidenceError((result.stdout + result.stderr).strip()[:300])
    finally:
        if os.path.exists(local_path):
            os.unlink(local_path)


def remove_app_profile(attempts=3):
    history = []
    for attempt in range(1, attempts + 1):
        remove = safe_sh(APP_HOST, f"rm -f {APP_CNF}", timeout=60)
        verify = safe_sh(APP_HOST, f"test ! -e {APP_CNF} && echo ABSENT || echo PRESENT", timeout=60)
        history.append({"attempt": attempt, "remove": remove, "verify": verify})
        if verify["ok"] and verify["output"] == "ABSENT":
            return {"absent": True, "history": history}
        time.sleep(2)
    return {"absent": False, "history": history}


def app_query(sql):
    return safe_sh(
        APP_HOST,
        f"timeout 25 mariadb --defaults-extra-file={APP_CNF} "
        f"-h {VIP} -P {VIP_PORT} --ssl-verify-server-cert=0 --connect-timeout=5 "
        f"isa_test -e \"{sql}\" 2>&1",
        timeout=40,
    )


def app_setup():
    return app_query(
        "CREATE TABLE IF NOT EXISTS app_degradation "
        "(id BIGINT AUTO_INCREMENT PRIMARY KEY, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )


def app_write():
    return app_query("INSERT INTO app_degradation () VALUES ()")


def admin_rows(host, sql, columns):
    output = must_output(host, f'{ADMIN_CLIENT} -e "{sql}" 2>&1', "ProxySQL admin query", timeout=60)
    return parse_tsv(output, columns)


def galera_state(host):
    output = must_output(
        host,
        "timeout 15 mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e \""
        "SHOW STATUS WHERE Variable_name IN ('wsrep_cluster_status',"
        "'wsrep_cluster_size','wsrep_local_state')\" 2>&1",
        "Galera state",
        timeout=30,
    )
    values = {}
    for row in parse_tsv(output, ("name", "value")):
        values[row["name"]] = row["value"]
    return (f"{values.get('wsrep_cluster_status', '?')}/"
            f"{values.get('wsrep_cluster_size', '?')}/"
            f"{values.get('wsrep_local_state', '?')}")


def os_version(host):
    return must_output(
        host,
        '. /etc/os-release && printf "%s; kernel %s" "$PRETTY_NAME" "$(uname -r)"',
        "OS version",
        timeout=30,
    )


def vip_holder():
    holders = []
    for host in PROXYSQL_HOSTS:
        result = safe_sh(host, "ip -br -4 addr show 2>&1", timeout=30)
        if not result["ok"]:
            raise EvidenceError(f"VIP probe failed on {host}: {result['error']}")
        # Check if VIP with CIDR mask prefix (e.g. "192.168.1.133/") is present in ip addr output
        if f"{VIP}/" in result["output"]:
            holders.append(host)
    if len(holders) != 1:
        raise EvidenceError(f"expected exactly one VIP holder, got {holders}")
    return holders[0]


def runtime_snapshot(host):
    rows = admin_rows(
        host,
        f"SELECT hostgroup_id, hostname, status FROM runtime_mysql_servers "
        f"WHERE hostgroup_id IN ({WRITER_HG}, {OFFLINE_HG}) ORDER BY hostgroup_id, hostname",
        ("hostgroup_id", "hostname", "status"),
    )
    for row in rows:
        row["hostgroup_id"] = int(row["hostgroup_id"])
    return rows


def collect_versions():
    proxy_versions = {}
    proxy_os = {}
    for host in PROXYSQL_HOSTS:
        rows = admin_rows(
            host,
            "SELECT variable_value FROM global_variables WHERE variable_name='admin-version'",
            ("version",),
        )
        if len(rows) != 1 or not rows[0]["version"]:
            raise EvidenceError(f"missing admin-version on {host}")
        proxy_versions[host] = rows[0]["version"]
        proxy_os[host] = os_version(host)
    mariadb = must_output(
        SURVIVOR,
        "timeout 15 mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e \"SELECT VERSION()\"",
        "MariaDB version",
        timeout=30,
    )
    client = must_output(APP_HOST, "mariadb --version", "client version", timeout=30)
    return {
        "proxysql": proxy_versions,
        "mariadb": mariadb,
        "client": client,
        "os_proxysql": proxy_os,
        "os_backend": os_version(SURVIVOR),
    }


def log_mark(host):
    output = must_output(host, f"stat -Lc '%i\\t%s' {PROXYSQL_LOG}", "ProxySQL log mark", timeout=30)
    rows = parse_tsv(output, ("inode", "size"))
    if len(rows) != 1:
        raise EvidenceError(f"invalid log mark on {host}: {output!r}")
    return {"inode": int(rows[0]["inode"]), "size": int(rows[0]["size"])}


def log_delta(host, mark):
    output = must_output(host, f"stat -Lc '%i\\t%s' {PROXYSQL_LOG}", "ProxySQL log recheck", timeout=30)
    rows = parse_tsv(output, ("inode", "size"))
    if len(rows) != 1:
        raise EvidenceError(f"invalid log recheck on {host}: {output!r}")
    inode, size = int(rows[0]["inode"]), int(rows[0]["size"])
    if inode != mark["inode"]:
        raise EvidenceError(f"ProxySQL log rotated on {host}: inode {mark['inode']} -> {inode}")
    if size < mark["size"]:
        raise EvidenceError(f"ProxySQL log shrank on {host}: {mark['size']} -> {size}")
    delta = must_output(
        host,
        f"tail -c +{mark['size'] + 1} {PROXYSQL_LOG} 2>/dev/null",
        "ProxySQL log delta",
        timeout=60,
    )
    lines = delta.splitlines()
    if len(lines) > 200:
        lines = [f"[pominieto {len(lines) - 200} wczesniejszych linii]"] + lines[-200:]
    return "\n".join(lines)


def monitor_row(host, survivor_address):
    rows = admin_rows(
        host,
        "SELECT hostname, time_start_us, primary_partition, wsrep_local_state, "
        "COALESCE(error, '') FROM mysql_server_galera_log "
        f"WHERE hostname='{survivor_address}' ORDER BY time_start_us DESC LIMIT 1",
        ("hostname", "time_start_us", "primary_partition", "wsrep_local_state", "error"),
    )
    if len(rows) != 1:
        raise EvidenceError(f"missing latest monitor row for {survivor_address} on {host}")
    row = rows[0]
    row["time_start_us"] = int(row["time_start_us"])
    row["wsrep_local_state"] = int(row["wsrep_local_state"])
    return row


def require_ok(result, label):
    if not result["ok"]:
        raise EvidenceError(f"{label}: {result['error']}")
    return result["output"]


def arm_node(host, run=safe_sh):
    require_ok(run(host, f"mkdir -p {DROPIN_DIR} && printf '[Service]\\nRestart=no\\n' > {DROPIN}"),
               f"write Restart=no on {host}")
    require_ok(run(host, "systemctl daemon-reload"), f"daemon-reload on {host}")
    policy = require_ok(run(host, "systemctl show mariadb -p Restart --value"),
                        f"read effective Restart on {host}")
    if policy.strip() != "no":
        raise EvidenceError(f"effective Restart on {host} is {policy!r}, expected 'no'")
    require_ok(run(host, "pkill -9 -x mariadbd"), f"SIGKILL mariadbd on {host}")
    alive = require_ok(
        run(host, "pgrep -x mariadbd >/dev/null && echo ALIVE || echo DEAD"),
        f"verify mariadbd death on {host}",
    )
    if alive.strip() != "DEAD":
        raise EvidenceError(f"mariadbd still alive on {host}")
    return True


def cleanup_nodes(hosts, restart_before, run=safe_sh):
    results = {}
    for host in hosts:
        remove = run(host, f"rm -f {DROPIN}", timeout=60)
        reload_result = run(host, "systemctl daemon-reload", timeout=60)
        start = run(host, "systemctl start --no-block mariadb", timeout=60)
        absent = run(host, f"test ! -e {DROPIN} && echo ABSENT || echo PRESENT", timeout=60)
        policy = run(host, "systemctl show mariadb -p Restart --value", timeout=60)
        results[host] = {
            "remove_ok": remove["ok"],
            "reload_ok": reload_result["ok"],
            "start_enqueued": start["ok"],
            "dropin_absent": absent["ok"] and absent["output"] == "ABSENT",
            "restart_policy_before": restart_before.get(host),
            "restart_policy_after": policy["output"] if policy["ok"] else None,
            "restart_policy_restored": (
                policy["ok"] and policy["output"] == restart_before.get(host)
            ),
            "errors": [
                item["error"] for item in (remove, reload_result, start, absent, policy)
                if not item["ok"]
            ],
        }
    return results


def new_record():
    acceptance = {name: False for name in LOCAL_ACCEPTANCE}
    acceptance.update({"platform_verify": None, "post_build_gate": None})
    return {
        "run_id": RUN_ID,
        "cluster": CLUSTER_NAME,
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outcome": OUTCOME_UNRESOLVED,
        "contract": {"expected": CONTRACT, "observed": OUTCOME_UNRESOLVED, "match": False},
        "acceptance": acceptance,
        "errors": [],
        "versions": {},
        "topology": {
            "vip": VIP, "app_user": APP_USER, "galera": GALERA_ADDR, "proxysql": PROXYSQL_ADDR,
            "writer_hostgroup": WRITER_HG, "offline_hostgroup": OFFLINE_HG,
        },
        "baseline": {},
        "failure": {},
        "cleanup": {"nodes": {}, "credential_profile_absent": False},
        "recovery": {},
        "external_gates": {
            "platform_verify": {"ok": None, "command": "make platform-verify", "rc": None},
            "post_build_gate": {
                "ok": None,
                "command": "make lab-post-build-gate CLUSTER=newclaude16-r9",
                "rc": None,
            },
        },
    }


def write_artifact(record):
    path = Path(f"/var/tmp/quorum-evidence-{EXPECTED_CLUSTER}-{RUN_ID}.json")
    fd, local_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f"quorum-evidence-{EXPECTED_CLUSTER}-{RUN_ID}-",
        suffix=".tmp",
        text=True,
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True))
        os.replace(local_path, path)
    finally:
        if os.path.exists(local_path):
            try:
                os.unlink(local_path)
            except OSError:
                pass
    return path


def run_measurement(record):
    errors = record["errors"]
    cleanup_hosts = tuple(STOPPED)  # established before the first mutation
    restart_before = {}
    confirmed_dead = []

    # Fail-closed baseline: every item must exist before SIGKILL.
    try:
        record["versions"] = collect_versions()
        baseline_states = {host: galera_state(host) for host in GALERA}
        if not recovery_complete(baseline_states, NODES_EXPECTED):
            raise EvidenceError(f"baseline Galera state is not Primary/3/Synced: {baseline_states}")
        holder = vip_holder()
        runtime_by_proxy = {host: runtime_snapshot(host) for host in PROXYSQL_HOSTS}
        writer_rows = {
            host: [row for row in rows if row["hostgroup_id"] == WRITER_HG and row["status"] == "ONLINE"]
            for host, rows in runtime_by_proxy.items()
        }
        if any(len(rows) != 1 for rows in writer_rows.values()):
            raise EvidenceError(f"baseline writer placement is not exactly one per proxy: {writer_rows}")
        writer_addresses = {rows[0]["hostname"] for rows in writer_rows.values()}
        if len(writer_addresses) != 1 or not writer_addresses.issubset(set(GALERA_ADDR.values())):
            raise EvidenceError(f"ProxySQL pair disagrees on a canonical Galera writer: {writer_rows}")
        for host in cleanup_hosts:
            restart_before[host] = must_output(
                host, "systemctl show mariadb -p Restart --value", "baseline Restart policy", timeout=30
            )
        setup = app_setup()
        if not setup["ok"]:
            raise EvidenceError(f"app table setup failed: {setup['error']}")
        baseline_write = app_write()
        if not baseline_write["ok"]:
            raise EvidenceError(f"baseline app write failed: {baseline_write['error']}")
        record["baseline"] = {
            "vip_holder": holder,
            "galera": baseline_states,
            "runtime_writer": {host: rows[0] for host, rows in writer_rows.items()},
            "restart_policy": restart_before,
            "app_write_ok": True,
        }
        record["acceptance"]["baseline_complete"] = True
    except (EvidenceError, ValueError) as exc:
        errors.append(f"baseline: {exc}")
        return record

    try:
        for host in cleanup_hosts:
            arm_node(host)
            confirmed_dead.append(host)
        record["acceptance"]["processes_dead"] = confirmed_dead == list(cleanup_hosts)

        deadline = time.monotonic() + 120
        survivor_status = "?"
        while time.monotonic() < deadline:
            try:
                survivor_status = galera_state(SURVIVOR).split("/", 1)[0]
            except (EvidenceError, ValueError):
                survivor_status = "?"
            if survivor_status == "non-Primary":
                break
            time.sleep(3)
        if survivor_status != "non-Primary":
            raise EvidenceError(f"survivor did not reach non-Primary: {survivor_status}")
        record["acceptance"]["survivor_non_primary"] = True

        holder_before = vip_holder()
        mark = log_mark(holder_before)
        window_start_us = int(time.time() * 1_000_000)
        window_started = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        app_result = app_write()
        direct_result = safe_sh(
            SURVIVOR,
            "timeout 20 mariadb --socket=/var/lib/mysql/mysql.sock isa_test "
            "-e \"INSERT INTO app_degradation () VALUES ()\" 2>&1",
            timeout=30,
        )

        # Zachowaj oba wyniki ZANIM kolejne zrodlo dowodu zdazy zawiesc.
        app_code, app_state = parse_client_error(app_result["output"] or app_result["error"])
        node_code, node_state = parse_client_error(direct_result["output"] or direct_result["error"])
        outcome = classify_outcome(app_code, app_state, node_code, node_state)
        record["outcome"] = outcome
        record["contract"] = {"expected": CONTRACT, "observed": outcome, "match": outcome == CONTRACT}
        record["acceptance"].update({
            "vip_write_rejected": not app_result["ok"],
            "direct_write_rejected": not direct_result["ok"],
            "backend_error_exact": (node_code, node_state) == ("1047", "08S01"),
            "classification_resolved": outcome in (OUTCOME_DEGRADED, OUTCOME_CLEAN),
        })
        record["failure"] = {
            "window_started_utc": window_started,
            "window_ended_utc": None,
            "window_start_us": window_start_us,
            "survivor": SURVIVOR,
            "stopped": confirmed_dead,
            "survivor_status": survivor_status,
            "app_code": app_code,
            "app_sqlstate": app_state,
            "app_error": app_result["output"] or app_result["error"],
            "node_code": node_code,
            "node_sqlstate": node_state,
            "node_error": direct_result["output"] or direct_result["error"],
            "monitor_row": None,
            "runtime_survivor": None,
            "proxysql_node": holder_before,
            "proxysql_log": None,
            "log_mark": mark,
        }
        if app_result["ok"]:
            errors.append("critical: application write succeeded without quorum")
        if direct_result["ok"]:
            errors.append("critical: direct backend write succeeded without quorum")
        if outcome == OUTCOME_UNRESOLVED:
            errors.append(f"classification unresolved: app={app_code}/{app_state}, backend={node_code}/{node_state}")

        # Monitor moze odswiezac sie po zapytaniu z opoznieniem. Czekamy na
        # pierwsza najnowsza probke, ktorej epoch-us nalezy do TEGO okna.
        monitor = None
        monitor_error = ""
        monitor_deadline = time.monotonic() + 30
        while time.monotonic() < monitor_deadline:
            try:
                candidate = monitor_row(holder_before, GALERA_ADDR[SURVIVOR])
                if candidate["time_start_us"] >= window_start_us:
                    monitor = candidate
                    break
                monitor_error = (
                    f"latest row still predates window: {candidate['time_start_us']} < {window_start_us}"
                )
            except (EvidenceError, ValueError) as exc:
                monitor_error = str(exc)
            time.sleep(1)
        if monitor is None:
            raise EvidenceError(f"no post-window-start monitor row: {monitor_error}")
        if monitor["primary_partition"] != "NO":
            raise EvidenceError(f"monitor row does not prove quorum loss: {monitor}")

        runtime_rows = runtime_snapshot(holder_before)
        survivor_rows = [row for row in runtime_rows if row["hostname"] == GALERA_ADDR[SURVIVOR]]
        if len(survivor_rows) != 1:
            raise EvidenceError(f"expected one runtime row for survivor, got {survivor_rows}")
        runtime_survivor = survivor_rows[0]
        if runtime_survivor["hostgroup_id"] not in (WRITER_HG, OFFLINE_HG):
            raise EvidenceError(f"survivor is outside tenant writer/offline groups: {runtime_survivor}")

        log_text = log_delta(holder_before, mark)
        if not proxy_log_proves_backend_error(log_text, GALERA_ADDR[SURVIVOR]):
            raise EvidenceError("bounded ProxySQL log delta lacks survivor-correlated backend 1047/WSREP")
        holder_after = vip_holder()
        if holder_after != holder_before:
            raise EvidenceError(f"VIP holder changed inside evidence window: {holder_before} -> {holder_after}")

        window_ended = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record["failure"].update({
            "window_ended_utc": window_ended,
            "monitor_row": monitor,
            "runtime_survivor": runtime_survivor,
            "proxysql_log": log_text,
        })
        record["acceptance"].update({
            "same_window_monitor": True,
            "exact_runtime_placement": True,
            "bounded_correlated_log": True,
        })
    except (EvidenceError, ValueError) as exc:
        errors.append(f"measurement: {exc}")
    except Exception as exc:  # noqa: BLE001 — cleanup/recovery precede failure return.
        errors.append(f"unexpected measurement error {type(exc).__name__}: {exc}")
    except BaseException as exc:  # KeyboardInterrupt/SystemExit: najpierw cleanup + recovery.
        errors.append(f"measurement interrupted by {type(exc).__name__}: {exc}")
    finally:
        cleanup = cleanup_nodes(cleanup_hosts, restart_before)
        record["cleanup"]["nodes"] = cleanup
        record["acceptance"]["dropins_absent"] = all(
            item["dropin_absent"] for item in cleanup.values()
        )
        record["acceptance"]["restart_policy_restored"] = all(
            item["restart_policy_restored"] for item in cleanup.values()
        )
        for host, item in cleanup.items():
            errors.extend(f"cleanup {host}: {error}" for error in item["errors"])

    # Recovery readers are fail-soft: unknown state is recorded and retried to deadline.
    deadline = time.monotonic() + 240
    recovered_states = {}
    app_recovered = False
    while time.monotonic() < deadline:
        recovered_states = {}
        for host in GALERA:
            try:
                recovered_states[host] = galera_state(host)
            except (EvidenceError, ValueError) as exc:
                recovered_states[host] = f"UNKNOWN: {exc}"
        if recovery_complete(recovered_states, NODES_EXPECTED):
            app_recovered = app_write()["ok"]
            if app_recovered:
                break
        time.sleep(5)
    record["recovery"] = {"nodes": recovered_states, "app_write_ok": app_recovered}
    record["acceptance"]["nodes_primary_synced"] = recovery_complete(recovered_states, NODES_EXPECTED)
    record["acceptance"]["app_recovered"] = app_recovered
    if not record["acceptance"]["nodes_primary_synced"]:
        errors.append(f"recovery: cluster did not return Primary/3/Synced: {recovered_states}")
    if not app_recovered:
        errors.append("recovery: application write did not resume")
    return record


def main():
    guard_errors = validate_target(
        CLUSTER_NAME,
        CONFIG_PATH,
        INVENTORY,
        GALERA,
        PROXYSQL_HOSTS,
        [APP_HOST] if APP_HOST else [],
    )
    if not APP_PW:
        guard_errors.append("APP_DB_PASSWORD is missing")
    if guard_errors:
        for error in guard_errors:
            print(f"REFUSED: {error}")
        return 1

    record = new_record()
    record["acceptance"]["target_guard"] = True
    copy_attempted = False
    try:
        copy_attempted = True
        install_app_profile()
        record = run_measurement(record)
    except (EvidenceError, OSError, subprocess.TimeoutExpired) as exc:
        record["errors"].append(f"credential/setup: {exc}")
    except Exception as exc:  # noqa: BLE001 — zachowaj artifact po nieoczekiwanym bledzie.
        record["errors"].append(f"unexpected setup/measurement error {type(exc).__name__}: {exc}")
    except BaseException as exc:
        record["errors"].append(f"main interrupted by {type(exc).__name__}: {exc}")
    finally:
        credential_cleanup = remove_app_profile() if copy_attempted else {"absent": True, "history": []}
        record["cleanup"]["credential_profile_absent"] = credential_cleanup["absent"]
        record["cleanup"]["credential_history"] = credential_cleanup["history"]
        record["acceptance"]["credential_profile_absent"] = credential_cleanup["absent"]
        if not credential_cleanup["absent"]:
            record["errors"].append(f"credential profile remains or is unknown on {APP_HOST}")

    artifact = write_artifact(record)  # after credential absence verification
    local_failures = acceptance_failures(record, final=False)
    if local_failures:
        print(f"FAIL: {local_failures}")
        exit_code = 1
    elif not record["contract"]["match"]:
        print(f"CONTRACT_MISMATCH: expected {CONTRACT}, observed {record['outcome']}")
        exit_code = 3
    else:
        print(f"PASS: locally accepted {record['outcome']} measurement")
        exit_code = 0
    print(f"ARTEFAKT: {artifact}")  # final output line, exact current path
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
