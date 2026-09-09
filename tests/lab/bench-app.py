#!/usr/bin/env python3
"""Pomiar przepustowosci Z HOSTA APLIKACYJNEGO (grupa `app`).

DLACZEGO STAD, A NIE Z WEZLA: pierwsze pomiary w tym repo robilem z hosta
`restore`, ktory nalezy do klastra. To zaburza wynik w obie strony — dzieli CPU
i sieciowke z warstwa bazodanowa, a do wezlow ma bliżej niz jakikolwiek klient.
Aplikacja stoi obok, wiec mierzymy stad.

Co porownuje (ta sama praca, rozne sciezki):
  * bezposrednio do aktywnego writera        — koszt samej bazy,
  * przez VIP ProxySQL                       — koszt bazy + przeskok proxy,
  * bezposrednio z TLS i bez TLS             — koszt szyfrowania polaczenia klienta.

To NIE jest bramka jakosci: laboratorium na wspoldzielonym hypervisorze nie daje
powtarzalnosci wymaganej od progu wydajnosci. Skrypt konczy sie bledem tylko
wtedy, gdy nie da sie ZMIERZYC (brak narzedzia, brak polaczenia). Liczby sluza
do porownan miedzy sciezkami w JEDNYM przebiegu, nie miedzy dniami.

Uzywa `mariadb-slap` z pakietu klienta przypietego lockfile'em — zadnych
dodatkowych instalacji na hoscie.

Wymaga APP_DB_PASSWORD. Parametry: BENCH_QUERIES, BENCH_CONCURRENCY, BENCH_ITERATIONS.
"""

import os
import re
import secrets
import subprocess
import sys

import yaml

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/example-cluster/cluster.yml")
INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/example-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")
APP_PW = os.environ.get("APP_DB_PASSWORD", "")
QUERIES = int(os.environ.get("BENCH_QUERIES", "2000"))
CONCURRENCY = int(os.environ.get("BENCH_CONCURRENCY", "8"))
ITERATIONS = int(os.environ.get("BENCH_ITERATIONS", "3"))
ROWS = 5000
# WLASNOSC TABEL POMIAROWYCH (poprawka 2026-09-08). Pierwsza wersja pracowala na
# stalych nazwach `bench_w`/`bench_r` w bazie NAJEMCY: tabeli zapisu nie kasowala
# nigdy, a tabele odczytu kasowala PRZED przebiegiem, nie po nim. Skutek zmierzony
# na cassiopeiav14-r9: 24 270 wierszy `bench_w` zostawionych w `isa_test`, ktore
# wliczyly sie do raportu drilla restore (30505 zamiast 1505) — narzedzie pomiarowe
# falszowalo dowod innej sondy. Teraz kazdy przebieg tworzy WLASNE, unikalnie
# nazwane tabele i kasuje wylacznie je, takze gdy pomiar padnie.
BENCH_ID = secrets.token_hex(4)
TABLE_W = f"bench_w_{BENCH_ID}"
TABLE_R = f"bench_r_{BENCH_ID}"

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER = yaml.safe_load(fh)
with open(INVENTORY, encoding="utf-8") as fh:
    INV = yaml.safe_load(fh)

VIP = CLUSTER["proxysql"]["endpoint"]["address"]
VIP_PORT = CLUSTER["proxysql"]["endpoint"]["port"]
APP_USER = CLUSTER.get("proxysql", {}).get("app_user", "app_user")
CLUSTER_NAME = CLUSTER["cluster"]["name"]
TLS_FULL = (CLUSTER.get("tls") or {}).get("mode", "disabled") == "full"
CA_PATH = f"/etc/mysql/app/{CLUSTER_NAME}/ca.pem"

_app = (INV["all"]["children"].get("app") or {}).get("hosts") or {}
if not _app:
    print("FAIL: inventory nie ma grupy 'app'")
    sys.exit(1)
APP_HOST = next(iter(_app))
GALERA = INV["all"]["children"]["galera"]["hosts"]


def on_app(script, timeout=600):
    cmd = [ANSIBLE, APP_HOST, "-i", INVENTORY, "-m", "ansible.builtin.shell", "-a", script]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = r.stdout
    m = re.search(rf'^{re.escape(APP_HOST)}\s*\|\s*\w+\s*\|\s*rc=(\d+)\s*>>?\s*$', out, re.M)
    if not m:
        return 1, (out + r.stderr).strip()
    return int(m.group(1)), out[m.end():].strip()


def active_writer_address():
    """Adres wezla, do ktorego ProxySQL kieruje zapisy — mierzymy 'direct' do NIEGO."""
    rc, out = on_app(
        f"MYSQL_PWD='{APP_PW}' mariadb -h {VIP} -P {VIP_PORT} -u {APP_USER} "
        f"--ssl-verify-server-cert=0 -N -B -e 'SELECT @@wsrep_node_address' 2>&1"
    )
    addr = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if rc != 0 or not re.fullmatch(r"[0-9.]+", addr):
        return None
    return addr


def slap(host, port, query, extra=""):
    """Zwraca (zapytania/s, surowy_czas) albo (None, komunikat)."""
    cmd = (
        f"MYSQL_PWD='{APP_PW}' mariadb-slap --host={host} --port={port} --user={APP_USER} "
        f"{extra} --create-schema=isa_test --query=\"{query}\" "
        f"--concurrency={CONCURRENCY} --iterations={ITERATIONS} --number-of-queries={QUERIES} 2>&1"
    )
    rc, out = on_app(cmd)
    m = re.search(r"Average number of seconds to run all queries:\s+([0-9.]+)", out)
    if rc != 0 or not m:
        return None, out.strip().splitlines()[-1][:120] if out.strip() else "brak wyniku"
    secs = float(m.group(1))
    return (QUERIES / secs if secs > 0 else 0.0), secs


def main():
    if not APP_PW:
        print("FAIL: brak APP_DB_PASSWORD w srodowisku")
        return 1

    writer = active_writer_address()
    if not writer:
        print(f"FAIL: nie udalo sie ustalic aktywnego writera przez VIP {VIP}:{VIP_PORT}")
        return 1

    # Tabela odczytowa o STALEJ liczbie wierszy. Pierwsza wersja tego pomiaru
    # losowala id w warunku (`WHERE id=FLOOR(1+RAND()*N)`), przez co optymalizator
    # nie mogl uzyc klucza i skanowal cala tabele — porownywalem wtedy ROZMIARY
    # TABEL, nie sciezki sieciowe. Stad staly klucz i staly zbior.
    setup = (
        f"MYSQL_PWD='{APP_PW}' mariadb -h {writer} -P 3306 -u {APP_USER} "
        f"--ssl-verify-server-cert=0 isa_test -e \""
        "SET SESSION max_recursive_iterations = 100000; "
        f"CREATE TABLE {TABLE_W} (id BIGINT AUTO_INCREMENT PRIMARY KEY, v INT NOT NULL); "
        f"CREATE TABLE {TABLE_R} (id INT PRIMARY KEY, v INT NOT NULL); "
        f"INSERT INTO {TABLE_R} (id, v) WITH RECURSIVE s AS "
        f"(SELECT 1 AS n UNION ALL SELECT n + 1 FROM s WHERE n < {ROWS}) SELECT n, n * 7 FROM s;\" 2>&1"
    )
    rc, out = on_app(setup)
    if rc != 0:
        print(f"FAIL: przygotowanie tabel nie powiodlo sie: {out[:200]}")
        return 1

    noverify = "--ssl-verify-server-cert=0"
    write_q = f"INSERT INTO {TABLE_W} (v) VALUES (1)"
    read_q = f"SELECT v FROM {TABLE_R} WHERE id={ROWS // 2}"

    paths = [
        ("direct (plaintext)", writer, 3306, noverify),
        ("direct (TLS)", writer, 3306, f"--ssl {noverify}"),
        ("przez VIP ProxySQL", VIP, VIP_PORT, noverify),
    ]
    if TLS_FULL:
        paths.append(("direct (TLS zweryfikowany)", writer, 3306,
                      f"--ssl-ca={CA_PATH} --ssl-verify-server-cert"))

    print(f"# pomiar z {APP_HOST} | klaster {CLUSTER_NAME} | writer {writer}")
    print(f"# {CONCURRENCY} watkow, {QUERIES} zapytan x {ITERATIONS} iteracje")
    # Lab stoi na wspoldzielonym hypervisorze: rozrzut miedzy kolejnymi przebiegami
    # tej samej sciezki siega kilkunastu procent. Roznice ponizej ~20% traktuj jako
    # szum, nie jako zjawisko — realny sygnal to dopiero rzedy wielkosci w rodzaju
    # kosztu przeskoku przez proxy.
    print("# uwaga: rozrzut labu ~20%; ponizej tego progu roznice sa szumem\n")
    failed = 0
    for label, q, unit in (("ZAPIS", write_q, "insert"), ("ODCZYT", read_q, "select")):
        # Rozgrzewka przed pomiarem. Bez niej PIERWSZA mierzona sciezka placi za
        # zimny buffer pool i zimne polaczenia, a wynik wyglada jak wlasciwosc tej
        # sciezki. Zmierzone: odczyt plaintext wypadal o 35% GORZEJ od TLS tylko
        # dlatego, ze szedl pierwszy — po rozgrzewce roznica znika.
        slap(paths[0][1], paths[0][2], q, paths[0][3])
        print(f"{label} ({unit}):")
        base = None
        for name, host, port, extra in paths:
            qps, info = slap(host, port, q, extra)
            if qps is None:
                print(f"  {name:<28} BLAD: {info}")
                failed += 1
                continue
            if base is None:
                base = qps
            delta = f"{(qps / base - 1) * 100:+.0f}%" if base else ""
            print(f"  {name:<28} {qps:8.0f} q/s  ({info:.3f} s)  {delta}")
        print()

    if failed:
        print(f"FAIL: {failed} sciezek nie dalo sie zmierzyc")
        return 1
    print("OK: pomiar wykonany (liczby porownywalne w obrebie tego przebiegu, nie miedzy dniami)")
    return 0


def cleanup(writer):
    """Kasuje WYLACZNIE tabele tego przebiegu. Cudzych nie tyka."""
    rc, out = on_app(
        f"MYSQL_PWD='{APP_PW}' mariadb -h {writer} -P 3306 -u {APP_USER} "
        f"--ssl-verify-server-cert=0 isa_test -e \""
        f"DROP TABLE IF EXISTS {TABLE_W}; DROP TABLE IF EXISTS {TABLE_R};\" 2>&1",
        timeout=120,
    )
    if rc != 0:
        print(f"UWAGA: nie udalo sie sprzatnac tabel {TABLE_W}/{TABLE_R}: {out[:200]}")
    return rc


def report_foreign_leftovers(writer):
    """Cudze tabele pomiarowe to decyzja operatora, nie automatu.

    Ta sama zasada co przy `gcache_meas`: sonda usuwa tylko to, co sama
    utworzyla. Stale nazwy `bench_w`/`bench_r` sprzed poprawki wlasnosci moga
    lezec w bazie najemcy i zawyzac raport drilla — mowimy o nich glosno,
    zamiast kasowac cudze dane.
    """
    rc, out = on_app(
        f"MYSQL_PWD='{APP_PW}' mariadb -h {writer} -P 3306 -u {APP_USER} "
        f"--ssl-verify-server-cert=0 -N -B isa_test -e \""
        "SELECT GROUP_CONCAT(table_name) FROM information_schema.tables "
        "WHERE table_schema='isa_test' AND table_name IN ('bench_w','bench_r');\" 2>&1",
        timeout=120,
    )
    leftovers = (out or "").strip()
    if rc == 0 and leftovers and leftovers != "NULL":
        print(
            f"UWAGA: w isa_test leza tabele sprzed poprawki wlasnosci: {leftovers}. "
            "Sonda ich NIE kasuje (nie utworzyla ich) — wliczaja sie do raportu "
            "drilla restore, wiec usun je swiadomie."
        )


if __name__ == "__main__":
    _writer = None
    try:
        _writer = active_writer_address()
        sys.exit(main())
    finally:
        # Sprzatanie takze przy bledzie i przerwaniu: to jedyna roznica miedzy
        # narzedziem pomiarowym a zanieczyszczeniem bazy najemcy.
        if _writer:
            cleanup(_writer)
            report_foreign_leftovers(_writer)
