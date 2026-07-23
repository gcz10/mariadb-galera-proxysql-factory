# Makefile — stabilny interfejs operatora.
# Komendy dodawane INKREMENTALNIE wraz z działającym feature.

.PHONY: help lab-up cluster-discover cluster-validate cluster-deploy cluster-bootstrap cluster-health cluster-monitoring lab-monitoring-verify

CLUSTER ?= example-cluster
ANSIBLE_OPTS ?=

help:  ## Pokaż dostępne komendy
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-28s %s\n", $$1, $$2}'
lab-up:  ## Zbuduj i uruchom laboratorium, usuwając osierocone usługi
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	@test -f tests/lab/ssh_key || ssh-keygen -q -t ed25519 -N '' -f tests/lab/ssh_key
	@test -f tests/lab/ssh_key.pub || ssh-keygen -y -f tests/lab/ssh_key > tests/lab/ssh_key.pub
	@chmod 600 tests/lab/ssh_key
	@chmod 644 tests/lab/ssh_key.pub
	docker compose -f tests/lab/docker-compose.yml up -d --build --remove-orphans


cluster-discover:  ## F0 Discovery — zbierz fakty z hostów (read-only)
	ansible-playbook playbooks/f0_discovery.yml -i clusters/$(CLUSTER)/inventory.yml $(ANSIBLE_OPTS)

cluster-validate:  ## Waliduj konfigurację klastra (schema + preflight)
	python3 tests/validation/validate-cluster-schema.py clusters/$(CLUSTER)/cluster.yml
	ansible-playbook playbooks/f2_preflight.yml -i clusters/$(CLUSTER)/inventory.yml $(ANSIBLE_OPTS)

cluster-deploy:  ## F2+F3 — instaluj pakiety + konfiguruj (idempotentny converge)
	@: "$${SST_PASSWORD:?Ustaw SST_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f2_install.yml -i clusters/$(CLUSTER)/inventory.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/site.yml -i clusters/$(CLUSTER)/inventory.yml $(ANSIBLE_OPTS)

cluster-bootstrap:  ## F4 — initial bootstrap (JEDEN węzeł, wymaga confirm=yes)
	@: "$${SST_PASSWORD:?Ustaw SST_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/bootstrap.yml -i clusters/$(CLUSTER)/inventory.yml -e confirm=yes -l gnode1 $(ANSIBLE_OPTS)

cluster-health:  ## Weryfikuj cluster status (wsrep)
	ansible-playbook playbooks/f2_preflight.yml -i clusters/$(CLUSTER)/inventory.yml $(ANSIBLE_OPTS) --check
	@echo "Galera status per node:"
	@ansible galera -i clusters/$(CLUSTER)/inventory.yml -m shell -a "mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e \"SHOW STATUS WHERE Variable_name IN ('wsrep_cluster_status','wsrep_cluster_size','wsrep_connected','wsrep_ready','wsrep_local_state')\"" $(ANSIBLE_OPTS)

cluster-join:  ## F5 — dołącz węzły Galera do Primary Component (SST mariabackup)
	@: "$${SST_PASSWORD:?Ustaw SST_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f5_join.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-galera-verify:  ## Zweryfikuj zdrowie klastra Galera (ISC-7/8/9/10/14/16)
	tests/lab/probe-galera-cluster.py

cluster-proxysql:  ## F7 — skonfiguruj ProxySQL (mysql_galera_hostgroups)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	@: "$${PROXYSQL_MONITOR_PASSWORD:?Ustaw PROXYSQL_MONITOR_PASSWORD poza repozytorium}"
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f7_proxysql.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-proxysql-verify:  ## Zweryfikuj routing ProxySQL (ISC-18/19/20/21/22/23)
	tests/lab/probe-proxysql.py

cluster-endpoint:  ## F8 — redundantny endpoint ProxySQL (Keepalived VIP)
	@: "$${KEEPALIVED_AUTH_PASS:?Ustaw KEEPALIVED_AUTH_PASS poza repozytorium}"
	ansible-playbook playbooks/f8_keepalived.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-endpoint-verify:  ## Zweryfikuj endpoint VIP ProxySQL (ISC-24/26)
	tests/lab/probe-endpoint.py

lab-failover-test:  ## F9 — test failover writera (ISC-27/28, lab-only, destrukcyjny)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	tests/lab/chaos-failover.py

lab-split-brain-test:  ## F9 — test split-brain / partycji sieci (ISC-30, lab-only, destrukcyjny)
	tests/lab/chaos-split-brain.py

verify-no-mass-restart:  ## F9 — statyczny guard: brak masowego restartu Galery (ISC-31)
	python3 tests/validation/probe-no-mass-restart.py

cluster-backup:  ## F10 — backup → off-cluster S3 (szyfr, checksum, metadata); alert przy porażce
	@: "$${MINIO_ROOT_USER:?Ustaw MINIO_ROOT_USER poza repozytorium}"
	@: "$${MINIO_ROOT_PASSWORD:?Ustaw MINIO_ROOT_PASSWORD poza repozytorium}"
	@: "$${BACKUP_ENCRYPTION_KEY:?Ustaw BACKUP_ENCRYPTION_KEY poza repozytorium}"
	CLUSTER=$(CLUSTER) tests/lab/backup-run.sh backup

cluster-restore-drill:  ## F10 — restore drill na czysty host + integralność (ISC-36/37)
	@: "$${MINIO_ROOT_USER:?Ustaw MINIO_ROOT_USER poza repozytorium}"
	@: "$${BACKUP_ENCRYPTION_KEY:?Ustaw BACKUP_ENCRYPTION_KEY poza repozytorium}"
	CLUSTER=$(CLUSTER) tests/lab/backup-run.sh restore

lab-backup-verify:  ## F10 — zweryfikuj backup w S3 (ISC-32/33/34/35)
	tests/lab/probe-backup.py

lab-restore-verify:  ## F10 — zweryfikuj stan restore drill (ISC-36/37)
	tests/lab/probe-restore.py

lab-backup-impact:  ## F10 — backup pod obciążeniem nie degraduje writera (ISC-39, lab-only)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	tests/lab/backup-impact.py

cluster-harden:  ## F6 — hardening MariaDB: usuń anon/test, root localhost-only, least privilege
	ansible-playbook playbooks/f6_hardening.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-hardening-verify:  ## Zweryfikuj hardening MariaDB (ISC-40/41/42)
	tests/lab/probe-hardening.py

cluster-monitoring:  ## F11 — zarejestruj hosty i usługi w natywnym PMM Inventory
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	@: "$${PMM_MONITOR_PASSWORD:?Ustaw PMM_MONITOR_PASSWORD poza repozytorium}"
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f11_node_exporter.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/f11_pmm_client.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/f11_proxysql_metrics.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/f11_freshness.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/f11_log_lifecycle.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

cluster-monitoring-refresh:  ## F11 — odśwież metryki świeżości (po backup/restore)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f11_freshness.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-monitoring-verify:  ## Zweryfikuj natywne PMM Inventory i metryki laboratorium
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	PMM_ADMIN_PASSWORD="$${PMM_ADMIN_PASSWORD}" tests/lab/probe-pmm-native.py

cluster-rolling-restart:  ## F12 — rolling restart Galera serial:1 + brama zdrowia (ISC-50/51)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f12_rolling_restart.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-rolling-restart-verify:  ## F12 — zweryfikuj rolling restart (ISC-50/51)
	tests/lab/probe-rolling-restart.py

cluster-upgrade-plan:  ## F12 — wygeneruj read-only plan major upgrade (ISC-53/54/56)
	ansible-playbook playbooks/f12_upgrade_plan.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-upgrade-plan-verify:  ## F12 — zweryfikuj plan major upgrade (ISC-53/54/56)
	tests/lab/probe-upgrade-plan.py

cluster-patch:  ## F12 — rolling patch z canary + brama zdrowia (ISC-52/55/57)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f12_patch.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-patch-verify:  ## F12 — zweryfikuj wzorzec canary patch (ISC-52/55/57)
	tests/lab/probe-patch.py
