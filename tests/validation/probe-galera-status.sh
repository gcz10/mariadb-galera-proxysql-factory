#!/usr/bin/env bash
# Sonda: sprawdza stan Galera klastra (ISC-7,8,9,10,17).
# Uruchomienie na węźle Galera: ./tests/validation/probe-galera-status.sh [socket] [expected_size]
# PASS: jeden Primary, expected size, identyczny UUID, connected+ready+synced
# FAIL: inaczej
set -euo pipefail

SOCKET="${1:-/var/lib/mysql/mysql.sock}"
EXPECTED_SIZE="${2:-3}"

CMD="mariadb --socket=$SOCKET -N -B"

# ISC-7: jeden Primary Component
CLUSTER_STATUS=$($CMD -e "SHOW STATUS LIKE 'wsrep_cluster_status'" 2>/dev/null | awk '{print $2}')
if [ "$CLUSTER_STATUS" = "Primary" ]; then
  echo "PASS: ISC-7 — Primary Component"
else
  echo "FAIL: ISC-7 — cluster status is '$CLUSTER_STATUS', expected Primary"
  exit 1
fi

# ISC-8: cluster size
CLUSTER_SIZE=$($CMD -e "SHOW STATUS LIKE 'wsrep_cluster_size'" 2>/dev/null | awk '{print $2}')
if [ "$CLUSTER_SIZE" = "$EXPECTED_SIZE" ]; then
  echo "PASS: ISC-8 — cluster size $CLUSTER_SIZE"
else
  echo "FAIL: ISC-8 — cluster size '$CLUSTER_SIZE' != expected '$EXPECTED_SIZE'"
  exit 1
fi

# ISC-10: connected, ready, synced
CONNECTED=$($CMD -e "SHOW STATUS LIKE 'wsrep_connected'" 2>/dev/null | awk '{print $2}')
READY=$($CMD -e "SHOW STATUS LIKE 'wsrep_ready'" 2>/dev/null | awk '{print $2}')
LOCAL_STATE=$($CMD -e "SHOW STATUS LIKE 'wsrep_local_state'" 2>/dev/null | awk '{print $2}')

if [ "$CONNECTED" = "ON" ] && [ "$READY" = "ON" ] && [ "$LOCAL_STATE" = "4" ]; then
  echo "PASS: ISC-10 — connected=ON, ready=ON, local_state=4 (Synced)"
else
  echo "FAIL: ISC-10 — connected=$CONNECTED, ready=$READY, local_state=$LOCAL_STATE (expected ON,ON,4)"
  exit 1
fi

# ISC-9: cluster UUID (wymaga wielu węzłów — zwraca UUID do porównania zewnętrznego)
CLUSTER_UUID=$($CMD -e "SHOW STATUS LIKE 'wsrep_cluster_state_uuid'" 2>/dev/null | awk '{print $2}')
echo "INFO: ISC-9 — cluster_state_uuid=$CLUSTER_UUID (compare across all nodes)"

echo "PASS: ISC-7,8,10 — Galera cluster healthy on this node"
exit 0
