# Makefile — stabilny interfejs operatora.
# Komendy dodawane INKREMENTALNIE wraz z działającym feature.

# Samo `make` pokazuje help — zaden cel (w szczegolnosci destrukcyjny
# galera-rebuild) nie moze startowac domyslnie.
.DEFAULT_GOAL := help

.PHONY: galera-rebuild cluster-build cluster-recover help cluster-discover cluster-validate cluster-deploy \
        cluster-bootstrap cluster-health cluster-join cluster-proxysql \
        cluster-firewall cluster-firewall-verify cluster-harden cluster-monitoring cluster-monitoring-refresh cluster-backup cluster-backup-configure \
        cluster-restore-drill cluster-rolling-restart cluster-patch cluster-upgrade-plan \
        cluster-drift cluster-remove-node-plan cluster-remove-node cluster-alerts \
        lab-galera-verify lab-proxysql-verify lab-endpoint-verify lab-failover-test lab-failover-hard-test cluster-tls-rotate \
        cluster-app-host lab-app-verify lab-app-bench lab-app-degradation-test \
        lab-split-brain-test lab-backup-verify lab-restore-verify lab-backup-impact \
        lab-hardening-verify lab-monitoring-verify lab-rolling-restart-verify \
        lab-upgrade-plan-verify lab-patch-verify lab-drift-verify lab-gcache-verify lab-seed-smoke lab-proxysql-failover-test lab-post-build-gate \
        verify-no-mass-restart verify-no-double-bootstrap verify-zero-hardcode verify-role-contract verify-no-conditional-env verify-no-secrets-leak verify-proxysql-tenancy verify-no-state-latest verify-docs-fetch-hook verify-address-collision verify-dead-code \
        infra-teardown infra-provision cluster-trust-hosts cluster-deregister cluster-deregister-verify fleet-state \
        platform-validate platform-trust-hosts platform-deploy platform-firewall platform-infra platform-proxysql platform-monitor-rotate platform-endpoint platform-monitoring platform-alerts platform-adopt platform-build platform-verify

CLUSTER ?= example-cluster
ANSIBLE_OPTS ?=
TARGET_ENV = CLUSTER=$(CLUSTER) CLUSTER_CONFIG=clusters/$(CLUSTER)/cluster.yml CLUSTER_INVENTORY=clusters/$(CLUSTER)/inventory.yml

# Warstwa wspolna (para ProxySQL + VIP + host monitoringu + host aplikacyjny) jest
# jednostka NIEZALEZNA od klastrow. Wczesniej jej wlascicielem byl klaster Galera
# przez `proxysql.role: owner`, wiec skasowanie tego klastra osierocalo cala warstwe.
#
# Domyslny cel to SZABLON, nie instancja. Wczesniej stalo tu `shared` — konkretna
# warstwa, ktora z czasem przestala istniec, wiec `make platform-*` bez argumentu
# celowalo w martwe maszyny zamiast odmowic. Warstw jest w repo wiecej niz jedna,
# a szablon ma adresy `10.0.x`, wiec brak jawnego PLATFORM= konczy sie bledem,
# nie cicha operacja na cudzej infrastrukturze.
PLATFORM ?= example
PLATFORM_DIR = platform/$(PLATFORM)
PLATFORM_OPTS = -i $(PLATFORM_DIR)/inventory.yml -e @$(PLATFORM_DIR)/platform.yml $(ANSIBLE_OPTS)

# Cel zwiazany z konkretnym klastrem wymaga jawnego CLUSTER= (command line/env),
# nie domyslnego example-cluster. Dotyczy tak samo celow mutujacych, jak sond:
# sonda uruchomiona na example-cluster mierzy fikcyjne 10.0.0.0/24 i mowi o tym
# dopiero po timeoucie SSH, a `lab-failover-hard-test` bez CLUSTER= to zaproszenie
# do wykonania operacji niszczacej na cudzym klastrze.
cluster_guard = @case "$(origin CLUSTER)" in file|default|undefined) echo "ERROR: ten cel dziala na konkretnym klastrze — podaj CLUSTER=... (domyślny example-cluster niedozwolony)" >&2; exit 1;; esac

# Warstwa wspolna wymaga jawnego PLATFORM= z tych samych powodow co CLUSTER=
# wyzej: szablon example ma adresy 10.0.x, wiec cel bez argumentu konczy sie
# bledem albo czasem SSH, zamiast po cichu operowac na fikcyjnej infrastrukturze.
platform_guard = @case "$(origin PLATFORM)" in file|default|undefined) echo "ERROR: ten cel dziala na konkretnej warstwie wspolnej — podaj PLATFORM=... (domyslny szablon example niedozwolony)" >&2; exit 1;; esac

# Monitoring jest DEKLARACJA klastra, tak samo jak backup. Klaster moze go nie
# miec: bywa deweloperski albo obserwowany cudzym systemem. Bez tego przelacznika
# `cluster-build` zawsze zadal PMM i rejestrowal wezly w serwerze, ktorego moglo
# nie byc, a brama po budowie oblewala taki klaster za brak rejestracji.
# Zmienna liczona per-cel: nie chcemy czytac YAML-a przy kazdym wywolaniu make.
#
# UWAGA na semantyke recept: kazda linia to OSOBNA powloka, wiec `exit 0` w
# pierwszej linii NIE pomija kolejnych. Cele ponizej maja wiec caly korpus w
# jednym bloku `if`, a nie straznik w osobnej linii — pierwsza wersja tej zmiany
# wygladala na dzialajaca (test zwracal `false`), a mimo to uruchamiala wszystkie
# playbooki.
monitoring_enabled = $(shell python3 -c "import yaml,sys; c=yaml.safe_load(open('clusters/$(CLUSTER)/cluster.yml')) or {}; print(str((c.get('monitoring') or {}).get('enabled', True)).lower())" 2>/dev/null || echo true)
monitoring_skip_note = echo "SKIP: monitoring.enabled=false w clusters/$(CLUSTER)/cluster.yml — pomijam rejestracje w PMM"

# Backup jest DEKLARACJA klastra — dokladnie tak samo jak monitoring wyzej.
# Honorowaly go SONDY (`SKIP: backup wylaczony w cluster.yml`), a RECEPTY nie:
# `cluster-build` wolal konfiguracje kopii bezwarunkowo, wiec najemca z
# `backup.enabled: false` przechodzil deploy, bootstrap, join, ProxySQL,
# monitoring i hardening, po czym padal na braku poswiadczen S3 magazynu,
# ktorego swiadomie nie ma. Jedynym obejsciem bylo `BUILD_SKIP=backup`, czyli
# powtorzenie w wywolaniu tego, co juz stalo w konfiguracji.
backup_enabled = $(shell python3 -c "import yaml,sys; c=yaml.safe_load(open('clusters/$(CLUSTER)/cluster.yml')) or {}; print(str((c.get('backup') or {}).get('enabled', True)).lower())" 2>/dev/null || echo true)
backup_skip_note = echo "SKIP: backup.enabled=false w clusters/$(CLUSTER)/cluster.yml — pomijam konfiguracje kopii"

# TF_DIR domyślnie wyprowadzany z nazwy klastra; nadpisywalny dla nietypowych układów.
TF_DIR ?= terraform/$(CLUSTER)

# Wezly przebudowywane przy iteracji na samej Galerze. Hosty warstwy wspolnej
# (PMM/MinIO/Maildev, ProxySQL) zostaja NIETKNIETE — stawianie ich od nowa przy
# kazdej zmianie w Galerze to 11+ min zmarnowane (Docker CE + pull PMM + zimny
# start PMM + reinstalacja ProxySQL).
#
# Lista WYNIKA z inwentarza wskazanego klastra (grupy galera + restore):
# na kazdym innym klastrze cel destrukcyjny celowalby w nieistniejace maszyny.
# Nazwy hostow w inwentarzu sa tozsame z kluczami zasobow w terraform/<cluster>.
# Rekurencyjne `=` (nie `:=`): liczy sie dopiero przy uzyciu, wiec `make help`
# nie czyta zadnego inwentarza. `?=`, nie `=`: zwykle przypisanie w Makefile
# WYGRYWA ze zmienna srodowiskowa, wiec `GALERA_VMS=... make galera-rebuild`
# zostalby po cichu zignorowany — a to cel, ktory kasuje maszyny.
GALERA_VMS ?= $(shell python3 -c "import yaml,sys; c=yaml.safe_load(open('clusters/$(CLUSTER)/inventory.yml'))['all']['children']; print(' '.join(h for g in ('galera','restore') for h in (c.get(g) or {}).get('hosts', {})))" 2>/dev/null)
# Provider bpg/proxmox uwierzytelnia sie ALBO tokenem API (PROXMOX_VE_API_TOKEN),
# ALBO haslem (PROXMOX_VE_PASSWORD). Bramka zadajaca wylacznie hasla odbijala
# operatora uzywajacego tokena — wystarczy dowolne z dwoch.
pve_auth_guard = @test -n "$$PROXMOX_VE_API_TOKEN" -o -n "$$PROXMOX_VE_PASSWORD" || { echo "ERROR: ustaw PROXMOX_VE_API_TOKEN albo PROXMOX_VE_PASSWORD" >&2; exit 1; }


galera-rebuild:  ## Przebuduj TYLKO wezly Galera+restore (zachowuje PMM i ProxySQL); CONFIRM=yes
	$(cluster_guard)
	@: "$${PROXMOX_VE_ENDPOINT:?Ustaw PROXMOX_VE_ENDPOINT}"
	$(pve_auth_guard)
	@# Pusta lista to nieczytelny inwentarz albo klaster bez grup galera/restore.
	@# Bez tej bramki `pve-teardown.sh` dostaje zero argumentow i cel „udaje sukces".
	@test -n "$(GALERA_VMS)" || { echo "ERROR: nie wyznaczono wezlow z clusters/$(CLUSTER)/inventory.yml (grupy galera/restore)" >&2; exit 1; }
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (kasuje $(GALERA_VMS) w $(CLUSTER))"; exit 1)
	@cd $(TF_DIR) && terraform init -input=false >/dev/null
	CONFIRM_DESTROY=$(TF_DIR) terraform/pve-teardown.sh $(TF_DIR) $(GALERA_VMS)
	cd $(TF_DIR) && terraform apply -auto-approve -parallelism=1

infra-teardown:  ## Zniszcz VM klastra + posprzątaj sieroty ZFS (wymaga CONFIRM=yes)
	$(cluster_guard)
	@: "$${PROXMOX_VE_ENDPOINT:?Ustaw PROXMOX_VE_ENDPOINT}"
	$(pve_auth_guard)
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (kasuje WSZYSTKIE VM klastra $(CLUSTER))"; exit 1)
	@cd $(TF_DIR) && terraform init -input=false >/dev/null
	CONFIRM_DESTROY=$(TF_DIR) terraform/pve-teardown.sh $(TF_DIR)


cluster-deregister:  ## Usuń obiekty PMM/Grafana/ProxySQL i konto MinIO klastra; zachowaj bucket (CONFIRM=yes)
	$(cluster_guard)
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (usuwa obiekty klastra $(CLUSTER) i konto MinIO; bucket zostaje)"; exit 1)
	ansible-playbook playbooks/cluster_deregister.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

cluster-deregister-verify:  ## Zweryfikuj brak sierot w PMM, Grafanie, ProxySQL i kontach MinIO
	$(cluster_guard)
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
#
# DWA ROZNE NIEPOWODZENIA, DWA BUDZETY CZASU. Do 2026-08-25 host, ktorego w ogole
# nie ma, przechodzil te sama sciezke co host z niestabilnym kluczem: 12 prob po
# ~15 s = 3 minuty CISZY na kazdy adres. Pierwszy przebieg wg README na swiezo
# skopiowanym szablonie (z adresami 10.0.1.x) wygladal wiec na zawieszenie.
# Teraz najpierw pytamy, czy sshd w ogole odpowiada (banner z keyscan), i jesli
# nie — konczymy ten host po ~30 s, wypisujac go od razu.
TRUST_HOST_PROBES ?= 6
TRUST_KEYSCAN_TIMEOUT ?= 5
TRUST_KEY_RETRIES ?= 12
# ansible-inventory rozwiazuje dziedziczenie all/group/host vars, wiec uzywamy
# TEJ SAMEJ tozsamosci i portu co pozniejszy Ansible — nie hardcodujemy root ani
# secrets/ssh_key, bo szablon dokumentuje wlasnie uzytkownika nie-root z sudo.
cluster_trust_targets = $(shell ansible-inventory -i clusters/$(CLUSTER)/inventory.yml --list | python3 -c 'import json,os,shlex,sys; h=json.load(sys.stdin).get("_meta",{}).get("hostvars",{}); print(" ".join(shlex.quote("{}|{}|{}|{}".format(v.get("ansible_host",n),v.get("ansible_port",22),v.get("ansible_user","root"),os.path.expanduser(v.get("ansible_ssh_private_key_file","secrets/ssh_key")))) for n,v in sorted(h.items())))')
cluster-trust-hosts:  ## Re-skanuj klucze hostow do known_hosts (po re-provision)
	$(cluster_guard)
	@mkdir -p clusters/$(CLUSTER)
	@ok=0; total=0; dead=""; \
	for target in $(cluster_trust_targets); do \
		ip=$${target%%|*}; rest=$${target#*|}; port=$${rest%%|*}; \
		rest=$${rest#*|}; user=$${rest%%|*}; key=$${rest#*|}; \
		lookup="$$ip"; [ "$$port" = "22" ] || lookup="[$$ip]:$$port"; \
		total=$$((total+1)); good=0; alive=0; auth_failed=0; probe=0; \
		while [ "$$probe" -lt "$(TRUST_HOST_PROBES)" ]; do \
			probe=$$((probe+1)); \
			if ssh-keyscan -T "$(TRUST_KEYSCAN_TIMEOUT)" -p "$$port" "$$ip" 2>/dev/null | grep -q .; then alive=1; break; fi; \
			sleep 1; \
		done; \
		if [ "$$alive" = "0" ]; then \
			echo "  $$lookup: sshd nie odpowiada — host nie istnieje albo jeszcze nie wstal"; \
			dead="$$dead $$lookup"; continue; \
		fi; \
		errfile=$$(mktemp); try=0; \
		while [ "$$try" -lt "$(TRUST_KEY_RETRIES)" ]; do \
			try=$$((try+1)); \
			ssh-keygen -R "$$lookup" -f clusters/$(CLUSTER)/known_hosts >/dev/null 2>&1 || true; \
			ssh-keyscan -T "$(TRUST_KEYSCAN_TIMEOUT)" -p "$$port" -H "$$ip" >> clusters/$(CLUSTER)/known_hosts 2>/dev/null || true; \
			sort -u clusters/$(CLUSTER)/known_hosts -o clusters/$(CLUSTER)/known_hosts 2>/dev/null || true; \
			if ssh -i "$$key" -p "$$port" -o StrictHostKeyChecking=yes -o UserKnownHostsFile=clusters/$(CLUSTER)/known_hosts -o ConnectTimeout=5 -o ConnectionAttempts=1 -o BatchMode=yes -o PasswordAuthentication=no "$$user@$$ip" true 2>"$$errfile"; then \
				good=1; break; \
			fi; \
			if grep -qi "Permission denied" "$$errfile"; then auth_failed=1; break; fi; \
			sleep 5; \
		done; \
		rm -f "$$errfile"; \
		if [ "$$good" = "1" ]; then ok=$$((ok+1)); \
		elif [ "$$auth_failed" = "1" ]; then \
			echo "  $$lookup: sshd odpowiada, ale odrzuca $$user z kluczem $$key"; dead="$$dead $$lookup"; \
		else \
			echo "  $$lookup: sshd odpowiada, ale klucz hosta nie ustabilizowal sie po $(TRUST_KEY_RETRIES) probach"; dead="$$dead $$lookup"; \
		fi; \
	done; \
	if [ "$$total" -eq 0 ]; then \
		echo "brak hostow: ansible-inventory nie zwrocilo zadnego ansible_host dla clusters/$(CLUSTER)/inventory.yml" >&2; \
		exit 1; \
	fi; \
	echo "known_hosts: $$ok/$$total hostow zwerifikowanych (ssh OK)"; \
	if [ "$$ok" != "$$total" ]; then \
		echo "NIEOSIAGALNE:$$dead" >&2; \
		echo "Sprawdz ansible_host/ansible_port/ansible_user/ansible_ssh_private_key_file w inventory.yml." >&2; \
		exit 1; \
	fi

help:  ## Pokaż dostępne komendy
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-28s %s\n", $$1, $$2}'


# Jedna, jawna orkiestracja pelnego budowania klastra: kazdy krok to ISTNIEJACY
# cel, w kolejnosci zaleznosci. cluster-deploy juz robi firewall (f2_install +
# site + firewall.yml) — tutaj ZADNEGO dodatkowego kroku firewall. CLUSTER i
# CONFIRM propaguja sie na pod-make przez MAKEFLAGS; make domyslnie zatrzymuje
# sie na pierwszej porazce linii recepty, wiec build stal na pierwszym bledzie.
#
# Kroki warunkowe (seed/backup/alerts/app-host) da sie pominac bez edycji
# Makefile: BUILD_SKIP="alerts app-host" make cluster-build CLUSTER=... CONFIRM=yes
# Zaleznosc seed->backup jest EGZEKWOWANA w recepcie cluster-build, nie doradzana:
# na pustym klastrze pominiecie seed bez pominiecia backupu daje restore drill,
# ktory nie ma czego przywrocic, wiec bramka konczy sie zielono na pustych danych.
# Legalne warianty: BUILD_SKIP="seed backup", albo BUILD_SKIP=seed z jawnym
# EXISTING_DATA=yes (klaster ma juz dane uzytkownika, np. odtwarzasz istniejace bazy).
# Backup materializuje dowody bramki po budowie: configure -> backup -> restore
# drill (CONFIRM=yes) -> odswiezenie metryk swiezosci; kazdy krok fail-fast.
BUILD_SKIP ?=
EXISTING_DATA ?=

# ---------------------------------------------------------------------------
# WARSTWA WSPOLNA — cele niezalezne od jakiegokolwiek klastra Galera.
#
# Kolejnosc w platform-build nie jest dowolna: pakiety musza istniec zanim
# skonfigurujemy pare, para musi odpowiadac zanim Keepalived uzna ja za zdrowa,
# a rejestracja w PMM ma sens dopiero gdy eksportery maja co zbierac.
# ---------------------------------------------------------------------------

platform-validate:  ## Waliduj definicje warstwy wspolnej (schema + invarianty inwentarza + preflight)
	$(platform_guard)
	python3 tests/validation/validate-platform.py $(PLATFORM_DIR)/platform.yml platform/schema/platform.schema.json $(PLATFORM_DIR)/inventory.yml
	ansible-playbook playbooks/platform_preflight.yml $(PLATFORM_OPTS)

platform-trust-hosts:  ## Re-skanuj klucze hostow warstwy wspolnej do known_hosts
	$(platform_guard)
	@ok=0; total=0; \
	for ip in $$(grep -oE 'ansible_host:[[:space:]]+"?[0-9.]+"?' $(PLATFORM_DIR)/inventory.yml | grep -oE '[0-9.]+' | sort -u); do \
		total=$$((total+1)); good=0; \
		for try in 1 2 3 4 5 6 7 8 9 10 11 12; do \
			ssh-keygen -R $$ip -f $(PLATFORM_DIR)/known_hosts >/dev/null 2>&1 || true; \
			if ssh-keyscan -T 5 -H $$ip 2>/dev/null | grep -q .; then \
				ssh-keyscan -T 5 -H $$ip 2>/dev/null >> $(PLATFORM_DIR)/known_hosts; good=1; break; \
			fi; \
			sleep 5; \
		done; \
		[ $$good -eq 1 ] && ok=$$((ok+1)) || echo "UWAGA: $$ip nie odpowiada po 12 probach"; \
	done; \
	echo "known_hosts: $$ok/$$total hostow zweryfikowanych (ssh OK)"; \
	test $$ok -eq $$total

platform-deploy:  ## Instaluj pakiety warstwy wspolnej (ProxySQL wg lockfile EL10)
	$(platform_guard)
	ansible-playbook playbooks/platform_install.yml $(PLATFORM_OPTS)

# Polityka hosta fcp1/fcp2/fcinfra/fcapp nalezy do warstwy wspolnej. Najemca
# deklaruje te hosty w swoim inventory, ale ich firewalla nie dotyka — patrz
# bramka wlasciciela w playbooks/firewall.yml.
platform-firewall:  ## Polityka firewalld hostow warstwy wspolnej (proxysql, infra, app)
	$(platform_guard)
	ansible-playbook playbooks/firewall.yml $(PLATFORM_OPTS) -e firewall_target_hosts=proxysql:infra:app

platform-infra:  ## Uslugi warstwy wspolnej zadeklarowane w platform.infra.services
	@# Sekrety asertuje playbook, bo tylko on wie, KTORE uslugi platforma
	@# deklaruje. Twardy wymog MINIO_* w recepcie blokowal warstwe bez MinIO —
	@# konfiguracje calkowicie legalna, bo magazyn kopii moze stac gdziekolwiek.
	$(platform_guard)
	ansible-playbook playbooks/infra_services.yml $(PLATFORM_OPTS)

platform-proxysql:  ## Konfiguruj sama pare ProxySQL (frontend TLS, ustawienia globalne)
	$(platform_guard)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	@# Konto read-only dla straznika writera w backupie najemcy: rejestruje je
	@# platforma, wiec sekret jest wymagany tutaj, a nie na celach klastra.
	@: "$${PROXYSQL_STATS_PASSWORD:?Ustaw PROXYSQL_STATS_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/platform_proxysql.yml $(PLATFORM_OPTS)

platform-endpoint:  ## Redundantny endpoint ProxySQL (Keepalived VIP) — WYLACZNIE tutaj
	@: "$${KEEPALIVED_AUTH_PASS:?Ustaw KEEPALIVED_AUTH_PASS poza repozytorium}"
	$(platform_guard)
	ansible-playbook playbooks/f8_keepalived.yml $(PLATFORM_OPTS)

# `monitoring.agent_groups` w platform.yml bylo POLEM-WIDMEM: deklarowalo
# ["proxysql"], ale zaden krok go nie konsumowal, bo cel uruchamial wylacznie
# rejestracje external (restapi 6070). Natywny pmm-agent na fcp1/fcp2 istnial
# wczesniej tylko ubocznie — bo owner `finalclaude-r10` mial to pole u siebie.
# Gdy PR #60 slusznie zabral je najemcy, zniknelo jedyne zrodlo metryk
# `proxysql_connection_pool_*`, od ktorych zalezy regula ISC-47 "no ONLINE
# writer". Z `noDataState: Alerting` palila sie odtad na stale, czyli przestala
# odrozniac sprawny klaster od zepsutego. Wykryte przy odbudowie pary od zera.
platform-monitoring:  ## Zarejestruj wezly i eksportery warstwy wspolnej w PMM
	@# Sekrety asertuja playbooki (f11_pmm_agent, f11_proxysql_metrics): tylko
	@# one widza, czy platforma deklaruje `pmm` w infra.services. Twardy wymog
	@# w recepcie blokowal warstwe z monitoringiem prowadzonym osobno.
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	$(platform_guard)
	ansible-playbook playbooks/f11_pmm_agent.yml $(PLATFORM_OPTS)
	ansible-playbook playbooks/f11_proxysql_metrics.yml $(PLATFORM_OPTS)

platform-alerts:  ## Reguly alertowe warstwy wspolnej (namespace isa-shared-*)
	@# f15_alerts.yml sam wymaga PMM_ADMIN_PASSWORD i adresu alertow.
	$(platform_guard)
	ansible-playbook playbooks/f15_alerts.yml $(PLATFORM_OPTS)

# Migracja istniejacej floty: usuwa z PMM wezly zarejestrowane pod adresami
# warstwy przez bylego ownera. Swiezy deployment tego nie potrzebuje.
platform-adopt:  ## Przejmij rejestracje PMM zrobione przez bylego ownera (CONFIRM=yes)
	$(platform_guard)
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (usuwa wezly z PMM Inventory)"; exit 1)
	ansible-playbook playbooks/platform_adopt.yml $(PLATFORM_OPTS) -e confirm=yes

# Rotacja globalnego poswiadczenia monitora ProxySQL (P1-5). Wejsciem jest CALA
# flota, nie pojedynczy CLUSTER=: para `mysql-monitor_username`/`password` jest
# globalna dla instancji, a konto backendu zaklada kazdy najemca osobno.
# Kolejnosc faz JEST kontraktem — zamiana ich miejscami otwiera okno, w ktorym
# ProxySQL shunuje zdrowe backendy calej floty:
#   expand   -> na kazdym najemcy powstaje bezczynne konto i loguje sie na kazdym backendzie
#   switch   -> pojedyncza zmiana pary w ProxySQL + bramka na logu monitora
#   contract -> dopiero teraz znika konto, ktorego ProxySQL juz nie uzywa
TENANTS ?= $(filter-out example-cluster,$(notdir $(patsubst %/,%,$(dir $(wildcard clusters/*/cluster.yml)))))

platform-monitor-rotate:  ## Rotuj globalne haslo monitora ProxySQL w calej flocie (expand->switch->contract; CONFIRM=yes)
	$(platform_guard)
	@: "$${PROXYSQL_MONITOR_PASSWORD_NEXT:?Ustaw PROXYSQL_MONITOR_PASSWORD_NEXT poza repozytorium}"
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (zmienia poswiadczenie monitora calej floty)"; exit 1)
	@test -n "$(TENANTS)" || (echo "ERROR: brak najemcow w clusters/*/cluster.yml"; exit 1)
	@echo "== faza 1/3 expand: $(TENANTS) =="
	@for c in $(TENANTS); do \
		echo "-- expand $$c"; \
		ansible-playbook playbooks/monitor_rotate.yml -i clusters/$$c/inventory.yml \
			-e @clusters/$$c/cluster.yml -e rotation_phase=expand $(ANSIBLE_OPTS) || exit 1; \
	done
	@echo "== faza 2/3 switch (warstwa wspolna) =="
	ansible-playbook playbooks/platform_monitor_switch.yml $(PLATFORM_OPTS)
	@echo "== faza 3/3 contract: $(TENANTS) =="
	@for c in $(TENANTS); do \
		echo "-- contract $$c"; \
		ansible-playbook playbooks/monitor_rotate.yml -i clusters/$$c/inventory.yml \
			-e @clusters/$$c/cluster.yml -e rotation_phase=contract $(ANSIBLE_OPTS) || exit 1; \
	done
	@echo "PASS: rotacja monitora zakonczona dla: $(TENANTS)"
	@echo "UWAGA: od tej chwili obowiazuje nowa para pokazana w raporcie switch."
	@echo "       Ustaw PROXYSQL_MONITOR_USER na nowa nazwe i przenies"
	@echo "       PROXYSQL_MONITOR_PASSWORD_NEXT do PROXYSQL_MONITOR_PASSWORD,"
	@echo "       inaczej kolejny 'make platform-proxysql' cofnie pare do poprzedniej."

platform-verify:  ## Sondy warstwy wspolnej: para ProxySQL, VIP, TLS endpointu, rejestracja w PMM
	$(platform_guard)
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	CLUSTER=$(PLATFORM) CLUSTER_CONFIG=$(PLATFORM_DIR)/platform.yml CLUSTER_INVENTORY=$(PLATFORM_DIR)/inventory.yml \
	  PMM_ADMIN_PASSWORD="$${PMM_ADMIN_PASSWORD}" tests/lab/probe-platform.py

# Stan floty NIE jest dokumentem. Recznie wpisywany snapshot ogloszil kiedys
# jako aktywny stack, ktorego maszyn nie bylo od dwoch dni. Zamiar mieszka
# w `clusters/<nazwa>/` i `platform/<nazwa>/`, rzeczywistosc na hypervisorze,
# a historia w `docs/records/`. Ten cel zestawia pierwsze z drugim na zywo.
fleet-state:  ## Co naprawde zyje: maszyny w puli, definicje w repo, wspolne endpointy
	@: "$${PROXMOX_VE_API_TOKEN:?Ustaw PROXMOX_VE_API_TOKEN poza repozytorium}"
	python3 tests/lab/fleet-state.py

platform-build:  ## Cala warstwa wspolna jednym poleceniem: validate→deploy→firewall→infra→proxysql→endpoint→monitoring→alerts→sonda
	$(platform_guard)
	$(MAKE) platform-validate PLATFORM=$(PLATFORM)
	$(MAKE) platform-deploy PLATFORM=$(PLATFORM)
	$(MAKE) platform-firewall PLATFORM=$(PLATFORM)
	$(MAKE) platform-infra PLATFORM=$(PLATFORM)
	$(MAKE) platform-proxysql PLATFORM=$(PLATFORM)
	$(MAKE) platform-endpoint PLATFORM=$(PLATFORM)
	$(MAKE) platform-monitoring PLATFORM=$(PLATFORM)
	$(MAKE) platform-alerts PLATFORM=$(PLATFORM)
	$(MAKE) platform-verify PLATFORM=$(PLATFORM)

cluster-build:  ## Caly klaster jednym poleceniem: validate→deploy→bootstrap→join→proxysql→monitoring→harden→warunkowe→bramka (CLUSTER+CONFIRM=yes)
	$(cluster_guard)
	@# Sprzezenie seed->backup bylo dotad WYLACZNIE komentarzem, wiec nie istnialo.
	@# Pominiecie seed bez pominiecia backupu daje restore drill bez czego przywracac:
	@# bramka konczy sie zielono na pustych danych. Jedyne legalne wyjscie to jawna
	@# deklaracja, ze klaster ma juz dane uzytkownika.
	@# Na klastrze z `backup.enabled: false` drillu nie ma w ogole, wiec zadanie
	@# EXISTING_DATA bylo pytaniem o dane dla przebiegu, ktory nie nastapi.
	@if [ "$(backup_enabled)" = "true" ]; then \
		case " $(BUILD_SKIP) " in \
			*" seed "*) \
				case " $(BUILD_SKIP) " in \
					*" backup "*) ;; \
					*) test "$(EXISTING_DATA)" = "yes" || { \
						echo "ERROR: BUILD_SKIP pomija seed, ale nie backup — restore drill nie mialby czego przywrocic." >&2; \
						echo "       Pomin tez backup (BUILD_SKIP=\"seed backup\") albo zadeklaruj EXISTING_DATA=yes." >&2; \
						exit 1; } ;; \
				esac ;; \
		esac; \
	fi
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (bootstrap tworzy nowy Primary Component)"; exit 1)
	$(MAKE) cluster-validate
	$(MAKE) cluster-deploy
	$(MAKE) cluster-bootstrap ANSIBLE_OPTS="$(ANSIBLE_OPTS) -e bootstrap_skip_existing_primary=true"
	$(MAKE) cluster-join
	$(MAKE) cluster-proxysql
	$(MAKE) cluster-monitoring
	$(MAKE) cluster-harden
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
	$(cluster_guard)
	ansible-playbook playbooks/f0_discovery.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

cluster-validate:  ## Waliduj konfigurację klastra (schema + invariants inventory + preflight)
	$(cluster_guard)
	python3 tests/validation/validate-cluster-schema.py clusters/$(CLUSTER)/cluster.yml clusters/schema/cluster.schema.json
	python3 tests/validation/validate-inventory.py clusters/$(CLUSTER)/inventory.yml clusters/$(CLUSTER)/cluster.yml --require-known-hosts
	ansible-playbook playbooks/f2_preflight.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

cluster-deploy:  ## F2+F3 — instaluj pakiety + konfiguruj (idempotentny converge)
	$(cluster_guard)
	@: "$${SST_PASSWORD:?Ustaw SST_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f2_install.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/site.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)
	ansible-playbook playbooks/firewall.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e firewall_target_hosts=galera:restore $(ANSIBLE_OPTS)

cluster-firewall:  ## Wymuś minimalną politykę firewalld według roli hosta
	$(cluster_guard)
	ansible-playbook playbooks/firewall.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e firewall_target_hosts=galera:restore $(ANSIBLE_OPTS)
cluster-firewall-verify:  ## Zweryfikuj dokładną politykę firewalld i Docker ingress
	$(cluster_guard)
	CLUSTER_CONFIG=clusters/$(CLUSTER)/cluster.yml CLUSTER_INVENTORY=clusters/$(CLUSTER)/inventory.yml \
		python3 tests/lab/probe-firewall.py



cluster-bootstrap:  ## F4 — initial bootstrap (JEDEN węzeł, wymaga CONFIRM=yes)
	$(cluster_guard)
	@: "$${SST_PASSWORD:?Ustaw SST_PASSWORD poza repozytorium}"
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (bootstrap tworzy nowy Primary Component)"; exit 1)
	ansible-playbook playbooks/bootstrap.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e confirm=yes $(ANSIBLE_OPTS)

cluster-health:  ## Weryfikuj cluster status (wsrep)
	$(cluster_guard)
	@echo "Galera status per node:"
	@ansible galera -i clusters/$(CLUSTER)/inventory.yml -m shell -a "mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e \"SHOW STATUS WHERE Variable_name IN ('wsrep_cluster_status','wsrep_cluster_size','wsrep_connected','wsrep_ready','wsrep_local_state')\"" $(ANSIBLE_OPTS)

cluster-join:  ## F5 — dołącz węzły Galera do Primary Component (SST mariabackup)
	$(cluster_guard)
	@: "$${SST_PASSWORD:?Ustaw SST_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f5_join.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-galera-verify:  ## Zweryfikuj zdrowie klastra Galera (ISC-7/8/9/10/14/16)
	$(cluster_guard)
	$(TARGET_ENV) tests/lab/probe-galera-cluster.py

cluster-proxysql:  ## F7 — skonfiguruj ProxySQL (mysql_galera_hostgroups)
	$(cluster_guard)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	@: "$${PROXYSQL_MONITOR_PASSWORD:?Ustaw PROXYSQL_MONITOR_PASSWORD poza repozytorium}"
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f7_proxysql.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-proxysql-verify:  ## Zweryfikuj routing ProxySQL (ISC-18/19/20/21/22/23)
	$(cluster_guard)
	$(TARGET_ENV) tests/lab/probe-proxysql.py

# `cluster-endpoint` USUNIETY 2026-08-21. VIP .139 (dawniej .133) nalezy do warstwy wspolnej,
# a ten cel pozwalal dowolnemu najemcy odpalic Keepalived na cudzej parze —
# na newclaude17-r9 wyjdzie to jako `changed=0`, ale bramki nie bylo zadnej.
# Zastapiony przez `make platform-endpoint`; f8_keepalived.yml odrzuca teraz
# definicje klastra asercja fail-closed.

lab-endpoint-verify:  ## Zweryfikuj endpoint VIP ProxySQL (ISC-24/26)
	$(cluster_guard)
	$(TARGET_ENV) tests/lab/probe-endpoint.py

lab-failover-test:  ## F9 — test failover writera (ISC-27/28, lab-only, destrukcyjny)
	$(cluster_guard)
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

lab-failover-hard-test:  ## F9 — failover przy TWARDEJ utracie maszyny (sysrq, lab-only, destrukcyjny)
	$(cluster_guard)
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
	$(cluster_guard)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	$(TARGET_ENV) APP_DB_PASSWORD="$${APP_DB_PASSWORD}" tests/lab/probe-app-conformance.py

# Pomiar Z HOSTA APLIKACYJNEGO, nie z wezla klastra: wczesniejsze benchmarki
# szly z hosta `restore`, ktory dzieli CPU i siec z warstwa bazodanowa.
lab-app-bench:  ## Zmierz przepustowosc z hosta `app` (direct vs VIP, TLS vs plaintext)
	$(cluster_guard)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	$(TARGET_ENV) APP_DB_PASSWORD="$${APP_DB_PASSWORD}" tests/lab/bench-app.py

# Sondy stanu ustalonego mowia, ze wszystko dziala, dopoki wszystko dziala.
# Ta sprawdza, co aplikacja widzi przy utracie kworum: czy zapis zostaje
# odrzucony (bezpieczenstwo) i czy blad da sie odroznic od awarii sieci.
lab-app-degradation-test:  ## P2 quorum loss (destrukcyjny, wymaga CLUSTER i CONFIRM=yes)
	$(cluster_guard)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	@: "$${QUORUM_RUN_ID:?Ustaw unikalny QUORUM_RUN_ID (32 hex)}"
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (SIGKILL na wezlach $(CLUSTER))"; exit 1)
	$(TARGET_ENV) APP_DB_PASSWORD="$${APP_DB_PASSWORD}" \
	  QUORUM_RUN_ID="$${QUORUM_RUN_ID}" \
	  CONFIRM="$${CONFIRM}" \
	  APP_QUORUM_ERROR_CONTRACT="$${APP_QUORUM_ERROR_CONTRACT:-degraded}" \
	  tests/lab/chaos-app-degradation.py

# Caly pozostaly chaos celuje w Galere. Ta sprawdza WARSTWE POSREDNIA — ta, przez
# ktora aplikacja faktycznie chodzi. Tryb `service` (domyslny) zabija sam proces
# ProxySQL i zostawia keepalived przy zyciu: VRRP nie widzi wtedy nic, wiec VIP
# moze zabrac WYLACZNIE vrrp_script chk_proxysql. Tryb `node` gasi cala maszyne
# przez API Proxmoksa i sprawdza klasyczny VRRP.
lab-proxysql-failover-test:  ## Awaria wezla ProxySQL — przelaczenie VIP, ciaglosc, brak utraty (lab-only)
	$(cluster_guard)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	$(TARGET_ENV) APP_DB_PASSWORD="$${APP_DB_PASSWORD}" \
	  PROXYSQL_FAILOVER_MODE="$${PROXYSQL_FAILOVER_MODE:-service}" \
	  PROXYSQL_VMIDS="$${PROXYSQL_VMIDS:-}" \
	  PROXMOX_VE_API_TOKEN="$${PROXMOX_VE_API_TOKEN:-}" \
	  tests/lab/chaos-proxysql-failover.py

lab-split-brain-test:  ## F9 — test split-brain / partycji sieci (ISC-30, lab-only, destrukcyjny)
	$(cluster_guard)
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

# Katalog w roles/ bez tasks/main.yml jest dla Ansible poprawna, PUSTA rola:
# `roles: mariadb_install` konczy sie rc=0 i zerem zadan. Ta sonda nie pozwala
# zamienic literowki w nazwie roli w cicha zgode.
verify-role-contract:  ## Statyczny guard: katalog w roles/ to rola albo assety, nigdy cicha atrapa
	python3 tests/validation/probe-role-contract.py

# Adres hypervisora bierze sie z PROXMOX_VE_ENDPOINT, wiec ta czesc dziala tylko
# lokalnie; kolizje miedzy klastrami i z VIP-em sa sprawdzane zawsze, takze w CI.
verify-address-collision:  ## Statyczny guard: adresy wezlow nie kolidują z hypervisorem, innym klastrem ani VIP-em
	python3 tests/validation/probe-address-collision.py

verify-no-secrets-leak:  ## Statyczny guard: brak sekretów w repo i argv procesów
	bash tests/validation/probe-no-secrets-leak.sh

# CI ma ten krok od dawna, lokalnie go nie bylo - martwy import w tescie
# przechodzil przez pelny `unittest discover` i padal dopiero po pushu.
# Wersja przypieta identycznie jak w .github/workflows/ci.yml.
verify-dead-code:  ## Statyczny guard: martwe importy, nieuzywane zmienne, puste f-stringi (pyflakes)
	python3 -m pyflakes tests roles/galera_backup/filter_plugins roles/galera_backup/files/galera_backup

verify-no-state-latest:  ## Statyczny guard: brak state: latest w rolach i playbookach (ISC-63)
	bash tests/validation/probe-no-state-latest.sh

# `node`, nie `deno`: CI ma node w obrazie, deno wymagaloby dodatkowego kroku.
# Node >= 22.18 zdejmuje typy sam, wiec plik .ts idzie bez transpilacji.
verify-docs-fetch-hook:  ## Statyczny guard: hook blokujacy scraping dokumentacji przez curl
	node tests/validation/probe-docs-fetch-hook.ts

cluster-backup-configure:  ## F10 — skonfiguruj runner, minio identity i cron dla klastra (gdy backup.enabled)
	$(cluster_guard)
	@if [ "$(backup_enabled)" != "true" ]; then $(backup_skip_note); exit 0; fi; \
	set -e; \
	ansible-playbook playbooks/f10_backup.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e galera_backup_action=configure $(ANSIBLE_OPTS)

cluster-backup:  ## F10 — backup → destination storage via galera-backup runner (gdy backup.enabled)
	$(cluster_guard)
	@if [ "$(backup_enabled)" != "true" ]; then $(backup_skip_note); exit 0; fi; \
	set -e; \
	ansible-playbook playbooks/f10_backup.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e galera_backup_action=run $(ANSIBLE_OPTS)

lab-seed-smoke:  ## LAB — zasiej minimalne dane user-space, bez których drill restore pada
	$(cluster_guard)
	ansible-playbook playbooks/lab_seed_smoke.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

cluster-restore-drill:  ## F10 — restore drill na czysty host + integralność (CONFIRM=yes, gdy backup.enabled)
	$(cluster_guard)
	@if [ "$(backup_enabled)" != "true" ]; then $(backup_skip_note); exit 0; fi; \
	test "$(CONFIRM)" = "yes" || { echo "Wymaga CONFIRM=yes (drill kasuje datadir hosta grupy restore)" >&2; exit 1; }; \
	set -e; \
	ansible-playbook playbooks/f10_restore.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e restore_confirm=yes $(ANSIBLE_OPTS)

lab-backup-verify:  ## F10 — zweryfikuj backup w S3 (ISC-32/33/34/35)
	$(cluster_guard)
	$(TARGET_ENV) tests/lab/probe-backup.py

lab-restore-verify:  ## F10 — zweryfikuj stan restore drill (ISC-36/37)
	$(cluster_guard)
	$(TARGET_ENV) tests/lab/probe-restore.py

lab-backup-impact:  ## F10 — backup pod obciążeniem nie degraduje writera (ISC-39, lab-only)
	$(cluster_guard)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	$(TARGET_ENV) tests/lab/backup-impact.py

cluster-harden:  ## F6 — hardening MariaDB: usuń anon/test, root localhost-only, least privilege
	$(cluster_guard)
	ansible-playbook playbooks/f6_hardening.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-hardening-verify:  ## Zweryfikuj hardening MariaDB (ISC-40/41/42)
	$(cluster_guard)
	$(TARGET_ENV) tests/lab/probe-hardening.py

# Najemca rejestruje WYLACZNIE wlasne wezly. Eksportery ProxySQL (fcp1/fcp2)
# rejestruje `make platform-monitoring` — nalezą do warstwy wspolnej, a gdy
# robil to najemca, deregistracja tego klastra zabierala monitoring calej pary.
# Z tego samego powodu znika stad straznik PROXYSQL_ADMIN_PASSWORD: zadny
# z pozostalych krokow nie laczy sie juz z portem admina ProxySQL.
cluster-monitoring:  ## F11 — zarejestruj hosty i usługi w natywnym PMM Inventory (gdy monitoring.enabled)
	$(cluster_guard)
	@if [ "$(monitoring_enabled)" != "true" ]; then $(monitoring_skip_note); exit 0; fi; \
	set -e; \
	: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"; \
	: "$${PMM_MONITOR_PASSWORD:?Ustaw PMM_MONITOR_PASSWORD poza repozytorium}"; \
	ansible-playbook playbooks/f11_node_exporter.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS); \
	ansible-playbook playbooks/f11_pmm_agent.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS); \
	ansible-playbook playbooks/f11_pmm_client.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS); \
	ansible-playbook playbooks/f11_freshness.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS); \
	ansible-playbook playbooks/f11_log_lifecycle.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

cluster-monitoring-refresh:  ## F11 — odśwież metryki świeżości (po backup/restore)
	$(cluster_guard)
	@if [ "$(monitoring_enabled)" != "true" ]; then $(monitoring_skip_note); exit 0; fi; \
	ansible-playbook playbooks/f11_freshness.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-monitoring-verify:  ## Zweryfikuj natywne PMM Inventory i metryki laboratorium
	$(cluster_guard)
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	$(TARGET_ENV) PMM_ADMIN_PASSWORD="$${PMM_ADMIN_PASSWORD}" tests/lab/probe-pmm-native.py


cluster-rolling-restart:  ## F12 — rolling restart Galera serial:1 + brama zdrowia (ISC-50/51)
	$(cluster_guard)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f12_rolling_restart.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-rolling-restart-verify:  ## F12 — zweryfikuj rolling restart (ISC-50/51)
	$(cluster_guard)
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
# Wezel wybrany przez playbooks/cluster_recover.yml. Plik stanu jest artefaktem
# JEDNEGO przebiegu, nie pamiecia ostatniego sukcesu: cel usuwa poprzedni plik,
# generuje run_id, playbook zapisuje JSON {run_id, generated_at, node}, a
# verifier porownuje run_id i czlonkostwo node w grupie galera. Samo `test -s`
# przepuszczalo stary wybor, gdy Ansible zwrocil rc=0 bez wykonania play.
RECOVER_STATE_FILE = $(CURDIR)/clusters/$(CLUSTER)/recover-bootstrap-node
RECOVER_NODE_FILE = $(RECOVER_STATE_FILE).validated
cluster-recover: RECOVER_RUN_ID := $(shell python3 -c 'import uuid; print(uuid.uuid4())')
cluster-recover:  ## Cold recovery Galera: serialny stop + bezpieczny bootstrap + join (CLUSTER+CONFIRM=yes; BOOTSTRAP_NODE przy remisie)
	$(cluster_guard)
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (cold recovery zatrzymuje caly klaster)"; exit 1)
	@: "$${SST_PASSWORD:?Ustaw SST_PASSWORD poza repozytorium}"
	@rm -f "$(RECOVER_STATE_FILE)" "$(RECOVER_NODE_FILE)" "$(RECOVER_NODE_FILE).tmp"
	ansible-playbook playbooks/cluster_recover.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e confirm=yes -e recover_state_file="$(RECOVER_STATE_FILE)" -e recover_run_id="$(RECOVER_RUN_ID)" $(if $(BOOTSTRAP_NODE),-e recover_bootstrap_node=$(BOOTSTRAP_NODE)) $(ANSIBLE_OPTS)
	@python3 tests/validation/verify-recovery-state.py "$(RECOVER_STATE_FILE)" "$(RECOVER_RUN_ID)" "clusters/$(CLUSTER)/inventory.yml" > "$(RECOVER_NODE_FILE).tmp"
	@mv "$(RECOVER_NODE_FILE).tmp" "$(RECOVER_NODE_FILE)"
	@echo "Bootstrap po recovery: $$(cat "$(RECOVER_NODE_FILE)") (kanoniczny playbooks/bootstrap.yml)"
	$(MAKE) cluster-bootstrap ANSIBLE_OPTS="$(ANSIBLE_OPTS) -e bootstrap_node=$$(cat "$(RECOVER_NODE_FILE)") -e bootstrap_confirm_all_down=true"
	$(MAKE) cluster-join ANSIBLE_OPTS="$(ANSIBLE_OPTS) -e join_bootstrap_node=$$(cat "$(RECOVER_NODE_FILE)")"
	$(MAKE) cluster-health

cluster-upgrade-plan:  ## F12 — wygeneruj read-only plan major upgrade (ISC-53/54/56)
	$(cluster_guard)
	ansible-playbook playbooks/f12_upgrade_plan.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-upgrade-plan-verify:  ## F12 — zweryfikuj plan major upgrade (ISC-53/54/56)
	$(cluster_guard)
	$(TARGET_ENV) tests/lab/probe-upgrade-plan.py

cluster-patch:  ## F12 — rolling patch z canary + brama zdrowia (ISC-52/55/57)
	$(cluster_guard)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f12_patch.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-patch-verify:  ## F12 — zweryfikuj wzorzec canary patch (ISC-52/55/57)
	$(cluster_guard)
	$(TARGET_ENV) tests/lab/probe-patch.py

cluster-drift:  ## F13 — read-only raport dryfu konfiguracji (ISC-21)
	$(cluster_guard)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	ansible-playbook playbooks/f13_drift.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-drift-verify:  ## F13 — zweryfikuj drift detection (ISC-21)
	$(cluster_guard)
	$(TARGET_ENV) tests/lab/probe-drift.py

cluster-remove-node-plan:  ## F13 — read-only plan usunięcia węzła Galera (wymaga NODE=<nazwa_wezla>)
	$(cluster_guard)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	@test -n "$(NODE)" || (echo "Ustaw NODE=<nazwa_wezla> (np. make cluster-remove-node-plan NODE=grg2)"; exit 1)
	ansible-playbook playbooks/f13_remove_node_plan.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e node=$(NODE) $(ANSIBLE_OPTS)

cluster-remove-node:  ## F13 — usuń węzeł Galera (confirm-gated, wymaga NODE=<nazwa_wezla> CONFIRM=yes)
	$(cluster_guard)
	@: "$${PROXYSQL_ADMIN_PASSWORD:?Ustaw PROXYSQL_ADMIN_PASSWORD poza repozytorium}"
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (destrukcyjne)"; exit 1)
	@test -n "$(NODE)" || (echo "Ustaw NODE=<nazwa_wezla>"; exit 1)
	ansible-playbook playbooks/f13_remove_node.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e node=$(NODE) -e confirm=yes $(ANSIBLE_OPTS)

cluster-alerts:  ## F15 — reguly alertowe ISC-47 (gdy monitoring.enabled)
	$(cluster_guard)
	@if [ "$(monitoring_enabled)" != "true" ]; then $(monitoring_skip_note); exit 0; fi; \
	: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"; \
	ansible-playbook playbooks/f15_alerts.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml $(ANSIBLE_OPTS)

lab-gcache-verify:  ## F0/ISC-68 — zmierz write rate + weryfikuj gcache.size (IST window)
	$(cluster_guard)
	$(TARGET_ENV) tests/lab/probe-gcache.py

# Jedno polecenie po zbudowaniu klastra: wszystkie sondy STANU USTALONEGO.
# Kazda sonda jest fail-closed (tests/lab/_probe_common.py): brak odpowiedzi
# hosta to UNDETERMINED (exit 2), nie zielone "wszystko OK". Pierwszy niezerowy
# kod konczy bramke — nie ma sensu mierzyc dalej na klastrze, ktory nie
# przeszedl kontraktu.
lab-post-build-gate:  ## Bramka po budowie: wszystkie sondy stanu ustalonego, fail-closed
	$(cluster_guard)
	@# Straznicy sekretow PRZED sondami: brak zmiennej wychodzil dopiero w 13. sondzie,
	@# po kilkunastu minutach pracy calej bramki. Kazdy sekret uzywany nizej jest
	@# sprawdzany tutaj, nawet jesli jego sonda stoi na koncu listy.
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	@: "$${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"
	@# PIERWSZA, nie ostatnia: uruchamia converge po raz drugi, wiec wszystkie
	@# sondy ponizej mierza stan JUZ PO nim. Odwrotna kolejnosc dawalaby zielone
	@# swiatlo stanowi, ktorego nikt potem nie sprawdzil. CoP stawia ten warunek
	@# bezwarunkowo, a repo mialo tu dziure mimo 501 testow jednostkowych.
	$(TARGET_ENV) tests/lab/probe-idempotence.py
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
	$(TARGET_ENV) PMM_ADMIN_PASSWORD="$${PMM_ADMIN_PASSWORD}" tests/lab/probe-pmm-native.py
	@echo "PASS: brama po budowie — wszystkie sondy stanu ustalonego zmierzone i zielone"
