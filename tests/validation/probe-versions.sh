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
  INSTALLED=$(rpm -q --qf '%{VERSION}' MariaDB-server 2>/dev/null || echo "not-installed")
  if [ "$INSTALLED" = "$MARIADB_VER" ]; then
    echo "PASS: ISC-3 — MariaDB-server $INSTALLED matches lockfile"
  else
    echo "FAIL: ISC-3 — MariaDB-server '$INSTALLED' != lockfile '$MARIADB_VER'"
    FAIL=1
  fi
fi

# mariadb-backup
# audit#12: oficjalne repo instaluje MariaDB-backup (wielkie M); wcześniej sprawdzano
# mariadb-backup → rpm -q nigdy nie trafiał i test dawał fałszywy sygnał.
MB_PKG=$(grep -E 'mariadb_backup_package:' "$LOCKFILE" | head -1 | sed 's/.*: *"//' | sed 's/".*//')
MB_PKG=${MB_PKG:-MariaDB-backup}
if rpm -q "$MB_PKG" >/dev/null 2>&1; then
  echo "PASS: ISC-3 — $MB_PKG installed"
else
  echo "FAIL: ISC-3 — $MB_PKG not installed"
  FAIL=1
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
