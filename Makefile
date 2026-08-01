# Makefile — stabilny interfejs operatora.
# Komendy dodawane INKREMENTALNIE wraz z działającym feature.

.PHONY: help lab-up lab-start-services cluster-discover cluster-validate cluster-deploy \
        cluster-bootstrap cluster-health cluster-join cluster-proxysql cluster-endpoint \
        cluster-infra cluster-firewall cluster-firewall-verify cluster-harden cluster-monitoring cluster-monitoring-refresh cluster-backup \
        cluster-restore-drill cluster-rolling-restart cluster-patch cluster-upgrade-plan \
        cluster-drift cluster-remove-node-plan cluster-remove-node cluster-alerts \
        lab-galera-verify lab-proxysql-verify lab-endpoint-verify lab-failover-test \
        lab-split-brain-test lab-backup-verify lab-restore-verify lab-backup-impact \
        lab-hardening-verify lab-monitoring-verify lab-rolling-restart-verify \
        lab-upgrade-plan-verify lab-patch-verify lab-drift-verify lab-gcache-verify \
        verify-no-mass-restart verify-no-double-bootstrap verify-zero-hardcode verify-no-conditional-env \
        infra-teardown infra-provision cluster-trust-hosts

CLUSTER ?= example-cluster
ANSIBLE_OPTS ?=
TARGET_ENV = CLUSTER=$(CLUSTER) CLUSTER_CONFIG=clusters/$(CLUSTER)/cluster.yml CLUSTER_INVENTORY=clusters/$(CLUSTER)/inventory.yml

# Cel mutujący wymaga jawnego CLUSTER= (command line/env), nie domyślnego example-cluster.
cluster_guard = @case "$(origin CLUSTER)" in file|default|undefined) echo "ERROR: ten cel jest mutujący — podaj CLUSTER=... (domyślny example-cluster niedozwolony)" >&2; exit 1;; esac

# TF_DIR domyślnie wyprowadzany z nazwy klastra; nadpisywalny dla nietypowych układów.
TF_DIR ?= terraform/$(CLUSTER)

# Wezly przebudowywane przy iteracji na samej Galerze. infranode (PMM/MinIO/Maildev,
# stan monitoringu) i pnode* (ProxySQL) zostaja NIETKNIETE — stawianie ich od nowa
# przy kazdej zmianie w Galerze to 11+ min zmarnowane (Docker CE + pull PMM +
# zimny start PMM + reinstalacja ProxySQL).
GALERA_VMS ?= gnode1 gnode2 gnode3 rnode1

galera-rebuild:  ## Przebuduj TYLKO wezly Galera+restore (zachowuje PMM i ProxySQL); CONFIRM=yes
	$(cluster_guard)
	@: "$${PROXMOX_VE_ENDPOINT:?Ustaw PROXMOX_VE_ENDPOINT}"
	@: "$${PROXMOX_VE_PASSWORD:?Ustaw PROXMOX_VE_PASSWORD}"
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (kasuje $(GALERA_VMS) w $(CLUSTER))"; exit 1)
	@cd $(TF_DIR) && terraform init -input=false >/dev/null
	terraform/pve-teardown.sh $(TF_DIR) $(GALERA_VMS)
	cd $(TF_DIR) && terraform apply -auto-approve -parallelism=1

infra-teardown:  ## Zniszcz VM klastra + posprzątaj sieroty ZFS (wymaga CONFIRM=yes)
	$(cluster_guard)
	@: "$${PROXMOX_VE_ENDPOINT:?Ustaw PROXMOX_VE_ENDPOINT}"
	@: "$${PROXMOX_VE_PASSWORD:?Ustaw PROXMOX_VE_PASSWORD}"
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (kasuje WSZYSTKIE VM klastra $(CLUSTER))"; exit 1)
	@cd $(TF_DIR) && terraform init -input=false >/dev/null
	terraform/pve-teardown.sh $(TF_DIR)

infra-provision:  ## Utwórz VM klastra (parallelism=1 — równoległość wywala locki ZFS na PVE)
	$(cluster_guard)
	@: "$${PROXMOX_VE_ENDPOINT:?Ustaw PROXMOX_VE_ENDPOINT}"
	@: "$${PROXMOX_VE_PASSWORD:?Ustaw PROXMOX_VE_PASSWORD}"
	cd $(TF_DIR) && terraform init -input=false >/dev/null && terraform apply -auto-approve -parallelism=1

# known_hosts jest git-ignorowany, a inventory wymusza StrictHostKeyChecking=yes.
# Po kazdym re-provision klucze hosta sie zmieniaja — bez tego kroku KAZDY
# ansible/ssh pada z "Host key verification failed". Wymagane po infra-provision.
#
# Petla per-host, bo ssh-keyscan potrafi zlapac host ZANIM cloud-init wystartuje
# sshd z ostatecznymi kluczami (wyscig — zlapalem to raz na .33). Wtedy scan zapisuje
# klucz tymczasowy i polaczenie pada. Dlatego: scan -> probka polaczenia -> retry.
cluster-trust-hosts:  ## Re-skanuj klucze hostow do known_hosts (po re-provision)
	$(cluster_guard)
	@mkdir -p clusters/$(CLUSTER)
	@ok=0; total=0; \
	for ip in $$(grep -oE 'ansible_host:[[:space:]]+"?[0-9.]+"?' clusters/$(CLUSTER)/inventory.yml | grep -oE '[0-9.]+' | sort -u); do \
		total=$$((total+1)); good=0; \
		for try in 1 2 3 4 5 6 7 8 9 10 11 12; do \
			ssh-keygen -R $$ip -f clusters/$(CLUSTER)/known_hosts >/dev/null 2>&1 || true; \
			ssh-keyscan -H $$ip >> clusters/$(CLUSTER)/known_hosts 2>/dev/null || true; \
			sort -u clusters/$(CLUSTER)/known_hosts -o clusters/$(CLUSTER)/known_hosts 2>/dev/null || true; \
			if ssh -i tests/lab/ssh_key -o StrictHostKeyChecking=yes -o UserKnownHostsFile=clusters/$(CLUSTER)/known_hosts -o ConnectTimeout=5 -o ConnectionAttempts=1 -o BatchMode=yes -o PasswordAuthentication=no root@$$ip true 2>/dev/null; then \
				good=1; break; \
			fi; \
			sleep 5; \
		done; \
		[ "$$good" = "1" ] && ok=$$((ok+1)) || echo "UWAGA: $$ip nie odpowiada po 12 probach"; \
	done; \
	echo "known_hosts: $$ok/$$total hostow zwerifikowanych (ssh OK)"; \
	[ "$$ok" = "$$total" ] || exit 1

help:  ## Pokaż dostępne komendy
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-28s %s\n", $$1, $$2}'
lab-up:  ## Zbuduj i uruchom laboratorium, usuwając osierocone usługi
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	@test -f tests/lab/ssh_key || ssh-keygen -q -t ed25519 -N '' -f tests/lab/ssh_key
	@test -f tests/lab/ssh_key.pub || ssh-keygen -y -f tests/lab/ssh_key > tests/lab/ssh_key.pub
	@chmod 600 tests/lab/ssh_key
	@chmod 644 tests/lab/ssh_key.pub
	docker compose -f tests/lab/docker-compose.yml up -d --build --remove-orphans

lab-start-services:  ## Lab-only: (re)start ProxySQL po restarcie kontenera (brak systemd, idempotentny)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	ansible proxysql -i clusters/$(CLUSTER)/inventory.yml -m shell -a "pgrep -x proxysql >/dev/null || proxysql --idle-threads -c /etc/proxysql.cnf" $(ANSIBLE_OPTS)
	ansible proxysql -i clusters/$(CLUSTER)/inventory.yml -m wait_for -a "host=127.0.0.1 port=6032 timeout=20" $(ANSIBLE_OPTS)


cluster-discover:  ## F0 Discovery — zbierz fakty z hostów (read-only)
	ansible-playbook playbooks/f0_discovery.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

cluster-validate:  ## Waliduj konfigurację klastra (schema + invariants inventory + preflight)
	python3 tests/validation/validate-cluster-schema.py clusters/$(CLUSTER)/cluster.yml clusters/schema/cluster.schema.json
	python3 tests/validation/validate-inventory.py clusters/$(CLUSTER)/inventory.yml clusters/$(CLUSTER)/cluster.yml
	ansible-playbook playbooks/f2_preflight.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

cluster-deploy:  ## F2+F3 — instaluj pakiety + konfiguruj (idempotentny converge)
	$(cluster_guard)
	@: "$${SST_PASSWORD:?Ustaw SST_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f2_install.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/site.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/firewall.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

cluster-infra:  ## Usługi wspierające na infra VM: PMM + MinIO + Maildev
	$(cluster_guard)
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	@: "$${MINIO_ROOT_USER:?Ustaw MINIO_ROOT_USER poza repozytorium}"
	@: "$${MINIO_ROOT_PASSWORD:?Ustaw MINIO_ROOT_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/infra_services.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
cluster-firewall:  ## Wymuś minimalną politykę firewalld według roli hosta
	$(cluster_guard)
	ansible-playbook playbooks/firewall.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
cluster-firewall-verify:  ## Zweryfikuj dokładną politykę firewalld i Docker ingress
	CLUSTER_CONFIG=clusters/$(CLUSTER)/cluster.yml CLUSTER_INVENTORY=clusters/$(CLUSTER)/inventory.yml \
		python3 tests/lab/probe-firewall.py



cluster-bootstrap:  ## F4 — initial bootstrap (JEDEN węzeł, wymaga CONFIRM=yes)
	$(cluster_guard)
	@: "$${SST_PASSWORD:?Ustaw SST_PASSWORD poza repozytorium}"
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (bootstrap tworzy nowy Primary Component)"; exit 1)
	ansible-playbook playbooks/bootstrap.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e confirm=yes $(ANSIBLE_OPTS)

cluster-health:  ## Weryfikuj cluster status (wsrep)
	@echo "Galera status per node:"
	@ansible galera -i clusters/$(CLUSTER)/inventory.yml -m shell -a "mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e \"SHOW STATUS WHERE Variable_name IN ('wsrep_cluster_status','wsrep_cluster_size','wsrep_connected','wsrep_ready','wsrep_local_state')\"" $(ANSIBLE_OPTS)

cluster-join:  ## F5 — dołącz węzły Galera do Primary Component (SST mariabackup)
	$(cluster_guard)
	@: "$${SST_PASSWORD:?Ustaw SST_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f5_join.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-galera-verify:  ## Zweryfikuj zdrowie klastra Galera (ISC-7/8/9/10/14/16)
	$(TARGET_ENV) tests/lab/probe-galera-cluster.py

cluster-proxysql:  ## F7 — skonfiguruj ProxySQL (mysql_galera_hostgroups)
	$(cluster_guard)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	@: "$${PROXYSQL_MONITOR_PASSWORD:?Ustaw PROXYSQL_MONITOR_PASSWORD poza repozytorium}"
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f7_proxysql.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-proxysql-verify:  ## Zweryfikuj routing ProxySQL (ISC-18/19/20/21/22/23)
	$(TARGET_ENV) tests/lab/probe-proxysql.py

cluster-endpoint:  ## F8 — redundantny endpoint ProxySQL (Keepalived VIP)
	$(cluster_guard)
	@: "$${KEEPALIVED_AUTH_PASS:?Ustaw KEEPALIVED_AUTH_PASS poza repozytorium}"
	ansible-playbook playbooks/f8_keepalived.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-endpoint-verify:  ## Zweryfikuj endpoint VIP ProxySQL (ISC-24/26)
	$(TARGET_ENV) tests/lab/probe-endpoint.py

lab-failover-test:  ## F9 — test failover writera (ISC-27/28, lab-only, destrukcyjny)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	$(TARGET_ENV) tests/lab/chaos-failover.py

lab-split-brain-test:  ## F9 — test split-brain / partycji sieci (ISC-30, lab-only, destrukcyjny)
	$(TARGET_ENV) tests/lab/chaos-split-brain.py

verify-no-mass-restart:  ## F9 — statyczny guard: brak masowego restartu Galery (ISC-31)
	python3 tests/validation/probe-no-mass-restart.py

verify-no-double-bootstrap:  ## F13 — statyczny guard: brak dwóch niezależnych Primary (ISC-65)
	python3 tests/validation/probe-no-double-bootstrap.py
verify-zero-hardcode:  ## F14 — statyczny guard: brak hardkodowanych danych klastra (ISC-58/59)
	python3 tests/validation/probe-zero-hardcode.py

verify-no-conditional-env:  ## Statyczny guard: play-level environment bez warunkowej konfiguracji backupu
	python3 tests/validation/probe-no-conditional-env.py

cluster-backup:  ## F10 — backup → off-cluster S3 (szyfr, checksum, metadata); alert przy porażce
	$(cluster_guard)
	@: "$${MINIO_ROOT_USER:?Ustaw MINIO_ROOT_USER poza repozytorium}"
	@: "$${MINIO_ROOT_PASSWORD:?Ustaw MINIO_ROOT_PASSWORD poza repozytorium}"
	@: "$${BACKUP_ENCRYPTION_KEY:?Ustaw BACKUP_ENCRYPTION_KEY poza repozytorium}"
	CLUSTER=$(CLUSTER) tests/lab/backup-run.sh backup

cluster-restore-drill:  ## F10 — restore drill na czysty host + integralność (wymaga CONFIRM=yes)
	$(cluster_guard)
	@: "$${MINIO_ROOT_USER:?Ustaw MINIO_ROOT_USER poza repozytorium}"
	@: "$${BACKUP_ENCRYPTION_KEY:?Ustaw BACKUP_ENCRYPTION_KEY poza repozytorium}"
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (drill kasuje datadir hosta grupy 'restore')"; exit 1)
	CLUSTER=$(CLUSTER) RESTORE_CONFIRM=yes tests/lab/backup-run.sh restore

lab-backup-verify:  ## F10 — zweryfikuj backup w S3 (ISC-32/33/34/35)
	$(TARGET_ENV) tests/lab/probe-backup.py

lab-restore-verify:  ## F10 — zweryfikuj stan restore drill (ISC-36/37)
	$(TARGET_ENV) tests/lab/probe-restore.py

lab-backup-impact:  ## F10 — backup pod obciążeniem nie degraduje writera (ISC-39, lab-only)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	$(TARGET_ENV) tests/lab/backup-impact.py

cluster-harden:  ## F6 — hardening MariaDB: usuń anon/test, root localhost-only, least privilege
	$(cluster_guard)
	ansible-playbook playbooks/f6_hardening.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-hardening-verify:  ## Zweryfikuj hardening MariaDB (ISC-40/41/42)
	$(TARGET_ENV) tests/lab/probe-hardening.py

cluster-monitoring:  ## F11 — zarejestruj hosty i usługi w natywnym PMM Inventory
	$(cluster_guard)
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	@: "$${PMM_MONITOR_PASSWORD:?Ustaw PMM_MONITOR_PASSWORD poza repozytorium}"
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f11_node_exporter.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/f11_pmm_client.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/f11_proxysql_metrics.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/f11_freshness.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/f11_log_lifecycle.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

cluster-monitoring-refresh:  ## F11 — odśwież metryki świeżości (po backup/restore)
	$(cluster_guard)
	ansible-playbook playbooks/f11_freshness.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-monitoring-verify:  ## Zweryfikuj natywne PMM Inventory i metryki laboratorium
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	$(TARGET_ENV) PMM_ADMIN_PASSWORD="$${PMM_ADMIN_PASSWORD}" tests/lab/probe-pmm-native.py

cluster-rolling-restart:  ## F12 — rolling restart Galera serial:1 + brama zdrowia (ISC-50/51)
	$(cluster_guard)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f12_rolling_restart.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-rolling-restart-verify:  ## F12 — zweryfikuj rolling restart (ISC-50/51)
	$(TARGET_ENV) tests/lab/probe-rolling-restart.py

cluster-upgrade-plan:  ## F12 — wygeneruj read-only plan major upgrade (ISC-53/54/56)
	ansible-playbook playbooks/f12_upgrade_plan.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-upgrade-plan-verify:  ## F12 — zweryfikuj plan major upgrade (ISC-53/54/56)
	$(TARGET_ENV) tests/lab/probe-upgrade-plan.py

cluster-patch:  ## F12 — rolling patch z canary + brama zdrowia (ISC-52/55/57)
	$(cluster_guard)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f12_patch.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-patch-verify:  ## F12 — zweryfikuj wzorzec canary patch (ISC-52/55/57)
	$(TARGET_ENV) tests/lab/probe-patch.py

cluster-drift:  ## F13 — read-only raport dryfu konfiguracji (ISC-21)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f13_drift.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-drift-verify:  ## F13 — zweryfikuj drift detection (ISC-21)
	$(TARGET_ENV) tests/lab/probe-drift.py

cluster-remove-node-plan:  ## F13 — read-only plan usunięcia węzła Galera (wymaga node=gnodeX)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	@test -n "$(NODE)" || (echo "Ustaw NODE=gnodeX (np. make cluster-remove-node-plan NODE=gnode2)"; exit 1)
	ansible-playbook playbooks/f13_remove_node_plan.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e node=$(NODE) $(ANSIBLE_OPTS)

cluster-remove-node:  ## F13 — usuń węzeł Galera (confirm-gated, wymaga NODE=gnodeX CONFIRM=yes)
	$(cluster_guard)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (destrukcyjne)"; exit 1)
	@test -n "$(NODE)" || (echo "Ustaw NODE=gnodeX"; exit 1)
	ansible-playbook playbooks/f13_remove_node.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e node=$(NODE) -e confirm=yes $(ANSIBLE_OPTS)

cluster-alerts:  ## F15 — provision alert rules ISC-47 (quorum/writer/node loss + freshness)
	$(cluster_guard)
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f15_alerts.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-gcache-verify:  ## F0/ISC-68 — zmierz write rate + weryfikuj gcache.size (IST window)
	$(TARGET_ENV) tests/lab/probe-gcache.py
