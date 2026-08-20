# Makefile — stabilny interfejs operatora.
# Komendy dodawane INKREMENTALNIE wraz z działającym feature.

# Samo `make` pokazuje help — zaden cel (w szczegolnosci destrukcyjny
# galera-rebuild) nie moze startowac domyslnie.
.DEFAULT_GOAL := help

.PHONY: cluster-build cluster-recover help lab-up lab-start-services cluster-discover cluster-validate cluster-deploy \
        cluster-bootstrap cluster-health cluster-join cluster-proxysql cluster-endpoint \
        cluster-infra cluster-firewall cluster-firewall-verify cluster-harden cluster-monitoring cluster-monitoring-refresh cluster-backup cluster-backup-configure \
        cluster-restore-drill cluster-rolling-restart cluster-patch cluster-upgrade-plan \
        cluster-drift cluster-remove-node-plan cluster-remove-node cluster-alerts \
        lab-galera-verify lab-proxysql-verify lab-endpoint-verify lab-failover-test lab-failover-hard-test cluster-tls-rotate \
        cluster-app-host lab-app-verify lab-app-bench lab-app-degradation-test \
        lab-split-brain-test lab-backup-verify lab-restore-verify lab-backup-impact \
        lab-hardening-verify lab-monitoring-verify lab-rolling-restart-verify \
        lab-upgrade-plan-verify lab-patch-verify lab-drift-verify lab-gcache-verify lab-seed-smoke lab-proxysql-failover-test lab-post-build-gate \
        verify-no-mass-restart verify-no-double-bootstrap verify-zero-hardcode verify-no-conditional-env verify-no-secrets-leak verify-proxysql-tenancy verify-no-state-latest verify-docs-fetch-hook verify-address-collision \
        infra-teardown infra-provision cluster-trust-hosts cluster-deregister cluster-deregister-verify

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
# Provider bpg/proxmox uwierzytelnia sie ALBO tokenem API (PROXMOX_VE_API_TOKEN),
# ALBO haslem (PROXMOX_VE_PASSWORD). Bramka zadajaca wylacznie hasla odbijala
# operatora uzywajacego tokena — wystarczy dowolne z dwoch.
pve_auth_guard = @test -n "$$PROXMOX_VE_API_TOKEN" -o -n "$$PROXMOX_VE_PASSWORD" || { echo "ERROR: ustaw PROXMOX_VE_API_TOKEN albo PROXMOX_VE_PASSWORD" >&2; exit 1; }


galera-rebuild:  ## Przebuduj TYLKO wezly Galera+restore (zachowuje PMM i ProxySQL); CONFIRM=yes
	$(cluster_guard)
	@: "$${PROXMOX_VE_ENDPOINT:?Ustaw PROXMOX_VE_ENDPOINT}"
	$(pve_auth_guard)
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (kasuje $(GALERA_VMS) w $(CLUSTER))"; exit 1)
	@cd $(TF_DIR) && terraform init -input=false >/dev/null
	terraform/pve-teardown.sh $(TF_DIR) $(GALERA_VMS)
	cd $(TF_DIR) && terraform apply -auto-approve -parallelism=1

infra-teardown:  ## Zniszcz VM klastra + posprzątaj sieroty ZFS (wymaga CONFIRM=yes)
	$(cluster_guard)
	@: "$${PROXMOX_VE_ENDPOINT:?Ustaw PROXMOX_VE_ENDPOINT}"
	$(pve_auth_guard)
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (kasuje WSZYSTKIE VM klastra $(CLUSTER))"; exit 1)
	@cd $(TF_DIR) && terraform init -input=false >/dev/null
	terraform/pve-teardown.sh $(TF_DIR)


cluster-deregister:  ## Usuń obiekty PMM/Grafana/ProxySQL i konto MinIO klastra; zachowaj bucket (CONFIRM=yes)
	$(cluster_guard)
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (usuwa obiekty klastra $(CLUSTER) i konto MinIO; bucket zostaje)"; exit 1)
	ansible-playbook playbooks/cluster_deregister.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

cluster-deregister-verify:  ## Zweryfikuj brak sierot w PMM, Grafanie, ProxySQL i kontach MinIO
	$(TARGET_ENV) python3 tests/lab/probe-orphans.py
infra-provision:  ## Utwórz VM klastra (parallelism=1 — równoległość wywala locki ZFS na PVE)
	$(cluster_guard)
	@: "$${PROXMOX_VE_ENDPOINT:?Ustaw PROXMOX_VE_ENDPOINT}"
	$(pve_auth_guard)
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


# Jedna, jawna orkiestracja pelnego budowania klastra: kazdy krok to ISTNIEJACY
# cel, w kolejnosci zaleznosci. cluster-deploy juz robi firewall (f2_install +
# site + firewall.yml) — tutaj ZADNEGO dodatkowego kroku firewall. CLUSTER i
# CONFIRM propaguja sie na pod-make przez MAKEFLAGS; make domyslnie zatrzymuje
# sie na pierwszej porazce linii recepty, wiec build stal na pierwszym bledzie.
#
# Kroki warunkowe (seed/backup/alerts/app-host) da sie pominac bez edycji
# Makefile: BUILD_SKIP="alerts app-host" make cluster-build CLUSTER=... CONFIRM=yes
# Zaleznosc seed->backup: na PUSTYM klastrze laboratoryjnym pominiencie seed
# wymaga pominiencia TEZ backupu — restore drill wymaga danych z seeda, nie ma
# wtedy czego archiwizowac ani przywracac. Seed pomijaj niezaleznie TYLKO wtedy,
# gdy user data juz istnieje (np. odtwarzasz klaster z istniejacymi bazami).
# Backup materializuje dowody bramki po budowie: configure -> backup -> restore
# drill (CONFIRM=yes) -> odswiezenie metryk swiezosci; kazdy krok fail-fast.
BUILD_SKIP ?=

cluster-build:  ## Caly klaster jednym poleceniem: validate→deploy→bootstrap→join→proxysql→monitoring→harden→endpoint→warunkowe→bramka (CLUSTER+CONFIRM=yes)
	$(cluster_guard)
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (bootstrap tworzy nowy Primary Component)"; exit 1)
	$(MAKE) cluster-validate
	$(MAKE) cluster-deploy
	$(MAKE) cluster-bootstrap
	$(MAKE) cluster-join
	$(MAKE) cluster-proxysql
	$(MAKE) cluster-monitoring
	$(MAKE) cluster-harden
	$(MAKE) cluster-endpoint
	@for step in $(filter-out $(BUILD_SKIP),seed backup alerts app-host); do \
		case $$step in \
			seed) $(MAKE) lab-seed-smoke || exit 1 ;; \
			backup) $(MAKE) cluster-backup-configure || exit 1; \
				$(MAKE) cluster-backup || exit 1; \
				$(MAKE) cluster-restore-drill CONFIRM=yes || exit 1; \
				$(MAKE) cluster-monitoring-refresh || exit 1 ;; \
			alerts) $(MAKE) cluster-alerts || exit 1 ;; \
			app-host) $(MAKE) cluster-app-host || exit 1 ;; \
		esac; \
	done
	$(MAKE) lab-post-build-gate

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

# Utrata CALEJ maszyny (sysrq b), nie tylko procesu bazy. Inna sciezka awarii:
# maszyna znika bez zamkniecia gniazd, a po restarcie wraca i dolacza przez IST.
# Zmierzone na newclaude8-r9, workload z tests/lab/workload-numbered.sh:
#   soft (SIGKILL mariadbd) : przerwa 6.0-6.2 s
#   hard (sysrq, maszyna)   : przerwa 0.0-0.1 s   (3 przebiegi, powtarzalne)
# Wbrew intuicji twarda utrata maszyny jest tu MNIEJ odczuwalna dla klienta niz
# zabicie samego procesu; mechanizmu nie zweryfikowano, wiec zadnej teorii tutaj.
# Wartosc tego celu nie lezy w dlugosci przerwy, tylko w pokryciu sciezki, ktorej
# soft nie dotyka: zero utraconych transakcji przy zniknieciu maszyny i powrot
# wezla po crashu (rejoin). Oddzielny pomiar recznym generatorem po TLS przez VIP
# dal 15.8 s — inna konfiguracja klienta daje inny wynik, nie porownuj wprost.
# Wymaga realnych VM — kontenerowy lab nie ma zapisywalnego /proc/sysrq-trigger.
lab-failover-hard-test:  ## F9 — failover przy TWARDEJ utracie maszyny (sysrq, lab-only, destrukcyjny)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	$(TARGET_ENV) FAILOVER_MODE=hard tests/lab/chaos-failover.py

# Rotacja materialu TLS bez przestoju, wg galera-security/reloading-tls-
# certificates-without-downtime.md: atomowa podmiana plikow + FLUSH SSL per wezel
# (przeladowuje kontekst serwera ORAZ providera wsrep) + dowod, ze wezel serwuje
# juz nowy certyfikat. Zadnego restartu i zadnego okna serwisowego.
# Zmiana SCIEZEK do certow to inna operacja — idzie przez server.cnf i cluster-deploy.
cluster-tls-rotate:  ## Rotuj certyfikaty TLS Galery bez przestoju (FLUSH SSL, serial:1)
	$(cluster_guard)
	ansible-playbook playbooks/tls_rotate.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

# Host aplikacyjny: nalezy do warstwy wspolnej (terraform/shared/), wiec przezywa
# przebudowy klastrow. `cluster-app-host` instaluje na nim klienta w wersji z
# lockfile'a JEGO platformy i rozprowadza CA testowanego klastra.
cluster-app-host:  ## Przygotuj host aplikacyjny (klient + CA klastra) dla grupy `app`
	$(cluster_guard)
	ansible-playbook playbooks/app_host.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

# Jedyna sonda patrzaca na klaster OCZAMI APLIKACJI: po sieci, przez VIP, klientem
# z lockfile'a. Pozostale patrza z hosta kontrolnego albo z samych wezlow, przez co
# przepuscily dwa realne defekty widoczne tylko stad (weryfikacja certu przez VIP,
# blad protokolu zamiast bledu bazy przy utracie kworum).
lab-app-verify:  ## Zweryfikuj kontrakt aplikacyjny z hosta `app` (TLS, read-your-writes, transakcje, jeden writer)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	$(TARGET_ENV) APP_DB_PASSWORD="$${APP_DB_PASSWORD}" tests/lab/probe-app-conformance.py

# Pomiar Z HOSTA APLIKACYJNEGO, nie z wezla klastra: wczesniejsze benchmarki
# szly z hosta `restore`, ktory dzieli CPU i siec z warstwa bazodanowa.
lab-app-bench:  ## Zmierz przepustowosc z hosta `app` (direct vs VIP, TLS vs plaintext)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	$(TARGET_ENV) APP_DB_PASSWORD="$${APP_DB_PASSWORD}" tests/lab/bench-app.py

# Sondy stanu ustalonego mowia, ze wszystko dziala, dopoki wszystko dziala.
# Ta sprawdza, co aplikacja widzi przy utracie kworum: czy zapis zostaje
# odrzucony (bezpieczenstwo) i czy blad da sie odroznic od awarii sieci.
lab-app-degradation-test:  ## Zachowanie aplikacji przy utracie kworum (lab-only, destrukcyjny)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	$(TARGET_ENV) APP_DB_PASSWORD="$${APP_DB_PASSWORD}" tests/lab/chaos-app-degradation.py

# Caly pozostaly chaos celuje w Galere. Ta sprawdza WARSTWE POSREDNIA — ta, przez
# ktora aplikacja faktycznie chodzi. Tryb `service` (domyslny) zabija sam proces
# ProxySQL i zostawia keepalived przy zyciu: VRRP nie widzi wtedy nic, wiec VIP
# moze zabrac WYLACZNIE vrrp_script chk_proxysql. Tryb `node` gasi cala maszyne
# przez API Proxmoksa i sprawdza klasyczny VRRP.
lab-proxysql-failover-test:  ## Awaria wezla ProxySQL — przelaczenie VIP, ciaglosc, brak utraty (lab-only)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	$(TARGET_ENV) APP_DB_PASSWORD="$${APP_DB_PASSWORD}" \
	  PROXYSQL_FAILOVER_MODE="$${PROXYSQL_FAILOVER_MODE:-service}" \
	  PROXYSQL_VMIDS="$${PROXYSQL_VMIDS:-}" \
	  PROXMOX_VE_API_TOKEN="$${PROXMOX_VE_API_TOKEN:-}" \
	  tests/lab/chaos-proxysql-failover.py

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

verify-proxysql-tenancy:  ## Statyczny guard: klastry na wspólnym ProxySQL mają rozłączne hostgroupy i app_user
	python3 tests/validation/probe-proxysql-tenancy.py

# Adres hypervisora bierze sie z PROXMOX_VE_ENDPOINT, wiec ta czesc dziala tylko
# lokalnie; kolizje miedzy klastrami i z VIP-em sa sprawdzane zawsze, takze w CI.
verify-address-collision:  ## Statyczny guard: adresy wezlow nie kolidują z hypervisorem, innym klastrem ani VIP-em
	python3 tests/validation/probe-address-collision.py

verify-no-secrets-leak:  ## Statyczny guard: brak sekretów w repo i argv procesów
	bash tests/validation/probe-no-secrets-leak.sh

verify-no-state-latest:  ## Statyczny guard: brak state: latest w rolach i playbookach (ISC-63)
	bash tests/validation/probe-no-state-latest.sh

# `node`, nie `deno`: CI ma node w obrazie, deno wymagaloby dodatkowego kroku.
# Node >= 22.18 zdejmuje typy sam, wiec plik .ts idzie bez transpilacji.
verify-docs-fetch-hook:  ## Statyczny guard: hook blokujacy scraping dokumentacji przez curl
	node tests/validation/probe-docs-fetch-hook.ts

cluster-backup-configure:  ## F10 — skonfiguruj runner, minio identity i cron dla klastra
	$(cluster_guard)
	ansible-playbook playbooks/f10_backup.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e galera_backup_action=configure $(ANSIBLE_OPTS)

cluster-backup:  ## F10 — backup → destination storage via galera-backup runner
	$(cluster_guard)
	ansible-playbook playbooks/f10_backup.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e galera_backup_action=run $(ANSIBLE_OPTS)

lab-seed-smoke:  ## LAB — zasiej minimalne dane user-space, bez których drill restore pada
	$(cluster_guard)
	ansible-playbook playbooks/lab_seed_smoke.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

cluster-restore-drill:  ## F10 — restore drill na czysty host + integralność (wymaga CONFIRM=yes)
	$(cluster_guard)
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (drill kasuje datadir hosta grupy restore)"; exit 1)
	ansible-playbook playbooks/f10_restore.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e restore_confirm=yes $(ANSIBLE_OPTS)

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
	ansible-playbook playbooks/f11_pmm_agent.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
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


# Cold recovery CALEGO klastra Galera (planowane okno / pelna awaria), w odroznieniu
# od cluster-rolling-restart (zywy klaster, wezel po wezle). Kontrakt bezpieczenstwa:
#   1. CLUSTER+CONFIRM=yes (cold recovery zatrzymuje caly klaster).
#   2. playbooks/cluster_recover.yml: odczytuje stan WSZYSTKICH wezlow — przy zywym
#      Primary Component konczy procedure (to scenariusz rolling-restart, ISC-65),
#      zatrzymuje Galere SERIALNIE (czyste zamkniecie zapisuje ostateczny seqno
#      do grastate.dat) i wybiera wezel bootstrap JAWNIE: jawny BOOTSTRAP_NODE,
#      wezel z safe_to_bootstrap=1, albo unikalny najwyzszy seqno. Przy remisie
#      STAJE zamiast zgadywac — wtedy: make cluster-recover ... BOOTSTRAP_NODE=<wezel>.
#   3. Bootstrapu NIE dublujemy: wybrany wezel idzie do kanonicznego
#      playbooks/bootstrap.yml przez istniejacy cel cluster-bootstrap z parametrami
#      bootstrap_node + bootstrap_confirm_all_down=true, potem join z brama zdrowia.
#
# Wezel wybrany przez playbooks/cluster_recover.yml. Sciezka jest ABSOLUTNA:
# ansible.builtin.copy z delegate_to: localhost zapisuje na hoscie sterujacym,
# a wzgledny dest moglby wskazac katalog domowy uzytkownika Ansible zamiast
# repozytorium. Wybor odczytuje POWLOKA dopiero na liniach bootstrap/join:
# `$(shell cat ...)` w zmiennej Make byloby rozwiniete przed uruchomieniem
# playbooka, czyli zanim plik stanu powstanie.
RECOVER_STATE_FILE = $(CURDIR)/clusters/$(CLUSTER)/recover-bootstrap-node
cluster-recover:  ## Cold recovery Galera: serialny stop + bezpieczny bootstrap + join (CLUSTER+CONFIRM=yes; BOOTSTRAP_NODE przy remisie)
	$(cluster_guard)
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (cold recovery zatrzymuje caly klaster)"; exit 1)
	@: "$${SST_PASSWORD:?Ustaw SST_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/cluster_recover.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e confirm=yes -e recover_state_file=$(RECOVER_STATE_FILE) $(if $(BOOTSTRAP_NODE),-e recover_bootstrap_node=$(BOOTSTRAP_NODE)) $(ANSIBLE_OPTS)
	@test -s $(RECOVER_STATE_FILE) || { echo "ERROR: playbooks/cluster_recover.yml nie zapisal wezla bootstrap" >&2; exit 1; }
	@echo "Bootstrap po recovery: $$(cat $(RECOVER_STATE_FILE)) (kanoniczny playbooks/bootstrap.yml)"
	$(MAKE) cluster-bootstrap ANSIBLE_OPTS="$(ANSIBLE_OPTS) -e bootstrap_node=$$(cat "$(RECOVER_STATE_FILE)") -e bootstrap_confirm_all_down=true"
	$(MAKE) cluster-join ANSIBLE_OPTS="$(ANSIBLE_OPTS) -e join_bootstrap_node=$$(cat "$(RECOVER_STATE_FILE)")"
	$(MAKE) cluster-health

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

# Jedno polecenie po zbudowaniu klastra: wszystkie sondy STANU USTALONEGO.
# Kazda sonda jest fail-closed (tests/lab/_probe_common.py): brak odpowiedzi
# hosta to UNDETERMINED (exit 2), nie zielone "wszystko OK". Pierwszy niezerowy
# kod konczy bramke — nie ma sensu mierzyc dalej na klastrze, ktory nie
# przeszedl kontraktu.
lab-post-build-gate:  ## Bramka po budowie: wszystkie sondy stanu ustalonego, fail-closed
	$(TARGET_ENV) tests/lab/probe-galera-cluster.py
	$(TARGET_ENV) tests/lab/probe-proxysql.py
	$(TARGET_ENV) tests/lab/probe-endpoint.py
	$(TARGET_ENV) tests/lab/probe-hardening.py
	$(TARGET_ENV) APP_DB_PASSWORD="$${APP_DB_PASSWORD}" tests/lab/probe-app-conformance.py
	$(TARGET_ENV) tests/lab/probe-backup.py
	$(TARGET_ENV) tests/lab/probe-restore.py
	$(TARGET_ENV) tests/lab/probe-rolling-restart.py
	$(TARGET_ENV) tests/lab/probe-upgrade-plan.py
	$(TARGET_ENV) tests/lab/probe-patch.py
	$(TARGET_ENV) tests/lab/probe-drift.py
	$(TARGET_ENV) tests/lab/probe-gcache.py
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	$(TARGET_ENV) PMM_ADMIN_PASSWORD="$${PMM_ADMIN_PASSWORD}" tests/lab/probe-pmm-native.py
	@echo "PASS: brama po budowie — wszystkie sondy stanu ustalonego zmierzone i zielone"
