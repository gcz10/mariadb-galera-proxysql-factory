#!/bin/bash
# ISC-44: w trybie tls.mode=full połączenie z niezaufanym/nieważnym certyfikatem
# jest odrzucane (a z zaufanym — akceptowane). Uruchom NA węźle z klientem mariadb.
#
#   probe-tls.sh <host> <user> <trusted_ca_path>   # hasło: cichy prompt lub DB_PASSWORD z secret store
#
# PASS gdy: zaufane CA => połączenie OK, niezaufane CA => połączenie odrzucone.
set -euo pipefail

HOST="${1:?usage: probe-tls.sh <host> <user> <trusted_ca_path>}"
DBUSER="${2:?user}"
TRUSTED_CA="${3:?trusted_ca_path}"
if [[ -n "${DB_PASSWORD:-}" ]]; then
  DBPASS="$DB_PASSWORD"
elif [[ -t 0 ]]; then
  read -r -s -p "Database password: " DBPASS
  printf '\n' >&2
else
  echo "DB_PASSWORD must be provided by a secret store in non-interactive mode" >&2
  exit 2
fi
AUTH_CNF="$(mktemp)"
WRONG_CA="$(mktemp)"
trap 'rm -f "$AUTH_CNF" "$WRONG_CA"' EXIT
chmod 0600 "$AUTH_CNF"
escaped_user="${DBUSER//\\/\\\\}"
escaped_user="${escaped_user//\"/\\\"}"
escaped_password="${DBPASS//\\/\\\\}"
escaped_password="${escaped_password//\"/\\\"}"
printf '[client]\nuser="%s"\npassword="%s"\n' "$escaped_user" "$escaped_password" >"$AUTH_CNF"

# 1) zaufane CA + weryfikacja certu serwera => musi się połączyć
if ! mariadb --defaults-extra-file="$AUTH_CNF" -h"$HOST" \
       --ssl-verify-server-cert --ssl-ca="$TRUSTED_CA" \
       -N -B -e "SELECT 1" >/dev/null 2>&1; then
  echo "FAIL: ISC-44 — połączenie z ZAUFANYM certem odrzucone (TLS niesprawny na $HOST)"
  exit 1
fi

# 2) niezaufane CA + weryfikacja => musi zostać odrzucone
openssl req -new -x509 -nodes -days 1 -newkey rsa:2048 \
  -keyout /dev/null -out "$WRONG_CA" -subj '/CN=isa-untrusted' >/dev/null 2>&1
if mariadb --defaults-extra-file="$AUTH_CNF" -h"$HOST" \
     --ssl-verify-server-cert --ssl-ca="$WRONG_CA" \
     -N -B -e "SELECT 1" >/dev/null 2>&1; then
  echo "FAIL: ISC-44 — połączenie z NIEZAUFANYM certem ZAAKCEPTOWANE (cert nieweryfikowany)"
  exit 1
fi

echo "PASS: ISC-44 — tls.mode=full: zaufany cert akceptowany, niezaufany odrzucony ($HOST)"
