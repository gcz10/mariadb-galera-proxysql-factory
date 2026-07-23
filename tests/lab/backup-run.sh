#!/usr/bin/env bash
# F10 backup/restore entrypoint z dostarczaniem alertu przy porażce.
#   ISC-37: wywoływany przez systemd timer / cron wg cluster.yml (full_backup_schedule,
#           restore_test_schedule); drill zapisuje stan (last_backup/last_restore.json).
#   ISC-38: przy porażce dostarcza alert do monitorowanego kanału (log + stan na węźle)
#           i kończy się kodem != 0.
#
# Użycie: backup-run.sh backup|restore
# Wymaga zmiennych środowiskowych sekretów (MINIO_ROOT_USER/PASSWORD, BACKUP_ENCRYPTION_KEY).
set -uo pipefail

MODE="${1:?Uzycie: backup-run.sh backup|restore}"
CLUSTER="${CLUSTER:-lab-cluster}"
INV="clusters/${CLUSTER}/inventory.yml"
CFG="clusters/${CLUSTER}/cluster.yml"
PLAYBOOK="playbooks/f10_${MODE}.yml"
TS="$(date -u +%FT%TZ)"

case "$MODE" in
  backup)  ALERT_GROUP="galera" ;;
  restore) ALERT_GROUP="restore" ;;
  *) echo "Nieznany tryb: $MODE (backup|restore)"; exit 2 ;;
esac

ansible-playbook "$PLAYBOOK" -i "$INV" -e "@$CFG"
RC=$?
if [ "$RC" -eq 0 ]; then
  echo "backup-run: ${MODE} OK (${TS})"
  # ISC-49: odśwież metryki świeżości w PMM textfile collector po udanym run.
  ansible-playbook playbooks/f11_freshness.yml -i "$INV" -e "@$CFG" >/dev/null 2>&1 || \
    echo "backup-run: WARN — odświeżenie metryk świeżości nie powiodło się (niekrytyczne)" >&2
  exit 0
fi

MSG="ALERT mariadb-${MODE} FAILED rc=${RC} cluster=${CLUSTER} at=${TS}"
# ISC-38: dostarcz alert do monitorowanego kanału (log + osobny stan porażki).
# last_${MODE}.json przechowuje wyłącznie ostatni SUKCES ( świeżość F11);
# porażka trafia do last_${MODE}_failure.json, aby nie nadpisać dowodu świeżości.
ansible "$ALERT_GROUP" -i "$INV" -m ansible.builtin.shell -a \
  "mkdir -p /var/lib/mariadb-backup-state /var/log;
   echo '${MSG}' >> /var/log/mariadb-backup.log;
   printf '{\"status\":\"failed\",\"mode\":\"%s\",\"rc\":%s,\"time\":\"%s\"}\n' '${MODE}' '${RC}' '${TS}' \
     > /var/lib/mariadb-backup-state/last_${MODE}_failure.json;
   logger -t mariadb-backup '${MSG}' 2>/dev/null || true" >/dev/null 2>&1 || true
echo "backup-run: ${MSG}" >&2
exit 1
