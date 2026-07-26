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
# Wczytaj sekrety z chronionego pliku (cron/systemd timer nie ma środowiska operatora).
# Plik (np. /etc/mariadb-backup/secrets.env, mode 0600 root) musi eksportować:
# MINIO_ROOT_USER, MINIO_ROOT_PASSWORD, BACKUP_ENCRYPTION_KEY.
if [ -n "${BACKUP_SECRETS_FILE:-}" ] && [ -f "${BACKUP_SECRETS_FILE:-}" ]; then
  set -a; . "${BACKUP_SECRETS_FILE}"; set +a
fi

MODE="${1:?Uzycie: backup-run.sh backup|restore}"
CLUSTER="${CLUSTER:-lab-cluster}"
INV="clusters/${CLUSTER}/inventory.yml"
CFG="clusters/${CLUSTER}/cluster.yml"
PLAYBOOK="playbooks/f10_${MODE}.yml"
TS="$(date -u +%FT%TZ)"

EXTRA=()
case "$MODE" in
  backup)  ALERT_GROUP="galera" ;;
  restore)
    ALERT_GROUP="restore"
    # audit#5: f10_restore czysci datadir hosta docelowego i wymaga -e restore_confirm=yes.
    # Nie potwierdzamy tego automatycznie - intencja musi byc jawna na kazdym wejsciu:
    #   operator -> `make cluster-restore-drill CONFIRM=yes`
    #   cron/timer -> RESTORE_CONFIRM=yes w BACKUP_SECRETS_FILE albo w srodowisku unitu.
    if [ "${RESTORE_CONFIRM:-no}" != "yes" ]; then
      echo "backup-run: restore wymaga RESTORE_CONFIRM=yes (drill kasuje datadir na hoscie grupy 'restore')." >&2
      exit 2
    fi
    EXTRA=(-e restore_confirm=yes)
    ;;
  *) echo "Nieznany tryb: $MODE (backup|restore)"; exit 2 ;;
esac

ansible-playbook "$PLAYBOOK" -i "$INV" -e "@$CFG" "${EXTRA[@]}"
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
