# Galera + ProxySQL Cluster Factory

Powtarzalna, idempotentna fabryka produkcyjnych klastrów MariaDB Galera z ProxySQL na istniejących hostach Rocky Linux 9 i 10.

**Status: faza BUILD — wspólny kod ról działa na Rocky Linux 9 i 10. Galera, ProxySQL/Keepalived, hardening, monitoring oraz szyfrowany backup/restore zostały sprawdzone na rzeczywistych klastrach. Backup obsługuje S3, zarządzany SMB i wcześniej zamontowany filesystem; scheduler, retencja, sekrety i artefakty są izolowane per klaster.**

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
export GALERA_BACKUP_ENCRYPTION_KEY='<klucz-szyfrowania-backupu>'
export MINIO_ROOT_USER='labbackup'
export MINIO_ROOT_PASSWORD='<minio-s3-secret>'
# Alternatywnie załaduj lokalny, ignorowany plik utworzony dla działającego labu:
# set -a; . tests/lab/.env; set +a

# Uruchom lab; PMM na czystym volume dostaje powyższe hasło, a orphans są usuwane
make lab-up

# Discovery i walidacja wybranego klastra
make cluster-discover CLUSTER=lab-cluster
make cluster-validate CLUSTER=lab-cluster

# Materiał TLS (gdy tls.mode=full). Artefakty są gitignorowane — na nowej
# maszynie trzeba je wytworzyć, inaczej F3/F7 padną na brakującym pliku.
#   1) CA + cert KLASTRA (ścieżki z tls.*_reference w cluster.yml); SAN musi
#      pokrywać nazwy ORAZ adresy węzłów — Galera łączy się po adresie.
tests/lab/tls/generate.sh <klaster> <n1,n2,n3,ip1,ip2,ip3>
#   2) CA + cert WSPÓLNEGO endpointu ProxySQL (proxysql.frontend_tls). CA jest
#      wspólne dla całej floty, nie klastrowe: jedna para ProxySQL serwuje
#      wszystkie klastry JEDNYM certem frontendu. Wdraża go wyłącznie owner;
#      SAN musi pokrywać VIP i adresy węzłów ProxySQL.
tests/lab/tls/generate.sh shared-proxysql fcp1,fcp2,<ip-fcp1>,<ip-fcp2>,<ip-vip>
#   Rotacja liścia pod tym samym CA: REUSE_CA=1 przed powyższym poleceniem,
#   potem `make cluster-tls-rotate CLUSTER=<klaster>` (węzły) albo
#   `make cluster-proxysql CLUSTER=<owner>` (frontend — PROXYSQL RELOAD TLS,
#   bez zrywania istniejących sesji). Rotację CA prowadzi tests/lab/tls/rotate-ca.sh.

# UWAGA kolejność: poniższa sekwencja jest zweryfikowana od zera (from-scratch).
# Zależności, które ją wymuszają:
#   F6 asertuje granty pmm_monitor  -> musi być PO F11
#   F11 rejestruje metryki ProxySQL -> musi być PO F7
#   F10 restore drill wymaga danych -> musi być PO F9 (workload zasiewa isa_test);
#     na klastrze bez testów chaos zasiej je jawnie: make lab-seed-smoke CLUSTER=<name>
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

# F10 — konfiguracja schedulera, ręczny backup i potwierdzany restore
make cluster-backup-configure CLUSTER=lab-cluster
make cluster-backup CLUSTER=lab-cluster                  # ISC-32/33/34/35
make lab-backup-verify CLUSTER=lab-cluster               # S3: szyfr/checksum/metadata
make cluster-restore-drill CLUSTER=lab-cluster CONFIRM=yes
make lab-restore-verify CLUSTER=lab-cluster               # integralność i świeżość
make lab-backup-impact CLUSTER=lab-cluster                # ISC-39, lab-only
#
# Backend, scheduler, sekrety, rotacja i diagnostyka:
# docs/runbooks/backup.md

# F15 — reguły alertów (ISC-47); adres e-mail z monitoring.alerts.email w cluster.yml
make cluster-alerts CLUSTER=lab-cluster

# Kontrakt końcowy: PMM 3.8.1, 5 nodes, 5 node_exporter 1.12.1, 3 MySQL services,
# QAN, świeże metryki Galery/lifecycle ORAZ reguły ISC-47
make cluster-monitoring-refresh CLUSTER=lab-cluster
make lab-monitoring-verify
```

PMM UI laboratorium: `http://127.0.0.1:8080`. Stan usług w PMM jest diagnostyczny: `Down` oznacza rzeczywiście nieosiągalną usługę, a nie błąd rejestracji.
`GF_SECURITY_ADMIN_PASSWORD` inicjalizuje tylko czysty `pmm-data`; istniejący volume zachowuje zapisane hasło. Rotację wykonaj w PMM UI, po czym ustaw tę samą wartość w `PMM_ADMIN_PASSWORD`.
Alerting (F15) jest wdrożony: `make cluster-alerts` provisionuje 6 reguł (node loss, quorum loss, node not Synced, brak writera, ostatni backup nieudany, backup przeterminowany). Krytyczne reguły używają `noDataState: Alerting`; brak metryk nie przechodzi cicho. Contact point i notification policy (`managed_by=ansible` → e-mail) biorą adres z `monitoring.alerts.email` w `cluster.yml`. W laboratorium poczta trafia do `maildev`.

`lab-backup-verify` weryfikuje backend S3 i wymaga przypiętego SDK (`minio.sdk_version` z lockfile). Zarządzany SMB oraz wcześniej zamontowany filesystem weryfikuje `tests/live/probe-galera-backup-backends.py`; procedury i ograniczenia opisuje `docs/runbooks/backup.md`.

## Żywa flota

Szybki start powyżej uczy na `lab-cluster` (kontenery). Realne klastry są dwa i
chodzą na **tym samym kodzie** — różnicę niesie wyłącznie `versions.lock_file`
w `cluster.yml`:

| Klaster | OS | Węzły | TLS | Hostgroupy ProxySQL |
|---|---|---|---|---|
| `finalclaude-r9` | Rocky 9 | `f9g1-3` + `f9r1` (restore) | `full`, SST szyfrowany | 110/120/130/140 |
| `finalclaude-r10` | Rocky 10 | `f10g1-3` + `f10r1` | `disabled` (kontrast platformowy) | 10/20/30/40 |

Warstwa wspólna dla obu: `fcp1`/`fcp2` (ProxySQL w HA, VIP `192.168.1.133:6033`)
oraz `fcinfra` (PMM, MinIO, maildev). Jedna para ProxySQL obsługuje całą flotę,
a klastry rozdziela wyłącznie rozłączność hostgroup i użytkowników — pilnuje jej
sonda `make verify-proxysql-tenancy`.

Każda komenda ze Szybkiego startu działa na nich przez `CLUSTER=<nazwa>`, np.
`make cluster-backup CLUSTER=finalclaude-r9`. Aktualny stan maszyn, adresy i
dowody z żywej instalacji: `docs/infrastructure-state.md`.

## Struktura

```
clusters/<name>/     — inventory.yml + cluster.yml + secrets per klaster
versions/            — lockfile, discovered-versions, compatibility-report
profiles/            — production/staging/laboratory
playbooks/           — feature po feature (F0-F15) + tasks/ z helperami wspoldzielonymi
roles/               — standardowe katalogi, gdy potrzebne
  galera_backup/files/
    galera-backup           — cienki wrapper (21 linii), wdrazany na scheduler
    galera_backup/          — pakiet: pipeline, storage/{s3,filesystem}, config,
                              runner, state, locking, secrets, fsutil, textutil
tests/               — unit/ (138), validation/ (sondy statyczne, blokujace w CI),
                       lab/ (sondy przeciw zywemu klastrowi), live/
docs/                — adr, runbooks, plans, records, stan infrastruktury
```

Nowy klaster = nowy katalog `clusters/<name>/`. Kod ról nie zawiera danych klastra.

## Kontrakt

Pełny kontrakt pracy, format ISA i wymagania w `MASTER_PROMPT.md`.
Bieżący stan projektu w `ISA.md` (log decyzji na końcu pliku).
Stan infrastruktury — maszyny, klastry, rozbieżności — w `docs/infrastructure-state.md`.
