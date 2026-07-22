#!/usr/bin/env bash
# Sonda: brak PK na tabelach użytkownika jest blockerem (ISC-16).
# Uruchomienie na węźle Galera: ./tests/validation/probe-missing-pk.sh [socket]
# PASS: zero tabel bez PK
# FAIL: ≥1 tabela bez PK (deployment blocker)
set -euo pipefail

SOCKET="${1:-/var/lib/mysql/mysql.sock}"

MISSING=$(mariadb --socket="$SOCKET" -N -B -e "
  SELECT t.TABLE_SCHEMA, t.TABLE_NAME
  FROM information_schema.TABLES t
  LEFT JOIN information_schema.STATISTICS s
    ON t.TABLE_SCHEMA = s.TABLE_SCHEMA
    AND t.TABLE_NAME = s.TABLE_NAME
    AND s.NON_UNIQUE = 0
  WHERE t.TABLE_TYPE = 'BASE TABLE'
    AND t.TABLE_SCHEMA NOT IN ('mysql','sys','performance_schema','information_schema')
    AND s.TABLE_NAME IS NULL;
" 2>/dev/null)

COUNT=$(echo -n "$MISSING" | grep -c . || true)

if [ "$COUNT" -eq 0 ]; then
  echo "PASS: ISC-16 — no tables missing primary key"
  exit 0
else
  echo "FAIL: ISC-16 — $COUNT table(s) missing primary key:"
  echo "$MISSING" | head -20
  exit 1
fi
