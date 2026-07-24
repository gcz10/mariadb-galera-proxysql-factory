#!/bin/bash
# ISC-44: w trybie tls.mode=full połączenie z niezaufanym/nieważnym certyfikatem
# jest odrzucane (a z zaufanym — akceptowane). Uruchom NA węźle z klientem mariadb.
#
# Uruchomienie:
#   probe-tls.sh <host> <user> <password> <trusted_ca_path>
# np. przez ansible:
#   ansible gnode1 -m script -a "tests/validation/probe-tls.sh 172.28.0.11 app_user $PW /etc/mysql/tls/ca.pem"
#
# PASS gdy: zaufane CA => połączenie OK, niezaufane CA => połączenie odrzucone.
set -u

HOST="${1:?usage: probe-tls.sh <host> <user> <password> <trusted_ca_path>}"
DBUSER="${2:?user}"
DBPASS="${3:?password}"
TRUSTED_CA="${4:?trusted_ca_path}"

# 1) zaufane CA + weryfikacja certu serwera => musi się połączyć
if ! mariadb -h"$HOST" -u"$DBUSER" -p"$DBPASS" \
       --ssl-verify-server-cert --ssl-ca="$TRUSTED_CA" \
       -N -B -e "SELECT 1" >/dev/null 2>&1; then
  echo "FAIL: ISC-44 — połączenie z ZAUFANYM certem odrzucone (TLS niesprawny na $HOST)"
  exit 1
fi

# 2) niezaufane CA + weryfikacja => musi zostać odrzucone
WRONG_CA="$(mktemp)"
openssl req -new -x509 -nodes -days 1 -newkey rsa:2048 \
  -keyout /dev/null -out "$WRONG_CA" -subj '/CN=isa-untrusted' >/dev/null 2>&1
if mariadb -h"$HOST" -u"$DBUSER" -p"$DBPASS" \
     --ssl-verify-server-cert --ssl-ca="$WRONG_CA" \
     -N -B -e "SELECT 1" >/dev/null 2>&1; then
  rm -f "$WRONG_CA"
  echo "FAIL: ISC-44 — połączenie z NIEZAUFANYM certem ZAAKCEPTOWANE (cert nieweryfikowany)"
  exit 1
fi
rm -f "$WRONG_CA"

echo "PASS: ISC-44 — tls.mode=full: zaufany cert akceptowany, niezaufany odrzucony ($HOST)"
