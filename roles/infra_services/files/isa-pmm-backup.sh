#!/bin/bash
# Cykliczna kopia danych PMM (volume isa-pmm-data) do zarzadzanego MinIO.
#
# Wdraza playbooks/platform_pmm_backup.yml; uruchamia isa-pmm-backup.timer.
# Konfiguracja: /etc/isa-pmm-backup/backup.env (nazwy, bez sekretow) oraz
# /etc/isa-pmm-backup/s3.env (0600, MC_HOST_backup z kluczem BEZ prawa kasowania;
# retencje robi regula ILM bucketa).
#
# SPOJNOSC: Percona zaleca zatrzymanie pmm-server na czas kopii volume
# (docs PMM 3, "Back up PMM Server Docker container"). Kopia z pracujacego
# serwera lapalaby w polowie zapisu PostgreSQL/ClickHouse/VictoriaMetrics.
# Koszt: ~1-2 min bez metryk i alertow na kazdy przebieg — decyzja operatora
# 2026-09-26 (raz w tygodniu).
#
# BEZPIECZENSTWO SERWISU: pmm-server wraca ZAWSZE (trap EXIT), takze po bledzie
# archiwizacji lub przerwaniu; kopia do MinIO idzie dopiero po powrocie serwera,
# wiec awaria MinIO nie przedluza przestoju monitoringu.
#
# WYNIK: metryki w katalogu textfile collectora pmm-agent na tym hoscie
# (isa_pmm_backup_*), z ktorych regula isa-shared-pmm-backup-stale liczy wiek.
set -Eeuo pipefail

CONF=/etc/isa-pmm-backup/backup.env
S3_ENV=/etc/isa-pmm-backup/s3.env
STAGING=/var/lib/isa-pmm-backup
TEXTFILE_DIR=/usr/local/percona/pmm/collectors/textfile-collector/low-resolution
METRICS="$TEXTFILE_DIR/isa_pmm_backup.prom"

# shellcheck source=/dev/null
. "$CONF"
: "${PMM_BACKUP_BUCKET:?}" "${PMM_BACKUP_PLATFORM:?}" "${PMM_BACKUP_MC_IMAGE:?}"
PMM_DATA_VOLUME=${PMM_DATA_VOLUME:-isa-pmm-data}

exec 9>/run/lock/isa-pmm-backup.lock
flock -n 9 || { echo "isa-pmm-backup: inny przebieg trwa" >&2; exit 1; }

started=$(date +%s)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
name="pmm-${PMM_BACKUP_PLATFORM}-${stamp}.tar.gz"
archive="$STAGING/$name"
was_running=0
stopped_at=0
downtime=0

write_metrics() {
  # Plik tymczasowy w TYM SAMYM katalogu: dziedziczy etykiete SELinux katalogu,
  # a rename jest atomowy — node_exporter nigdy nie czyta polowy pliku.
  local tmp
  tmp=$(mktemp "$TEXTFILE_DIR/.isa_pmm_backup.XXXXXX")
  cat >"$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$METRICS"
}

previous_success() {
  awk '/^isa_pmm_backup_last_success_unixtime /{print $2}' "$METRICS" 2>/dev/null || true
}

start_pmm() {
  if [ "$was_running" = 1 ]; then
    docker start pmm-server >/dev/null
    downtime=$(( $(date +%s) - stopped_at ))
    was_running=0
  fi
}

on_exit() {
  local rc=$?
  start_pmm || true
  rm -f "$archive" "$archive.sha256"
  if [ "$rc" -ne 0 ]; then
    local last
    last=$(previous_success)
    write_metrics <<EOF || true
# HELP isa_pmm_backup_last_success_unixtime Czas ostatniej udanej kopii PMM w MinIO.
# TYPE isa_pmm_backup_last_success_unixtime gauge
isa_pmm_backup_last_success_unixtime ${last:-0}
# HELP isa_pmm_backup_last_run_success 1 gdy ostatni przebieg sie powiodl.
# TYPE isa_pmm_backup_last_run_success gauge
isa_pmm_backup_last_run_success 0
# HELP isa_pmm_backup_last_failure_unixtime Czas ostatniej nieudanej kopii PMM.
# TYPE isa_pmm_backup_last_failure_unixtime gauge
isa_pmm_backup_last_failure_unixtime $(date +%s)
EOF
    echo "isa-pmm-backup: porazka rc=$rc" >&2
  fi
}
trap on_exit EXIT

pmm_image=$(docker inspect pmm-server --format '{{.Config.Image}}')
if [ "$(docker inspect pmm-server --format '{{.State.Running}}')" = "true" ]; then
  stopped_at=$(date +%s)
  docker stop pmm-server >/dev/null
  was_running=1
fi

install -d -m 0700 "$STAGING"
docker run --rm --user 0 --network none \
  -v "$PMM_DATA_VOLUME:/from:ro" -v "$STAGING:/to:z" \
  --entrypoint /bin/sh "$pmm_image" -ceu \
  "tar -C /from -czf /to/$name . && cd /to && sha256sum $name > $name.sha256 && chmod 0600 /to/$name /to/$name.sha256"

start_pmm

# Serwer wrocil — reszta nie przedluza przestoju. Czekamy jednak na healthy,
# zeby porazke startu zglosic TUTAJ, a nie dowiedziec sie o niej z ciszy.
for _ in $(seq 1 60); do
  [ "$(docker inspect pmm-server --format '{{.State.Health.Status}}')" = "healthy" ] && break
  sleep 5
done
[ "$(docker inspect pmm-server --format '{{.State.Health.Status}}')" = "healthy" ] \
  || { echo "isa-pmm-backup: pmm-server nie wrocil do healthy" >&2; exit 1; }

size=$(stat -c %s "$archive")
mc() {
  docker run --rm --network container:minio --env-file "$S3_ENV" \
    -v "$STAGING:/staging:ro,z" "$PMM_BACKUP_MC_IMAGE" "$@"
}
mc cp --quiet "/staging/$name" "/staging/$name.sha256" "backup/$PMM_BACKUP_BUCKET/"
# `mc stat`/`mc ls` traktuja cel jako PREFIKS — zwracaja tez `$name.sha256`.
# Rozmiar bierzemy wylacznie z wiersza o DOKLADNIE tym kluczu.
remote_size=$(mc ls --json "backup/$PMM_BACKUP_BUCKET/$name" \
  | grep -F "\"key\":\"$name\"" | sed -n 's/.*"size":\([0-9]*\).*/\1/p')
[ "$remote_size" = "$size" ] \
  || { echo "isa-pmm-backup: rozmiar w MinIO ($remote_size) != lokalny ($size)" >&2; exit 1; }

finished=$(date +%s)
write_metrics <<EOF
# HELP isa_pmm_backup_last_success_unixtime Czas ostatniej udanej kopii PMM w MinIO.
# TYPE isa_pmm_backup_last_success_unixtime gauge
isa_pmm_backup_last_success_unixtime $finished
# HELP isa_pmm_backup_last_run_success 1 gdy ostatni przebieg sie powiodl.
# TYPE isa_pmm_backup_last_run_success gauge
isa_pmm_backup_last_run_success 1
# HELP isa_pmm_backup_size_bytes Rozmiar ostatniej kopii PMM (skompresowanej).
# TYPE isa_pmm_backup_size_bytes gauge
isa_pmm_backup_size_bytes $size
# HELP isa_pmm_backup_pmm_downtime_seconds Jak dlugo pmm-server byl zatrzymany.
# TYPE isa_pmm_backup_pmm_downtime_seconds gauge
isa_pmm_backup_pmm_downtime_seconds $downtime
# HELP isa_pmm_backup_duration_seconds Czas calego przebiegu.
# TYPE isa_pmm_backup_duration_seconds gauge
isa_pmm_backup_duration_seconds $(( finished - started ))
EOF
echo "isa-pmm-backup: OK $name size=$size downtime=${downtime}s total=$(( finished - started ))s"
