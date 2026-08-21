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
    "classify_outcome",
    "datetime",
    "json",
    "parse_client_error",
    "proxy_log_proves_backend_error",
    "recovery_complete",
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
        result = safe_sh(host, f"ip -br addr show | grep -q '{VIP}/' && echo HOLDER || echo NO", timeout=30)
        if not result["ok"]:
            raise EvidenceError(f"VIP probe failed on {host}: {result['error']}")
        if result["output"] == "HOLDER":
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


def node_write_direct(host):
    """Ta sama proba bezposrednio na wezle — dla porownania komunikatow."""
    rc, out = sh(host,
                 "timeout 20 mariadb --socket=/var/lib/mysql/mysql.sock isa_test "
                 "-e \"INSERT INTO app_degradation () VALUES ()\" 2>&1")
    return rc == 0, out.strip()


def cluster_size(host):
    rc, out = sh(host,
                 "timeout 15 mariadb --socket=/var/lib/mysql/mysql.sock -N -B "
                 "-e \"SHOW STATUS LIKE 'wsrep_cluster_status'\" 2>&1")
    return out.split("\t")[-1].strip() if rc == 0 and "\t" in out else "?"


def main():
    if ENVIRONMENT == "production":
        print("REFUSED: chaos-app-degradation jest destrukcyjny i nie moze biec na produkcji (ISC-64)")
        return 1
    if not APP_PW:
        print("FAIL: brak APP_DB_PASSWORD w srodowisku")
        return 1
    if not APP_HOST:
        print("FAIL: inventory nie ma grupy 'app'")
        return 1
    if CONTRACT not in ("degraded", "clean"):
        print(f"FAIL: APP_QUORUM_ERROR_CONTRACT={CONTRACT!r} (dozwolone: degraded, clean)")
        return 1

    failures = []
    ok, out = app_write()
    if not ok:
        print(f"FAIL: aplikacja nie zapisuje JUZ PRZED awaria: {out[:200]}")
        return 1

    stopped_ok = []
    try:
        for host in STOPPED:
            # KOLEJNOSC MA ZNACZENIE: najpierw odbierz systemd prawo wskrzeszenia,
            # dopiero potem zabij. Jednostka MariaDB ma Restart=on-abnormal, a
            # systemd.service(5) mowi wprost, ze ta polityka restartuje usluge
            # "when the process is terminated by a signal (...) excluding
            # SIGHUP, SIGINT, SIGTERM, SIGPIPE" — SIGKILL nie jest wykluczony,
            # wiec wezel wstawal sam. Zmierzone na v10: NRestarts=2 i 3, klaster
            # z powrotem Primary, zanim sonda zdazyla zobaczyc utrate kworum.
            # Na v9 ta sama sonda przeszla, bo wyscig wygrala detekcja Galery —
            # czyli test byl NIEDETERMINISTYCZNY, a nie poprawny.
            #
            # Zdejmujemy wylacznie zmartwychwstanie; sposob smierci zostaje
            # nagly (SIGKILL, bez pozegnania do klastra).
            sh(host, f"mkdir -p {DROPIN_DIR} && printf '[Service]\\nRestart=no\\n' > {DROPIN} "
                     f"&& systemctl daemon-reload && echo armed", timeout=120)
            sh(host, "pkill -9 -x mariadbd; echo killed", timeout=180)
            stopped_ok.append(host)

        # Poprzednia wersja ufala, ze `pkill` zadzialal — a `pkill ...; echo killed`
        # zawsze konczy sie rc=0, wiec brak dopasowania procesu przechodzil cicho.
        still_alive = [h for h in stopped_ok
                       if sh(h, "pgrep -x mariadbd >/dev/null && echo ALIVE || echo DEAD")[1] != "DEAD"]
        if still_alive:
            print(f"FAIL: mariadbd nadal zyje na {still_alive} mimo SIGKILL — "
                  f"test nie wytworzyl awarii, wynik odrzucony")
            return 1
        print(f"zabito {stopped_ok} (bez wskrzeszenia przez systemd); zostal {SURVIVOR}")

        # Provider potrzebuje chwili, zeby stwierdzic utrate kworum.
        deadline = time.time() + 120
        status = "?"
        while time.time() < deadline:
            status = cluster_size(SURVIVOR)
            if status == "non-Primary":
                break
            time.sleep(3)
        print(f"{SURVIVOR}: wsrep_cluster_status={status}")
        if status != "non-Primary":
            # BRAMA POPRAWNOSCI POMIARU, nie asercja produktu. Bez utraty kworum
            # dalsze warunki nie znacza nic: zapis MA przechodzic, a brak bledu
            # nie jest defektem. Pierwsza wersja tej sondy gasila wezly przez
            # `systemctl stop`, czyli LAGODNIE — Galera przelicza wtedy sklad i
            # ocalaly zostaje Primary. Zmierzone: status=Primary, po czym trzy
            # asercje bezpieczenstwa zapalily sie jako falszywe alarmy.
            print(
                f"FAIL: nie udalo sie doprowadzic do utraty kworum (status={status}); "
                f"test nie zmierzyl tego, co mial zmierzyc — wynik odrzucony"
            )
            return 1

        app_ok, app_err = app_write()
        node_ok, node_err = node_write_direct(SURVIVOR)

        # 1. BEZPIECZENSTWO — nadrzedne nad wszystkim innym.
        if app_ok:
            failures.append(
                "aplikacja ZAPISALA przez VIP mimo utraty kworum — utrata danych (ISC-30)")
        if node_ok:
            failures.append(
                f"{SURVIVOR} przyjal zapis bez kworum — naruszenie Primary Component")

        # 2. DIAGNOZOWALNOSC.
        app_code = re.search(r"ERROR (\d+)", app_err)
        node_code = re.search(r"ERROR (\d+)", node_err)
        app_code = app_code.group(1) if app_code else "brak"
        node_code = node_code.group(1) if node_code else "brak"
        print(f"aplikacja przez VIP -> ERROR {app_code}; wezel bezposrednio -> ERROR {node_code}")

        # Bledy protokolu klienta: 2026/2027 mowia "cos zjadlo pakiet", nie "baza
        # odmowila" — pula polaczen nie odrozni tego od awarii sieci.
        protocol_error = app_code in ("2026", "2027")
        if CONTRACT == "clean" and protocol_error:
            failures.append(
                f"aplikacja dostala blad protokolu ERROR {app_code} zamiast bledu bazy "
                f"(oczekiwano 1047/08S01 jak przy polaczeniu bezposrednim): {app_err[:160]}"
            )
        if CONTRACT == "degraded" and not protocol_error:
            failures.append(
                f"aplikacja dostala ERROR {app_code}, a sonda spodziewa sie znanego "
                f"zlamania (bledu protokolu). Jesli routing ProxySQL zostal naprawiony, "
                f"ustaw APP_QUORUM_ERROR_CONTRACT=clean i egzekwuj kontrakt."
            )

    finally:
        # Sprzatanie MUSI dojsc do konca dla KAZDEGO hosta. Poprzednia wersja
        # robila blokujacy `systemctl start`, ktory przy niepelnym skladzie czeka
        # na uformowanie Primary Component; timeout wywalal wyjatek w polowie
        # petli i kolejny wezel zostawal z Restart=no na stale. Zmierzone na v10.
        for host in stopped_ok:
            try:
                # Najpierw oddaj polityke restartu sprzed testu, potem startuj.
                sh(host, f"rm -f {DROPIN} && systemctl daemon-reload && echo disarmed", timeout=120)
                # --no-block: systemctl(1) "it is only verified and enqueued".
                # Start wezla Galera czeka na grupe, wiec synchroniczny start
                # zakleszcza sie z brama powrotu ponizej — ona i tak sprawdza
                # efekt (Primary + udany zapis), wiec czekanie tutaj nic nie wnosi.
                sh(host, "systemctl start --no-block mariadb", timeout=120)
            except subprocess.TimeoutExpired:
                print(f"UWAGA: sprzatanie {host} przekroczylo limit; kontynuuje pozostale")
        # Powrot wezlow to IST; brama ponizej i tak czeka na pelny sklad.
        time.sleep(20)

    # 3. POWROT bez interwencji.
    recovered = False
    deadline = time.time() + 180
    while time.time() < deadline:
        if cluster_size(SURVIVOR) == "Primary":
            ok, out = app_write()
            if ok:
                recovered = True
                break
        time.sleep(5)
    if not recovered:
        failures.append("aplikacja NIE wrocila do dzialania po przywroceniu wezlow")

    if failures:
        print("FAIL: kontrakt aplikacji w awarii naruszony:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"PASS: przy utracie kworum aplikacja nie zapisala (bezpieczenstwo OK), "
        f"dostala ERROR {app_code} przez VIP wobec ERROR {node_code} bezposrednio "
        f"[kontrakt: {CONTRACT}], i wrocila do dzialania po przywroceniu wezlow"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
