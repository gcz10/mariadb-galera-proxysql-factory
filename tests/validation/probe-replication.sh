#!/usr/bin/env bash
# Sonda: replikacja zapisu przez ProxySQL jest widoczna na innym węźle (ISC-11).
# Uruchomienie z klienta: ./tests/validation/probe-replication.sh <vip> <port> <socket_other_node>
# Zapisuje wiersz przez ProxySQL, czyta na innym węźle, weryfikuje.
set -euo pipefail

VIP="${1:?usage: probe-replication.sh <vip> <port> <socket_other_node>}"
PORT="${2:-6033}"
SOCKET="${3:?socket of another galera node required}"

TABLE="isa_replication_test"
MARKER="probe-$(date +%s)-$RANDOM"

# Zapis przez ProxySQL
mariadb -h "$VIP" -P "$PORT" -e "
  CREATE DATABASE IF NOT EXISTS isa_test;
  CREATE TABLE IF NOT EXISTS isa_test.${TABLE} (id INT AUTO_INCREMENT PRIMARY KEY, marker VARCHAR(64), ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
  INSERT INTO isa_test.${TABLE} (marker) VALUES ('${MARKER}');
" 2>/dev/null

if [ $? -ne 0 ]; then
  echo "FAIL: ISC-11 — write through ProxySQL failed"
  exit 1
fi

# Odczyt na innym węźle (z timeout na replikację)
sleep 2
RESULT=$(mariadb --socket="$SOCKET" -N -B -e "SELECT COUNT(*) FROM isa_test.${TABLE} WHERE marker='${MARKER}'" 2>/dev/null || echo "0")

if [ "$RESULT" = "1" ]; then
  echo "PASS: ISC-11 — write through ProxySQL visible on other node"
  # Cleanup
  mariadb -h "$VIP" -P "$PORT" -e "DELETE FROM isa_test.${TABLE} WHERE marker='${MARKER}';" 2>/dev/null || true
  exit 0
else
  echo "FAIL: ISC-11 — marker '$MARKER' not found on other node (count=$RESULT)"
  exit 1
fi
