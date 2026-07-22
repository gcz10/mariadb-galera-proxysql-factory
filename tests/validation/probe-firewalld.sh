#!/usr/bin/env bash
# Sonda: sprawdza czy firewalld działa i dopuszcza tylko zadeklarowany ruch (ISC-5).
# Uruchomienie na docelowym hoście: ./tests/validation/probe-firewalld.sh
# PASS: firewalld running, tylko zadeklarowane porty otwarte
# Argumenty: $1 = lista dozwolonych portów (np. "3306/tcp 4567/tcp 4568/tcp 4567/udp 6033/tcp 6032/tcp")
set -euo pipefail

ALLOWED_PORTS="${1:-}"

STATE=$(firewall-cmd --state 2>/dev/null || echo "not-running")
if [ "$STATE" != "running" ]; then
  echo "FAIL: ISC-5 — firewalld is '$STATE', expected running"
  exit 1
fi

# Pobierz otwarte porty ze wszystkich stref
OPEN_PORTS=$(firewall-cmd --list-all-zones 2>/dev/null | grep -oE 'ports:.*' | tr ' ' '\n' | grep -E '^[0-9]+/' | sort -u)

if [ -z "$ALLOWED_PORTS" ]; then
  echo "PASS: ISC-5 — firewalld running (allowed ports not specified, skipping port check)"
  exit 0
fi

# Sprawdź czy wszystkie otwarte porty są na liście dozwolonych
FAIL=0
for port in $OPEN_PORTS; do
  if ! echo "$ALLOWED_PORTS" | grep -qw "$port"; then
    echo "FAIL: ISC-5 — unexpected open port: $port"
    FAIL=1
  fi
done

if [ "$FAIL" -eq 0 ]; then
  echo "PASS: ISC-5 — firewalld running, only declared ports open"
  exit 0
else
  exit 1
fi
