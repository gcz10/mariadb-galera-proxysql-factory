# Galera + ProxySQL Cluster Factory

Powtarzalna, idempotentna fabryka produkcyjnych klastrów MariaDB Galera z ProxySQL na istniejących hostach Rocky Linux 9.

**Status: faza BUILD — laboratorium 5× Rocky Linux 9 (+ MinIO + host restore) działa; 3-węzłowy klaster Galera (mariabackup SST, IST) jest zdrowy i Synced. MariaDB zahardenowana (bez anon/test, root localhost-only, least privilege). ProxySQL na obu węzłach routuje ruch przez `mysql_galera_hostgroups` (jeden aktywny writer, R/W split wyłączony), a redundantny endpoint Keepalived VIP przełącza się przy awarii ProxySQL w kilka sekund. Testy chaos potwierdzają failover writera bez utraty transakcji i brak split-brain. Backup jest szyfrowany, off-cluster (S3/MinIO), z checksumą i metadanymi wsrep; restore drill odtwarza na czysty host z testem integralności. PMM monitoruje wszystkie hosty, trzy usługi MariaDB i QAN.**

Zobacz `ISA.md` — jedyne źródło prawdy dla idealnego stanu, kryteriów, mapy testów i postępu.

## Szybki start

```bash
# Sekrety laboratorium pozostają wyłącznie w środowisku
export PMM_ADMIN_PASSWORD='<pmm-admin-password>'
export PMM_MONITOR_PASSWORD='<mariadb-monitor-password>'
export PROXYSQL_ADMIN_PASSWORD='<proxysql-admin-password>'
export PROXYSQL_MONITOR_PASSWORD='<proxysql-galera-monitor-password>'
export APP_DB_PASSWORD='<application-db-password>'
export KEEPALIVED_AUTH_PASS='<vrrp-8-znakowe-haslo>'
export BACKUP_ENCRYPTION_KEY='<klucz-szyfrowania-backupu>'
export MINIO_ROOT_USER='labbackup'
export MINIO_ROOT_PASSWORD='<minio-s3-secret>'
# Alternatywnie załaduj lokalny, ignorowany plik utworzony dla działającego labu:
# set -a; . tests/lab/.env; set +a

# Uruchom lab; PMM na czystym volume dostaje powyższe hasło, a orphans są usuwane
make lab-up

# Discovery i walidacja wybranego klastra
make cluster-discover CLUSTER=lab-cluster
make cluster-validate CLUSTER=lab-cluster

# UWAGA kolejność: poniższa sekwencja jest zweryfikowana od zera (from-scratch).
# Zależności, które ją wymuszają:
#   F6 asertuje granty pmm_monitor  -> musi być PO F11
#   F11 rejestruje metryki ProxySQL -> musi być PO F7
#   F10 restore drill wymaga danych -> musi być PO F9 (workload zasiewa isa_test)
#   lab-monitoring-verify sprawdza świeżość backupu i reguły -> na samym końcu

# F2+F3 — pakiety (wersje z versions.lock.yml) + konfiguracja
make cluster-deploy CLUSTER=lab-cluster

# F4 — initial bootstrap: JEDEN węzeł (galera[0]), wymaga jawnego CONFIRM=yes
make cluster-bootstrap CLUSTER=lab-cluster CONFIRM=yes

# F5 — dołącz pozostałe węzły (SST mariabackup, serial:1)
make cluster-join CLUSTER=lab-cluster
make lab-galera-verify

# F7 — ProxySQL (mysql_galera_hostgroups, jeden aktywny writer)
make cluster-proxysql CLUSTER=lab-cluster
make lab-proxysql-verify

# F11 — monitoring: node_exporter, PMM Inventory/QAN, metryki ProxySQL, logrotate
make cluster-monitoring CLUSTER=lab-cluster
# Przy każdej rotacji PMM_MONITOR_PASSWORD zwiększ także
# monitoring.pmm.credentials_revision w cluster.yml.

# F6 — hardening MariaDB (wymaga konta pmm_monitor z F11)
make cluster-harden CLUSTER=lab-cluster
make lab-hardening-verify

# F8 — redundantny endpoint (Keepalived VIP, unicast VRRP, failover < RTO)
make cluster-endpoint CLUSTER=lab-cluster
make lab-endpoint-verify

# F9 — testy chaos (destrukcyjne, tylko poza produkcją); zasiewają też isa_test
make lab-failover-test CLUSTER=lab-cluster      # ISC-27/28: kill writera, brak utraty tx
make lab-split-brain-test CLUSTER=lab-cluster   # ISC-30: partycja sieci, jeden Primary
make verify-no-mass-restart                     # ISC-31: brak masowego restartu Galery

# F10 — backup / restore (off-cluster S3, szyfrowany, restore drill)
make cluster-backup CLUSTER=lab-cluster         # ISC-32/33/34/35: backup → S3
make lab-backup-verify                          # weryfikacja szyfr/checksum/metadata
make cluster-restore-drill CLUSTER=lab-cluster  # ISC-36: restore na czysty host rnode1
make lab-restore-verify                         # ISC-37: świeżość drilla wg harmonogramu
make lab-backup-impact CLUSTER=lab-cluster      # ISC-39: backup pod obciążeniem nie degraduje writera

# F15 — reguły alertów (ISC-47); adres e-mail z monitoring.alerts.email w cluster.yml
make cluster-alerts CLUSTER=lab-cluster

# Kontrakt końcowy: PMM 3.8.1, 5 nodes, 5 node_exporter 1.12.1, 3 MySQL services,
# QAN, świeże metryki Galery/lifecycle ORAZ reguły ISC-47
make cluster-monitoring-refresh CLUSTER=lab-cluster
make lab-monitoring-verify
```

PMM UI laboratorium: `http://127.0.0.1:8080`. Stan usług w PMM jest diagnostyczny: `Down` oznacza rzeczywiście nieosiągalną usługę, a nie błąd rejestracji.
`GF_SECURITY_ADMIN_PASSWORD` inicjalizuje tylko czysty `pmm-data`; istniejący volume zachowuje zapisane hasło. Rotację wykonaj w PMM UI, po czym ustaw tę samą wartość w `PMM_ADMIN_PASSWORD`.
Alerting (F15) jest wdrożony: `make cluster-alerts` provisionuje 4 reguły ISC-47 (node loss, quorum loss, node not Synced, backup stale) w folderze „ISA Alerts", z `for: 2m` i `noDataState: Alerting` — martwy węzeł, który przestaje raportować metryki, alarmuje zamiast milczeć. Reguły są opóźnione o 2 minuty, żeby planowany rolling restart nie generował fałszywych alarmów. Contact point i notification policy (`managed_by=ansible` → e-mail) biorą adres z `monitoring.alerts.email` w `cluster.yml`; w laboratorium poczta trafia do kontenera `maildev`. PMM używa ograniczonego drivera logów Docker (`10m × 3`); hosty z systemd dostają timer logrotate, a laboratorium bez systemd waliduje tę samą politykę bez uruchamiania timera.

Sondy backupu (`probe-backup.py`) używają klienta S3 — zainstaluj przypięty SDK na maszynie uruchamiającej testy: `pip install minio==7.2.7`. Off-cluster backup w labie to MinIO (S3); produkcja używa SMB (kernel OrbStack nie ma modułu cifs — patrz Decisions w ISA).

## Struktura

```
clusters/<name>/     — inventory.yml + cluster.yml + secrets per klaster
versions/            — lockfile, discovered-versions, compatibility-report
profiles/            — production/staging/laboratory
playbooks/           — feature po feature
roles/               — standardowe katalogi, gdy potrzebne
tests/               — integration/idempotence/failure/recovery/upgrade/validation
docs/                — architecture, adr, runbooks
```

Nowy klaster = nowy katalog `clusters/<name>/`. Kod ról nie zawiera danych klastra.

## Kontrakt

Pełny kontrakt pracy, format ISA i wymagania w `MASTER_PROMPT.md`.
Bieżący stan projektu w `ISA.md`.
