#!/usr/bin/env python3
"""Kontrakt widziany przez APLIKACJE — sprawdzany z hosta aplikacyjnego (grupa `app`).

Wszystkie pozostale sondy patrza na klaster albo z hosta kontrolnego, albo z
samych wezlow Galery. Ta jedna laczy sie tak, jak zrobilaby to aplikacja: po
sieci, przez VIP ProxySQL, klientem w wersji z lockfile'a. Powstala, bo zdrowy
klaster nie znaczy dzialajaca aplikacja — dwa realne defekty tej klasy znalazly
sie w tej flocie przypadkiem, przy benchmarku:

  * klient MariaDB 11.4 z DOMYSLNA weryfikacja certu nie laczy sie przez VIP
    (ProxySQL serwuje auto-cert). Release notes 11.4: "Clients now require SSL
    and have server certificate verification enabled by default".
  * przy utracie kworum aplikacja dostaje "ERROR 2027 malformed packet" zamiast
    czystego "ERROR 1047 (08S01)", ktory zwraca ten sam wezel bezposrednio.

Sprawdza (stan ustalony, bez destrukcji):
  1. polaczenie przez VIP jest SZYFROWANE (brak cichego zejscia do plaintextu),
  2. read-your-writes przez proxy na NOWYM polaczeniu,
  3. semantyka transakcji (ROLLBACK cofa, COMMIT utrwala),
  4. kolejne polaczenia trafiaja do JEDNEGO writera,
  5. zweryfikowany TLS (--ssl-ca + --ssl-verify-server-cert) dziala BEZPOSREDNIO
     do wezla — czyli CA klastra faktycznie da sie uzyc po stronie aplikacji,
  6. zweryfikowany TLS przez VIP — porownanie ze SPODZIEWANYM stanem
     (APP_VIP_VERIFIED_TLS): rozbieznosc w KAZDA strone jest bledem.

Wymaga APP_DB_PASSWORD w srodowisku.
"""

import os
import re
import subprocess
import sys
import yaml

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")
APP_PW = os.environ.get("APP_DB_PASSWORD", "")
# Stan zweryfikowanego TLS przez VIP. Dzis 'fail': ProxySQL serwuje wlasny
# auto-cert, ktorego nasze CA nie podpisalo. Gdy frontend dostanie certyfikat z
# CA klastra, przestawienie na 'pass' zamienia dzisiejsza luke w egzekwowany
# kontrakt. Sonda pilnuje OBU kierunkow: ciche "naprawilo sie samo" tez jest
# rozbieznoscia wymagajaca decyzji.
VIP_VERIFIED_TLS = os.environ.get("APP_VIP_VERIFIED_TLS", "fail")

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER = yaml.safe_load(fh)
with open(INVENTORY, encoding="utf-8") as fh:
    INV = yaml.safe_load(fh)

VIP = CLUSTER["proxysql"]["endpoint"]["address"]
VIP_PORT = CLUSTER["proxysql"]["endpoint"]["port"]
APP_USER = CLUSTER.get("proxysql", {}).get("app_user", "app_user")
CLUSTER_NAME = CLUSTER["cluster"]["name"]
CA_PATH = f"/etc/mysql/app/{CLUSTER_NAME}/ca.pem"
TLS_FULL = (CLUSTER.get("tls") or {}).get("mode", "disabled") == "full"

_galera = INV["all"]["children"]["galera"]["hosts"]
FIRST_NODE_ADDR = next(iter(_galera.values()))["ansible_host"]
_app = (INV["all"]["children"].get("app") or {}).get("hosts") or {}
if not _app:
    print("FAIL: inventory nie ma grupy 'app' — sonda wymaga hosta aplikacyjnego")
    sys.exit(1)
APP_HOST = next(iter(_app))


def on_app(script, timeout=90):
    """Uruchom snippet na hoscie aplikacyjnym; zwroc (rc, tresc)."""
    cmd = [
        ANSIBLE, APP_HOST, "-i", INVENTORY, "-m", "ansible.builtin.shell",
        "-a", script,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = r.stdout
    m = re.search(rf'^{re.escape(APP_HOST)}\s*\|\s*\w+\s*\|\s*rc=(\d+)\s*>>?\s*$', out, re.M)
    if not m:
        return 1, (out + r.stderr).strip()
    return int(m.group(1)), out[m.end():].strip()


def mariadb(sql, host, port, extra="", database="isa_test"):
    """Polecenie klienta uruchamiane jako aplikacja (haslo przez MYSQL_PWD, nie argv)."""
    return (
        f"MYSQL_PWD='{APP_PW}' mariadb -h {host} -P {port} -u {APP_USER} {extra} "
        f"{database} -N -B -e \"{sql}\" 2>&1"
    )


def main():
    if not APP_PW:
        print("FAIL: brak APP_DB_PASSWORD w srodowisku")
        return 1
    if VIP_VERIFIED_TLS not in ("pass", "fail"):
        print(f"FAIL: APP_VIP_VERIFIED_TLS={VIP_VERIFIED_TLS!r} (dozwolone: pass, fail)")
        return 1

    failures = []
    # Klient 11.4 domyslnie weryfikuje certyfikat serwera, wiec sciezki, ktore
    # NIE testuja weryfikacji, musza ja jawnie wylaczyc — inaczej mierzylibysmy
    # zaufanie, a nie kontrakt aplikacyjny.
    noverify = "--ssl-verify-server-cert=0"

    # 1a. Przeskok APLIKACJA -> ProxySQL. Mierzony po stronie KLIENTA (`\s`), bo
    #     to jedyne miejsce, ktore opisuje wlasnie to polaczenie.
    #
    #     UWAGA na pulapke, w ktora ta sonda sama wpadla: `SHOW STATUS LIKE
    #     'Ssl_cipher'` puszczone przez proxy wraca z BACKENDU, wiec opisuje
    #     przeskok ProxySQL -> baza, a nie ruch aplikacji. Zmierzone: dla klastra
    #     z tls.mode=disabled to zapytanie zwracalo puste pole, choc polaczenie
    #     klienta bylo szyfrowane (TLS_AES_256_GCM_SHA384). Dwa rozne przeskoki,
    #     dwa rozne warunki.
    rc, out = on_app(mariadb("\\s", VIP, VIP_PORT, noverify).replace("-N -B ", ""))
    m_cipher = re.search(r"Cipher in use is ([^\s,]+)", out)
    cipher = m_cipher.group(1) if m_cipher else ""
    if rc != 0:
        failures.append(f"nie udalo sie polaczyc przez VIP {VIP}:{VIP_PORT}: {out[:160]}")
    elif not cipher:
        failures.append(
            f"ruch aplikacji do VIP {VIP}:{VIP_PORT} jest NIESZYFROWANY "
            f"(klient nie zglasza szyfru)"
        )

    # 1b. Przeskok ProxySQL -> baza. Szyfrowany MUSI byc tylko wtedy, gdy klaster
    #     deklaruje tls.mode=full (f7 ustawia wtedy mysql_servers.use_ssl=1).
    #     Przy 'disabled' plaintext jest zgodny z deklaracja — ale operator
    #     powinien to zobaczyc w wyniku, a nie zakladac.
    rc_b, out_b = on_app(mariadb("SHOW STATUS LIKE 'Ssl_cipher'", VIP, VIP_PORT, noverify))
    backend_cipher = out_b.split("\t")[-1].strip() if "\t" in out_b else ""
    if TLS_FULL and rc_b == 0 and not backend_cipher:
        failures.append(
            "przeskok ProxySQL -> baza jest NIESZYFROWANY mimo tls.mode=full "
            "(sprawdz mysql_servers.use_ssl dla hostgrup tego klastra)"
        )
    backend_note = (
        f"backend {backend_cipher}" if backend_cipher
        else "backend plaintext (zgodnie z tls.mode=" + str((CLUSTER.get("tls") or {}).get("mode", "disabled")) + ")"
    )

    # 2. Read-your-writes przez proxy, na NOWYM polaczeniu (inne moze trafic gdzie indziej).
    marker = os.urandom(4).hex()
    rc_w, out_w = on_app(mariadb(
        "CREATE TABLE IF NOT EXISTS app_conformance (id BIGINT AUTO_INCREMENT PRIMARY KEY, "
        f"marker VARCHAR(32) NOT NULL); INSERT INTO app_conformance (marker) VALUES ('{marker}')",
        VIP, VIP_PORT, noverify))
    rc_r, out_r = on_app(mariadb(
        f"SELECT COUNT(*) FROM app_conformance WHERE marker='{marker}'", VIP, VIP_PORT, noverify))
    if rc_w != 0:
        failures.append(f"zapis przez VIP nie powiodl sie: {out_w[:160]}")
    elif rc_r != 0 or out_r.strip() != "1":
        failures.append(
            f"read-your-writes przez VIP zawiodlo: zapisany znacznik {marker} nie jest "
            f"widoczny na nowym polaczeniu (odczyt: {out_r[:80]!r})"
        )

    # 3. Semantyka transakcji: ROLLBACK cofa, COMMIT utrwala.
    rb = os.urandom(4).hex()
    on_app(mariadb(
        f"BEGIN; INSERT INTO app_conformance (marker) VALUES ('{rb}'); ROLLBACK",
        VIP, VIP_PORT, noverify))
    rc_rb, out_rb = on_app(mariadb(
        f"SELECT COUNT(*) FROM app_conformance WHERE marker='{rb}'", VIP, VIP_PORT, noverify))
    if rc_rb != 0 or out_rb.strip() != "0":
        failures.append(f"ROLLBACK nie cofnal zapisu (widocznych wierszy: {out_rb[:40]!r})")

    cm = os.urandom(4).hex()
    on_app(mariadb(
        f"BEGIN; INSERT INTO app_conformance (marker) VALUES ('{cm}'); COMMIT",
        VIP, VIP_PORT, noverify))
    rc_cm, out_cm = on_app(mariadb(
        f"SELECT COUNT(*) FROM app_conformance WHERE marker='{cm}'", VIP, VIP_PORT, noverify))
    if rc_cm != 0 or out_cm.strip() != "1":
        failures.append(f"COMMIT nie utrwalil zapisu (widocznych wierszy: {out_cm[:40]!r})")

    # 4. Kolejne polaczenia aplikacji trafiaja do JEDNEGO writera.
    rc_h, out_h = on_app(
        "for i in 1 2 3 4 5; do " + mariadb("SELECT @@hostname", VIP, VIP_PORT, noverify) + "; done"
    )
    hosts_seen = {ln.strip() for ln in out_h.splitlines() if ln.strip() and " " not in ln.strip()}
    if rc_h != 0 or not hosts_seen:
        failures.append(f"nie udalo sie ustalic writera przez VIP: {out_h[:160]}")
    elif len(hosts_seen) > 1:
        failures.append(
            f"kolejne polaczenia przez VIP trafily do ROZNYCH wezlow {sorted(hosts_seen)} "
            f"— kontrakt jednego writera zlamany"
        )

    verified_note = "TLS nieweryfikowany (tls.mode != full)"
    if TLS_FULL:
        # 5. Zweryfikowany TLS BEZPOSREDNIO do wezla: dowod, ze CA klastra jest
        #    uzywalne po stronie aplikacji (a nie tylko lezy na dysku).
        rc_d, out_d = on_app(mariadb(
            "SELECT 1", FIRST_NODE_ADDR, 3306,
            f"--ssl-ca={CA_PATH} --ssl-verify-server-cert"))
        if rc_d != 0:
            failures.append(
                f"zweryfikowany TLS do wezla {FIRST_NODE_ADDR} NIE dziala mimo CA w "
                f"{CA_PATH}: {out_d[:160]} — aplikacja nie ma jak ufac bazie"
            )

        # 6. Zweryfikowany TLS przez VIP vs stan spodziewany.
        rc_v, out_v = on_app(mariadb(
            "SELECT 1", VIP, VIP_PORT, f"--ssl-ca={CA_PATH} --ssl-verify-server-cert"))
        works = rc_v == 0
        if works and VIP_VERIFIED_TLS == "fail":
            failures.append(
                "zweryfikowany TLS przez VIP DZIALA, a sonda spodziewa sie awarii — "
                "jesli frontend ProxySQL dostal certyfikat z CA klastra, ustaw "
                "APP_VIP_VERIFIED_TLS=pass i egzekwuj ten kontrakt"
            )
        elif not works and VIP_VERIFIED_TLS == "pass":
            failures.append(
                f"zweryfikowany TLS przez VIP NIE dziala, a mial dzialac: {out_v[:160]}"
            )
        verified_note = (
            "zweryfikowany TLS: wezel=OK, VIP="
            + ("OK" if works else "odrzucony (auto-cert ProxySQL, stan znany)")
        )

    if failures:
        print("FAIL: kontrakt aplikacyjny naruszony:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"PASS: kontrakt aplikacyjny OK z {APP_HOST} — app->VIP {cipher}, {backend_note}, "
        f"read-your-writes, ROLLBACK/COMMIT, jeden writer ({next(iter(hosts_seen))}); "
        f"{verified_note}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
