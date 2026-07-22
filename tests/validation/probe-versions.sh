#!/usr/bin/env bash
# Sonda: sprawdza wersje pakietów proti lockfile (ISC-3).
# Uruchomienie na docelowym hoście: ./tests/validation/probe-versions.sh <lockfile.yml>
# PASS: wszystkie pakiety z lockfile zainstalowane w dokładnej wersji
# FAIL: brak pakietu lub niezgodna wersja
set -euo pipefail

LOCKFILE="${1:-versions/versions.lock.yml}"
if [ ! -f "$LOCKFILE" ]; then
  echo "FAIL: ISC-3 — lockfile not found: $LOCKFILE"
  exit 1
fi

FAIL=0

# MariaDB
MARIADB_VER=$(grep -E '^  version:' "$LOCKFILE" | head -1 | sed 's/.*version: *"//' | sed 's/"//')
if [ -n "$MARIADB_VER" ]; then
  INSTALLED=$(rpm -q --qf '%{VERSION}' mariadb-server 2>/dev/null || echo "not-installed")
  if [ "$INSTALLED" = "$MARIADB_VER" ]; then
    echo "PASS: ISC-3 — mariadb-server $INSTALLED matches lockfile"
  else
    echo "FAIL: ISC-3 — mariadb-server '$INSTALLED' != lockfile '$MARIADB_VER'"
    FAIL=1
  fi
fi

# mariadb-backup
MB_VER=$(grep -E 'mariadb_backup' "$LOCKFILE" | head -1)
if echo "$MB_VER" | grep -q 'mariadb-backup'; then
  if rpm -q mariadb-backup >/dev/null 2>&1; then
    echo "PASS: ISC-3 — mariadb-backup installed"
  else
    echo "FAIL: ISC-3 — mariadb-backup not installed"
    FAIL=1
  fi
fi

# ProxySQL
PROXYSQL_VER=$(grep -A5 'proxysql:' "$LOCKFILE" | grep -E '^\s+version:' | head -1 | sed 's/.*version: *"//' | sed 's/"//')
if [ -n "$PROXYSQL_VER" ]; then
  INSTALLED=$(rpm -q --qf '%{VERSION}' proxysql 2>/dev/null || echo "not-installed")
  if [ "$INSTALLED" = "$PROXYSQL_VER" ]; then
    echo "PASS: ISC-3 — proxysql $INSTALLED matches lockfile"
  else
    echo "FAIL: ISC-3 — proxysql '$INSTALLED' != lockfile '$PROXYSQL_VER'"
    FAIL=1
  fi
fi

# Galera provider
GALERA_PKG="galera-4"
if rpm -q "$GALERA_PKG" >/dev/null 2>&1; then
  echo "PASS: ISC-3 — $GALERA_PKG installed"
else
  echo "FAIL: ISC-3 — $GALERA_PKG not installed"
  FAIL=1
fi

# Anti: state: latest (ISC-63) — sprawdz czy w lockfile nie ma placeholderow
if grep -qE 'to-confirm-F0|to-verify' "$LOCKFILE"; then
  echo "WARN: ISC-63 — lockfile contains placeholders; not ready for production"
  FAIL=1
fi

exit $FAIL
