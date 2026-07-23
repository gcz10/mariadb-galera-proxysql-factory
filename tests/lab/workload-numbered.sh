#!/bin/bash
# Numbered workload przez endpoint ProxySQL (VIP) — dowód ISC-27/28.
# Zapisuje monotoniczne, nigdy nie powtarzane seq do isa_test.isa_failover.
# Każdy commit (rc=0) trafia do LOG jako "epoch_ts seq" — to zbiór transakcji
# potwierdzonych klientowi. Po failoverze każdy zalogowany seq MUSI istnieć w DB.
# Hasło z --defaults-extra-file (bez sekretu w argv ani repo).
#
# Args: VIP PORT CNF LOG
set -u
VIP="$1"; PORT="$2"; CNF="$3"; LOG="$4"
TABLE="isa_failover"
: > "$LOG"
seq=0
while [ -f /tmp/workload.run ]; do
  seq=$((seq + 1))
  if mariadb --defaults-extra-file="$CNF" --skip-ssl -h"$VIP" -P"$PORT" \
       --connect-timeout=2 isa_test \
       -e "INSERT INTO ${TABLE} (seq) VALUES (${seq})" 2>/dev/null; then
    echo "$(date +%s.%N) ${seq}" >> "$LOG"
  fi
  # seq nigdy nie jest ponawiany: nieudany zapis (in-flight podczas failover)
  # pozostaje "niepewny" i nie liczy się jako potwierdzony. Brak duplikatów PK.
  sleep 0.02
done
