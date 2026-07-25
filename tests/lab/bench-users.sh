#!/usr/bin/env bash
# Benchmark testowej bazy użytkowników (isa_test.users) przez endpoint ProxySQL.
#
# Mierzy realną ścieżkę aplikacji: klient -> VIP Keepalived -> ProxySQL -> writer Galera.
# R/W split jest wyłączony (ISC-23), więc cały ruch (także odczyty) obsługuje jeden writer —
# liczby poniżej to przepustowość POJEDYNCZEGO writera, nie suma z trzech węzłów.
#
# Klientem obciążenia jest rnode1 (host restore, poza klastrem DB), żeby sysbench
# nie konkurował o CPU z węzłami bazodanowymi.
#
# Hasło idzie przez MYSQL_PWD (docker exec -e), nigdy przez argv — patrz ISC-43.
#
# Wymaga: APP_DB_PASSWORD w środowisku. Uruchomienie z repo root:
#   set -a; . tests/lab/.env; set +a; tests/lab/bench-users.sh [rows] [time_s]
set -uo pipefail

ROWS="${1:-200000}"
DURATION="${2:-20}"
CLIENT="${BENCH_CLIENT:-rnode1}"
CLUSTER="${CLUSTER:-lab-cluster}"
WRITER="${BENCH_WRITER:-gnode1}"
: "${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"

VIP="$(python3 -c "import yaml;print(yaml.safe_load(open('clusters/${CLUSTER}/cluster.yml'))['proxysql']['endpoint']['address'])")"
PORT="$(python3 -c "import yaml;print(yaml.safe_load(open('clusters/${CLUSTER}/cluster.yml'))['proxysql']['endpoint']['port'])")"

SB="sysbench --db-driver=mysql --mysql-host=${VIP} --mysql-port=${PORT} \
--mysql-user=app_user --mysql-db=isa_test --mysql-ignore-errors=all \
/tmp/users_bench.lua --users=${ROWS}"

dex() { docker exec -e MYSQL_PWD="$APP_DB_PASSWORD" "$CLIENT" bash -c "$1"; }

# Metryki Galera zbieramy na WRITERZE (tam certyfikacja i flow control faktycznie boli).
writer_metric() {
  docker exec "$WRITER" mariadb --socket=/var/lib/mysql/mysql.sock -N -B \
    -e "SHOW STATUS WHERE Variable_name='$1'" 2>/dev/null | awk '{print $2}'
}

echo "== klient obciążenia: ${CLIENT} -> VIP ${VIP}:${PORT} (${ROWS} wierszy, ${DURATION}s/próba) =="
docker cp tests/lab/bench/users_bench.lua "${CLIENT}:/tmp/users_bench.lua" >/dev/null

fc_before=$(writer_metric wsrep_flow_control_paused_ns)

echo
printf '%-8s %-6s %12s %10s %10s %8s\n' TRYB WATKI "QPS" "avg_ms" "p95_ms" "bledy"
printf '%s\n' "------------------------------------------------------------"

for mode in point email range update mixed; do
  for threads in 1 8 32; do
    out=$(dex "${SB} --mode=${mode} --threads=${threads} --time=${DURATION} run" 2>&1)
    qps=$(echo "$out" | awk '/^ *queries:/ {gsub(/[()]/,"");print $3; exit}')
    avg=$(echo "$out" | awk '/^ *avg:/ {print $2; exit}')
    p95=$(echo "$out" | awk '/95th percentile:/ {print $3; exit}')
    err=$(echo "$out" | awk '/ignored errors:/ {print $3; exit}')
    printf '%-8s %-6s %12s %10s %10s %8s\n' "$mode" "$threads" "${qps:-?}" "${avg:-?}" "${p95:-?}" "${err:-0}"
  done
done

fc_after=$(writer_metric wsrep_flow_control_paused_ns)

echo
echo "== wpływ na klaster Galera (writer ${WRITER}) =="
echo "  flow_control_paused delta : $(( ${fc_after:-0} - ${fc_before:-0} )) ns"
echo "  local_recv_queue_avg      : $(writer_metric wsrep_local_recv_queue_avg)"
echo "  local_cert_failures       : $(writer_metric wsrep_local_cert_failures)"
echo "  cluster_size / status     : $(writer_metric wsrep_cluster_size) / $(writer_metric wsrep_cluster_status)"

echo
echo "== spójność danych na wszystkich węzłach (po obciążeniu) =="
prev=""
consistent=1
for n in gnode1 gnode2 gnode3; do
  sum=$(docker exec "$n" mariadb --socket=/var/lib/mysql/mysql.sock -N -B \
        -e "SELECT CONCAT(COUNT(*),'/',COALESCE(SUM(login_count),0)) FROM isa_test.users" 2>/dev/null)
  echo "  ${n}: wierszy/suma_login_count = ${sum}"
  [ -n "$prev" ] && [ "$sum" != "$prev" ] && consistent=0
  prev="$sum"
done
if [ "$consistent" -eq 1 ]; then
  echo "  => PASS: wszystkie węzły identyczne (replikacja spójna po obciążeniu zapisami)"
else
  echo "  => FAIL: rozjazd między węzłami"
  exit 1
fi
