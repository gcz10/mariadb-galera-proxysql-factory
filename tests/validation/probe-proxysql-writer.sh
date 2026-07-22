#!/usr/bin/env bash
# Sonda: ProxySQL writer count i routing (ISC-18, ISC-19, ISC-23).
# Uruchomienie na hoście ProxySQL: ./tests/validation/probe-proxysql-writer.sh [admin_port]
# PASS: dokładnie jeden ONLINE writer, read_write_split OFF
set -euo pipefail

PORT="${1:-6032}"
ADMIN_USER="${PROXYSQL_ADMIN_USER:-admin}"
ADMIN_PASS="${PROXYSQL_ADMIN_PASS:-admin}"

# ISC-18: dokładnie jeden aktywny writer
WRITERS=$(mysql -h 127.0.0.1 -P "$PORT" -u"$ADMIN_USER" -p"$ADMIN_PASS" -N -B -e "
  SELECT COUNT(*) FROM mysql_servers
  WHERE hostgroup_id IN (SELECT writer_hostgroup FROM mysql_galera_hostgroups)
  AND status='ONLINE';
" 2>/dev/null || echo "-1")

if [ "$WRITERS" = "1" ]; then
  echo "PASS: ISC-18 — exactly one ONLINE writer"
else
  echo "FAIL: ISC-18 — writer count=$WRITERS (expected 1)"
  exit 1
fi

# ISC-23: read/write split OFF
RWS=$(mysql -h 127.0.0.1 -P "$PORT" -u"$ADMIN_USER" -p"$ADMIN_PASS" -N -B -e "
  SELECT active_reads FROM mysql_galera_hostgroups LIMIT 1;
" 2>/dev/null || echo "-1")

# active_reads=0 means no read split (all reads to writer)
if [ "$RWS" = "0" ]; then
  echo "PASS: ISC-23 — read/write split disabled"
else
  echo "FAIL: ISC-23 — active_reads=$RWS (expected 0, split must be off without app analysis)"
  exit 1
fi

echo "PASS: ISC-18, ISC-23 — ProxySQL routing correct"
exit 0
