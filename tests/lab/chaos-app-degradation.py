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

Punkt 2 jest dzis ZLAMANY i nie da sie tego naprawic w tym repo — decyduje o tym
routing ProxySQL, ktory trzyma wezel bez `primary_partition` w hostgrupie writera
(pilnuje tego juz probe-proxysql.py). Dlatego oczekiwanie jest STEROWANE flaga
APP_QUORUM_ERROR_CONTRACT:
  * "degraded" (domyslnie) — wiemy o zlamaniu; sonda pada, jesli stan sie zmieni
    W DOWOLNA STRONE, zeby naprawa nie przeszla niezauwazona,
  * "clean" — wymagamy bledu bazodanowego (1047/08S01 lub czysty blad polaczenia).

Wymaga APP_DB_PASSWORD. Odmawia uruchomienia na profilu produkcyjnym (ISC-64).
"""

import os
import re
import subprocess
import sys
import time
import yaml

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


def sh(host, script, timeout=120):
    cmd = [ANSIBLE, host, "-i", INVENTORY, "-m", "ansible.builtin.shell", "-a", script]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = r.stdout
    m = re.search(rf'^{re.escape(host)}\s*\|\s*\w+\s*\|\s*rc=(\d+)\s*>>?\s*$', out, re.M)
    if not m:
        return 1, (out + r.stderr).strip()
    return int(m.group(1)), out[m.end():].strip()


def app_write():
    """Proba zapisu aplikacji przez VIP. Zwraca (udalo_sie, tresc_bledu)."""
    sql = "CREATE TABLE IF NOT EXISTS app_degradation (id BIGINT AUTO_INCREMENT PRIMARY KEY, " \
          "ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP); INSERT INTO app_degradation () VALUES ()"
    rc, out = sh(APP_HOST,
                 f"MYSQL_PWD='{APP_PW}' timeout 25 mariadb -h {VIP} -P {VIP_PORT} -u {APP_USER} "
                 f"--ssl-verify-server-cert=0 --connect-timeout=5 isa_test -e \"{sql}\" 2>&1")
    return rc == 0, out.strip()


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
