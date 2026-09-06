#!/usr/bin/env python3
"""ISC-44, wariant WYGASŁY: serwer z przeterminowanym certyfikatem jest odrzucany.

Kryterium mówi o certyfikacie NIEZAUFANYM *lub NIEWAŻNYM*. Zmierzony był tylko
pierwszy wariant (obce CA, 2026-08-14); drugi zostawał hipotezą, przez co ISC-44
stał z `[~]`.

Dlaczego nie po stronie klienta: wygaszenie CA w magazynie zaufania klienta
mierzy zachowanie KLIENTA wobec własnego magazynu, nie reakcję na zły certyfikat
SERWERA. To ta sama pułapka co „próżny zielony" — pomiar wyglądający na dowód,
mierzący co innego.

Dlaczego nie na węźle klastra: podmiana certyfikatu działającego węzła to
mutacja produkcji, a nieudany powrót zostawia węzeł z martwym TLS.

Dlatego: jednorazowy `mariadbd` na hoście grupy `restore`, na loopbacku,
z certyfikatem serwera wystawionym na przeszłość przez to samo CA
(`openssl x509 -not_before/-not_after`). Klient z POPRAWNYM CA musi odrzucić
połączenie. Instancja i klucze znikają w `finally` — także po błędzie.

Kontrola pozytywna jest obowiązkowa: ten sam serwer, ten sam klient, certyfikat
WAŻNY — musi się połączyć. Bez niej „odrzucone" mogłoby znaczyć „serwer nie
wstał", czyli zielone światło bez pomiaru.

SELinux zostaje ENFORCING (ISC-4), więc sonda dopasowuje się do polityki zamiast
ją omijać: katalog roboczy leży pod `/var/lib/` i dostaje kontekst `mysqld_db_t`
(jak `/var/lib/mysql`), a port pochodzi z zakresu JUŻ oznakowanego
`mysqld_port_t` (1186, 3306, 63132-63164 — zmierzone `semanage port -l`).
Pierwsza wersja stawiała serwer w `/var/tmp` na porcie 33071 i `mariadb-install-db`
kończyło się `Permission denied` na `ib_buffer_pool`, bo `mysqld_t` nie pisze po
`/var/tmp`. Dokładanie reguł polityki byłoby trwałą zmianą systemu dla
jednorazowego pomiaru.

Uruchomienie: CLUSTER=<nazwa> python3 tests/lab/probe-tls-expired-cert.py
"""

from __future__ import annotations

import sys

from _probe_common import ProbeContext, check, finish, run_ansible

WORKDIR = "/var/lib/mysql-isc44"
PORT = 63132
SOCKET = f"{WORKDIR}/mysqld.sock"

SETUP = rf"""
set -e
rm -rf {WORKDIR}
mkdir -p {WORKDIR}/data {WORKDIR}/tls
cd {WORKDIR}/tls

openssl req -x509 -newkey rsa:2048 -sha256 -days 2 -nodes \
  -keyout ca-key.pem -out ca.pem -subj "/CN=isc44-throwaway-ca" >/dev/null 2>&1
openssl req -newkey rsa:2048 -sha256 -nodes \
  -keyout server-key.pem -out server.csr -subj "/CN=127.0.0.1" >/dev/null 2>&1

# WAZNY: od wczoraj na dobe. WYGASLY: okno zamkniete przed dwoma dniami.
# Oba podpisane TYM SAMYM CA, wiec jedyna roznica jest data waznosci.
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial \
  -sha256 -out valid-cert.pem \
  -not_before $(date -u -d '-1 day' +%Y%m%d%H%M%SZ) \
  -not_after  $(date -u -d '+1 day' +%Y%m%d%H%M%SZ) >/dev/null 2>&1
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial \
  -sha256 -out expired-cert.pem \
  -not_before $(date -u -d '-10 days' +%Y%m%d%H%M%SZ) \
  -not_after  $(date -u -d '-2 days'  +%Y%m%d%H%M%SZ) >/dev/null 2>&1

test -s valid-cert.pem && test -s expired-cert.pem
echo VALID_NOT_AFTER=$(openssl x509 -in valid-cert.pem -noout -enddate | cut -d= -f2- | tr ' ' '_')
echo EXPIRED_NOT_AFTER=$(openssl x509 -in expired-cert.pem -noout -enddate | cut -d= -f2- | tr ' ' '_')

mariadb-install-db --user=mysql --datadir={WORKDIR}/data \
  --auth-root-authentication-method=socket >/dev/null 2>&1
chown -R mysql:mysql {WORKDIR}
chcon -R -t mysqld_db_t {WORKDIR}
echo SETUP=OK
"""


def run_case(cert: str) -> str:
    """Uruchom serwer z danym certyfikatem, zmierz próbę połączenia, zatrzymaj."""
    return rf"""
set -e
cd {WORKDIR}
cp tls/{cert}-cert.pem tls/serving-cert.pem
chown mysql:mysql tls/serving-cert.pem
chcon -t mysqld_db_t tls/serving-cert.pem
nohup mariadbd --no-defaults --user=mysql --datadir={WORKDIR}/data \
  --socket={SOCKET} --port={PORT} --bind-address=127.0.0.1 \
  --skip-grant-tables \
  --ssl-ca={WORKDIR}/tls/ca.pem \
  --ssl-cert={WORKDIR}/tls/serving-cert.pem \
  --ssl-key={WORKDIR}/tls/server-key.pem \
  --pid-file={WORKDIR}/mysqld.pid >{WORKDIR}/{cert}.log 2>&1 &
# Gotowosc sprawdzamy BEZ TLS (`--skip-ssl`). Pierwsza wersja pingowala
# domyslnym klientem, ktory negocjuje TLS takze po gniezdzie unixowym — przy
# wygaslym certyfikacie ping konczyl sie `TLS/SSL error: certificate has
# expired`, a sonda brala to za "serwer nie wstal" i konczyla UNDETERMINED.
# Odrzucenie certyfikatu to WLASNIE mierzone zjawisko; brama gotowosci nie
# moze go mylic z awaria startu.
for _ in $(seq 1 60); do
  mariadb-admin --skip-ssl --socket={SOCKET} ping >/dev/null 2>&1 && break
  sleep 1
done
if ! mariadb-admin --skip-ssl --socket={SOCKET} ping >/dev/null 2>&1; then
  echo SERVER=DOWN
  echo "LOG=$(tail -3 {WORKDIR}/{cert}.log | tr '\n' ' ' | cut -c1-300)"
  exit 0
fi
echo SERVER=UP

# Klient z POPRAWNYM CA i weryfikacja nazwy — dokladnie tak, jak laczy sie
# aplikacja przy tls.mode=full.
out=$(mariadb --protocol=TCP -h 127.0.0.1 -P {PORT} -u root \
  --ssl-ca={WORKDIR}/tls/ca.pem --ssl-verify-server-cert \
  -N -B -e "SELECT 'CONNECTED'" 2>&1) && rc=0 || rc=$?
echo "RC=$rc"
echo "OUT=$(echo "$out" | tr '\n' ' ' | cut -c1-200)"

mariadb-admin --skip-ssl --socket={SOCKET} shutdown >/dev/null 2>&1 || true
for _ in $(seq 1 30); do pgrep -x mariadbd >/dev/null || break; sleep 1; done
pgrep -x mariadbd >/dev/null && {{ pkill -x mariadbd; sleep 2; }} || true
echo STOPPED=$(pgrep -xc mariadbd || echo 0)
"""


# `pkill -x` po DOKLADNEJ nazwie procesu, nie `-f` po wzorcu: wzorzec z numerem
# portu pasowal takze do wlasnej linii polecen powloki i sonda zabijala sama
# siebie (rc=-15). Host `restore` nie prowadzi wlasnego serwera bazy, wiec
# jedyny `mariadbd` to ten postawiony wyzej.
CLEANUP = rf"""
pkill -x mariadbd 2>/dev/null || true
sleep 2
pkill -9 -x mariadbd 2>/dev/null || true
rm -rf {WORKDIR}
echo LEFTOVER_PROC=$(pgrep -xc mariadbd || echo 0)
echo CLEANED=$(test -d {WORKDIR} && echo NO || echo YES)
"""


def parse(body: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in body.splitlines():
        key, _, value = line.strip().partition("=")
        if key and value:
            values.setdefault(key, value)
    return values


def main() -> int:
    failures: list[str] = []
    undetermined: list[str] = []
    ctx = ProbeContext()

    hosts = ctx.group_hosts("restore")
    if not hosts:
        undetermined.append("ISC-44: brak hosta w grupie 'restore' — nie ma gdzie postawić serwera")
        return finish(failures, undetermined, "")
    host = hosts[0]
    dates: dict[str, str] = {}

    try:
        setup = run_ansible(ctx, host, SETUP, timeout=300)
        if host in setup.errors or "SETUP=OK" not in setup.body(host):
            undetermined.append(
                f"ISC-44: przygotowanie na {host} nie powiodło się: "
                f"{(setup.errors.get(host) or setup.body(host) or 'brak wyjścia')[:300]}"
            )
            return finish(failures, undetermined, "")
        dates = parse(setup.body(host))

        results: dict[str, dict[str, str]] = {}
        for cert in ("valid", "expired"):
            res = run_ansible(ctx, host, run_case(cert), timeout=300)
            if host in res.errors:
                undetermined.append(f"ISC-44 ({cert}): {res.errors[host][:300]}")
                return finish(failures, undetermined, "")
            values = parse(res.body(host))
            if values.get("SERVER") != "UP":
                undetermined.append(
                    f"ISC-44 ({cert}): serwer testowy nie wstał — nie zmierzono niczego "
                    f"({values.get('LOG', '')})"
                )
                return finish(failures, undetermined, "")
            results[cert] = values

        # Kontrola pozytywna: bez niej „odrzucone" nie dowodzi niczego.
        check(
            results["valid"].get("RC") == "0",
            f"ISC-44: klient NIE połączył się z certyfikatem WAŻNYM "
            f"(rc={results['valid'].get('RC')}, {results['valid'].get('OUT', '')}) — "
            "pomiar wariantu wygasłego byłby bezwartościowy",
            failures,
        )
        # Właściwe kryterium.
        check(
            results["expired"].get("RC") not in (None, "0"),
            "ISC-44: klient POŁĄCZYŁ się z serwerem prezentującym certyfikat WYGASŁY "
            f"({results['expired'].get('OUT', '')}) — nieważny certyfikat nie jest odrzucany",
            failures,
        )
        if results["expired"].get("RC") not in (None, "0"):
            print(f"    odrzucenie: {results['expired'].get('OUT', '')}")
    finally:
        cleanup = run_ansible(ctx, host, CLEANUP, timeout=180)
        body = cleanup.body(host)
        if "CLEANED=YES" not in body or "LEFTOVER_PROC=0" not in body:
            failures.append(
                f"ISC-44: sprzątanie na {host} nie potwierdzone ({body[:200]}) — "
                f"sprawdź {WORKDIR} i procesy mariadbd"
            )

    return finish(
        failures,
        undetermined,
        "ISC-44 (wariant wygasły) — serwer z certyfikatem ważnym do "
        f"{dates.get('VALID_NOT_AFTER', '?')} przyjmuje połączenie, a z certyfikatem "
        f"wygasłym {dates.get('EXPIRED_NOT_AFTER', '?')} zostaje odrzucony przez klienta "
        f"z poprawnym CA (host {host}, serwer jednorazowy, sprzątnięty)",
    )


if __name__ == "__main__":
    sys.exit(main())
