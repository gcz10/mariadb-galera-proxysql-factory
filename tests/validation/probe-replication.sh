#!/usr/bin/env bash
# Sonda: replikacja zapisu przez ProxySQL jest widoczna na innym węźle (ISC-11).
# Uruchomienie z klienta: APP_DB_PASSWORD=... ./tests/validation/probe-replication.sh <vip> <port> <socket_other_node>
# Zapisuje wiersz przez ProxySQL, czyta na innym węźle, weryfikuje.
set -euo pipefail

VIP="${1:?usage: probe-replication.sh <vip> <port> <socket_other_node>}"
PORT="${2:-6033}"
SOCKET="${3:?socket of another galera node required}"
APP_USER="${APP_DB_USER:-app_user}"
APP_PASSWORD="${APP_DB_PASSWORD:?APP_DB_PASSWORD must be set in the environment}"
AUTH_CNF="$(mktemp)"
trap 'rm -f "$AUTH_CNF"' EXIT
chmod 0600 "$AUTH_CNF"
escaped_user="${APP_USER//\\/\\\\}"
escaped_user="${escaped_user//\"/\\\"}"
escaped_password="${APP_PASSWORD//\\/\\\\}"
escaped_password="${escaped_password//\"/\\\"}"
printf '[client]\nuser="%s"\npassword="%s"\n' "$escaped_user" "$escaped_password" >"$AUTH_CNF"

TABLE="isa_replication_test"
MARKER="probe-$(date +%s)-$RANDOM"

# Zapis przez ProxySQL
if ! mariadb --defaults-extra-file="$AUTH_CNF" -h "$VIP" -P "$PORT" -e "
  CREATE DATABASE IF NOT EXISTS isa_test;
  CREATE TABLE IF NOT EXISTS isa_test.${TABLE} (id INT AUTO_INCREMENT PRIMARY KEY, marker VARCHAR(64), ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
  INSERT INTO isa_test.${TABLE} (marker) VALUES ('${MARKER}');
" 2>/dev/null; then
  echo "FAIL: ISC-11 — write through ProxySQL failed"
  exit 1
fi

# Odczyt na innym węźle (z timeout na replikację)
sleep 2
RESULT=$(mariadb --socket="$SOCKET" -N -B -e "SELECT COUNT(*) FROM isa_test.${TABLE} WHERE marker='${MARKER}'" 2>/dev/null || echo "0")

if [ "$RESULT" = "1" ]; then
  echo "PASS: ISC-11 — write through ProxySQL visible on other node"
  # Cleanup
  mariadb --defaults-extra-file="$AUTH_CNF" -h "$VIP" -P "$PORT" -e "DELETE FROM isa_test.${TABLE} WHERE marker='${MARKER}';" 2>/dev/null || true
  exit 0
else
  echo "FAIL: ISC-11 — marker '$MARKER' not found on other node (count=$RESULT)"
  exit 1
fi
