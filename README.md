# Galera + ProxySQL Cluster Factory

Powtarzalna, idempotentna fabryka produkcyjnych klastrów MariaDB Galera z ProxySQL na istniejących hostach Rocky Linux 9 i 10.

**Status: faza BUILD — wspólny kod ról działa na Rocky Linux 9 i 10. Galera, ProxySQL/Keepalived, hardening, monitoring oraz szyfrowany backup/restore zostały sprawdzone na rzeczywistych klastrach. Backup obsługuje S3, zarządzany SMB i wcześniej zamontowany filesystem; scheduler, retencja, sekrety i artefakty są izolowane per klaster.**

Zobacz `ISA.md` — jedyne źródło prawdy dla idealnego stanu, kryteriów, mapy testów i postępu.

## Czego wymagają maszyny

Dostępu SSH, systemd, Rocky Linux 9 albo 10 i konta z sudo. **Nic ponadto.**
Skąd pochodzą — Proxmox, libvirt/KVM, blaszak, chmura, maszyny dostarczone przez
klienta — jest bez znaczenia dla fabryki.

`make infra-provision` (Terraform + Proxmox VE) tworzy maszyny w tym konkretnym
laboratorium i jest **opcjonalny**: żaden cel wołany przez `cluster-build` ani
`platform-build` go nie uruchamia i żaden nie czyta stanu Terraforma. Cała wiedza
o topologii pochodzi z `clusters/<name>/inventory.yml` i `platform/<name>/inventory.yml`.

Zweryfikowane, nie zadeklarowane: najemca powstał na maszynach utworzonych
wyłącznie przez REST API hypervisora, a cała budowa przebiegła z atrapą
`terraform` na początku `PATH`, która przy każdym wywołaniu kończy się błędem.
Nie strzeliła ani razu. Procedurę dla maszyn spoza Terraforma opisuje
`docs/runbooks/machines-from-elsewhere.md`.

Symetrycznie do tego: **cele niszczące maszyny są terraformowe** i wymagają
katalogu `terraform/<nazwa>`. Skoro fabryka nie tworzy Twoich maszyn, nie kasuje
ich też za Ciebie — pełny cykl dla maszyn z innego źródła opisuje
[runbook](docs/runbooks/machines-from-elsewhere.md). Warstwę logiczną fabryka
sprząta po sobie zawsze: `make cluster-deregister` usuwa najemcę z ProxySQL
i PMM niezależnie od pochodzenia maszyn.

### Własne hosty, krok po kroku

Kolejność poniżej odtwarza budowę, którą przeszedł ten repozytorium na świeżych
maszynach. Każdy krok, którego brak zatrzymał tamten przebieg, jest tu wypisany —
łącznie z tymi, które wyglądają na oczywiste.

```bash
# 0. Sekrety. Fabryka nie trzyma ich w repozytorium i odmawia startu bez nich.
#    Minimum 12 znaków (polityka: playbooks/vars/secret_policy.yml).
#    WYJĄTEK: KEEPALIVED_AUTH_PASS ma DOKŁADNIE do 8 znaków — VRRP używa tylko
#    tylu, więc dłuższe hasło jest odrzucane jako mylące.
export PMM_ADMIN_PASSWORD='...'         # gdy monitoring.enabled (domyślnie tak)
export PMM_MONITOR_PASSWORD='...'
export PROXYSQL_ADMIN_PASSWORD='...'
export PROXYSQL_STATS_PASSWORD='...'
export PROXYSQL_MONITOR_PASSWORD='...'
export APP_DB_PASSWORD='...'
export KEEPALIVED_AUTH_PASS='8znakow'
export SST_PASSWORD='...'

# 1. Warstwa wspólna: para ProxySQL z VIP-em, opcjonalnie PMM i magazyn kopii.
cp -r platform/example platform/<nazwa>
$EDITOR platform/<nazwa>/inventory.yml   # adresy Twoich maszyn
$EDITOR platform/<nazwa>/platform.yml    # VIP, `infra.services`, PMM, ingress

# 2. Najemca: węzły bazy. Gdy dokładasz klaster do ISTNIEJĄCEJ platformy,
#    pomijasz krok 1 i wskazujesz jej ProxySQL/VIP/PMM/CA w tej definicji.
cp -r clusters/example-cluster clusters/<nazwa>
$EDITOR clusters/<nazwa>/inventory.yml
$EDITOR clusters/<nazwa>/cluster.yml

# 3. Zaufanie kluczom hostów. Inwentarze wymuszają StrictHostKeyChecking=yes,
#    więc BEZ tego kroku Ansible zgłosi UNREACHABLE na każdej maszynie.
make platform-trust-hosts PLATFORM=<nazwa>
make cluster-trust-hosts CLUSTER=<nazwa>

# 4. Materiał TLS (`tls.mode=full` jest bezpiecznym defaultem). Dwa CA:
#    endpointu wspólnej platformy i własne każdego klastra.
pki/generate.sh <platforma> <p1,p2,ip-p1,ip-p2,ip-vip>
pki/generate.sh <klaster> <g1,g2,g3,ip-g1,ip-g2,ip-g3>
pki/issue-node-certs.sh <klaster> <g1=ip-g1,g2=ip-g2,g3=ip-g3>

# 5. Bramki PRZED zmianą maszyn: schemat i inventory są statyczne, potem
#    read-only SSH preflight sprawdza system i wersję Rocky Linux.
make cluster-validate CLUSTER=<nazwa>

# 6. Budowa. Na świeżym obrazie chmurowym pierwszy przebieg potrafi zażądać
#    restartu do zainstalowanego kernela — playbook powie to wprost.
make platform-build PLATFORM=<nazwa> ANSIBLE_OPTS="-e allow_kernel_reboot=yes"
make cluster-build CLUSTER=<nazwa> CONFIRM=yes
```

Po skopiowaniu szablonu uzupełnij **całą** listę poniżej. `cluster-validate`
odrzuca teraz placeholdery, puste CIDR-y, brak materiału TLS i niepełne
`known_hosts`; wcześniej część z nich wychodziła dopiero po kilku minutach
budowy.

**`platform/<nazwa>/platform.yml` — gdy stawiasz warstwę wspólną (krok 1):**

- `platform.name` oraz `platform.rocky_linux_major` zgodne z obrazem maszyn;
- `platform.infra.services` — usługi tej warstwy; dozwolone wartości to
  `pmm`, `minio` i `maildev`. Pusta lista jest legalna, gdy monitoring stoi poza
  fabryką. POMINIĘCIE bloku to co innego niż pusta lista: znaczy „pełny zestaw",
  więc dostaniesz też MinIO, nawet jeśli magazyn kopii masz gdzie indziej;
- `versions.policy` i `versions.lock_file` — `versions/versions.lock.yml` dla
  Rocky 9, `versions/versions-el10.lock.yml` dla Rocky 10;
- `proxysql.endpoint.address` — VIP, który **nie jest adresem żadnej maszyny**
  z `inventory.yml`, oraz `.port`;
- `proxysql.frontend_tls.{ca,certificate,private_key}_reference` — materiał
  wygenerowany w kroku 4 dla **pary ProxySQL i VIP-a**, nie dla klastra;
- wszystkie cztery listy `network.*_cidrs` niepuste;
- `monitoring.pmm.server_url` i `pmm.cluster_name`, a przy własnym PMM
  z certyfikatem self-signed `pmm.validate_certs: false`;
- `monitoring.alerts.email` — adres, który naprawdę odbiera pocztę.

**`platform/<nazwa>/inventory.yml`:** grupy `proxysql` (dwa węzły z
`proxysql_node_idx` i `proxysql_node_address`), `infra` (host PMM) oraz `app`
— host aplikacyjny jest WYMAGANY, bo bez niego sonda warstwy nie ma skąd
zmierzyć TLS endpointu i kończy się `UNDETERMINED`.

**`inventory.yml`:**

- `ansible_user`, `ansible_become` i `ansible_ssh_private_key_file`;
- wszystkie hosty `galera` oraz ich oba adresy (`ansible_host` i
  `galera_node_address`);
- hosty istniejącej platformy: `proxysql`, `app` oraz `infra`, gdy używasz PMM;
- usuń grupę `restore`, jeśli `backup.enabled: false`;
- po **każdej** zmianie adresów ponów `make cluster-trust-hosts`.

**`cluster.yml`:**

- `cluster.name`, `galera.cluster_name`, unikalny `proxysql.app_user`;
- `proxysql.hostgroup_base` rozłączna z innymi najemcami tej pary ProxySQL.
  Baza rezerwuje CZTERY hostgroupy (`base`, +10, +20, +30), więc 890 i 900
  kolidują, mimo że wyglądają na różne;
- `proxysql.endpoint.address` i `.port` z platformy oraz
  `proxysql.frontend_tls.ca_reference` wskazujące CA endpointu platformy;
- dla domyślnego `tls.mode: full`: `tls.ca_reference`,
  `tls.certificate_reference`, `tls.private_key_reference` do materiału
  wygenerowanego w kroku 4;
- wszystkie cztery listy `network.*_cidrs` niepuste: aplikacja, Galera,
  administracja i monitoring;
- `backup.enabled: false`, gdy magazynu jeszcze nie ma; przy `true` wypełnij
  blok wybranego backendu i harmonogram;
- `monitoring.enabled: false`, gdy nie używasz PMM; przy `true` ustaw
  `pmm.server_url`, `pmm.cluster_name`, `pmm.validate_certs`,
  `agent_groups`, `credentials_revision` i rzeczywisty adres alertów.
- `mariadb_tuning.gcache_size` — bufor, z którego wracający węzeł dostaje IST
  zamiast pełnego SST. **Wymagane statycznie** w `cluster.yml` — playbook nie ma
  fallbacku. Sugerowaną wartość policz `tests/validation/calc-gcache.py
  --write-rate <B/s> --window 30`; pomiar zrob `make lab-gcache-verify`.
  Za mała wartość nie psuje działania — kosztuje pełny SST przy każdym powrocie
  węzła i jest odrzucana przez bramkę po budowie;

Interfejs sieciowy dla VIP-a wykrywany jest z domyślnej trasy hosta; przy
nietypowej konfiguracji sieci wskaż go jawnie przez `proxysql_endpoint_interface`.

## Szybki start

```bash
# Sekrety laboratorium pozostają wyłącznie w środowisku
export PMM_ADMIN_PASSWORD='<pmm-admin-password>'
export PMM_MONITOR_PASSWORD='<mariadb-monitor-password>'
export PROXYSQL_ADMIN_PASSWORD='<proxysql-admin-password>'
export PROXYSQL_STATS_PASSWORD='<proxysql-readonly-password>'   # strażnik writera w backupie
export PROXYSQL_MONITOR_PASSWORD='<proxysql-galera-monitor-password>'
export APP_DB_PASSWORD='<application-db-password>'
export KEEPALIVED_AUTH_PASS='<vrrp-8-znakowe-haslo>'
export GALERA_BACKUP_ENCRYPTION_KEY='<klucz-szyfrowania-backupu>'
export MINIO_ROOT_USER='labbackup'
export MINIO_ROOT_PASSWORD='<minio-s3-secret>'
# Alternatywnie załaduj lokalny, ignorowany plik utworzony dla działającego labu:
# set -a; . tests/lab/.env; set +a

# Opcjonalne prowizjonowanie VM klastra w Proxmox VE (wymaga endpointu i
# poświadczeń PVE). Maszyny z innego źródła pomijają ten cel:
# make infra-provision CLUSTER=<nazwa>
# make cluster-trust-hosts CLUSTER=<nazwa>

# WARSTWA WSPOLNA — musi istniec ZANIM powstanie pierwszy klaster. Najemca
# zaklada dzialajaca pare ProxySQL i wdrozony `admin-check.cnf`; bez nich
# zatrzymuje sie na jawnej bramce, nie na przypadkowym bledzie SQL.
make platform-trust-hosts PLATFORM=<nazwa>
make platform-build PLATFORM=<nazwa>

# Pełne `cluster-validate` wymaga SSH do węzłów: po dwóch statycznych
# walidatorach uruchamia read-only preflight systemu i wersji Rocky Linux.
# Bez żywych maszyn wykonaj tylko walidatory repozytoryjne:
python3 tests/validation/validate-cluster-schema.py clusters/example-cluster/cluster.yml clusters/schema/cluster.schema.json
python3 tests/validation/validate-inventory.py clusters/example-cluster/inventory.yml clusters/example-cluster/cluster.yml
# Na żywej infrastrukturze:
# make cluster-discover CLUSTER=<nazwa>
# make cluster-validate CLUSTER=<nazwa>

# Materiał TLS (`tls.mode=full` jest defaultem). Artefakty są gitignorowane —
# na nowej maszynie trzeba je wytworzyć, inaczej bramka statyczna odmówi budowy.
#   1) CA + cert KLASTRA (ścieżki z tls.*_reference w cluster.yml); SAN musi
#      pokrywać nazwy ORAZ adresy węzłów — Galera łączy się po adresie.
pki/generate.sh <klaster> <n1,n2,n3,ip1,ip2,ip3>
#   2) CA + cert WSPÓLNEGO endpointu ProxySQL (proxysql.frontend_tls). CA jest
#      wspólne dla całej floty, nie klastrowe: jedna para ProxySQL serwuje
#      wszystkie klastry JEDNYM certem frontendu. Wdraza go wylacznie warstwa
#      wspolna (`make platform-proxysql`); klaster deklaruje tylko `ca_reference`.
#      SAN musi pokrywać VIP i adresy węzłów ProxySQL.
pki/generate.sh shared-proxysql fcp1,fcp2,<ip-fcp1>,<ip-fcp2>,<ip-vip>
#   3) Certyfikaty PER WĘZEŁ (tls.per_node_certificates=true). Po kroku 1 wystaw
#      osobny liść i klucz dla każdego węzła pod wspólnym CA klastra:
pki/issue-node-certs.sh <klaster> <n1=ip1,n2=ip2,n3=ip3> [dni]
#
#   Rotacja liścia pod tym samym CA:
#     * wspólny cert: REUSE_CA=1 przed generate.sh, potem `make cluster-tls-rotate`
#     * per węzeł: issue-node-certs.sh (nowy liść), potem `make cluster-tls-rotate`
#     * frontend ProxySQL: REUSE_CA=1 przed generate.sh shared, potem
#       `make platform-proxysql` (PROXYSQL RELOAD TLS bez zrywania sesji).
#   Rotację CA prowadzi pki/rotate-ca.sh (okno podwójnego zaufania).

# UWAGA kolejność: poniższa sekwencja jest zweryfikowana od zera (from-scratch).
# Zależności, które ją wymuszają:
#   F6 asertuje granty pmm_monitor  -> musi być PO F11
#   F11 rejestruje metryki ProxySQL -> musi być PO F7
#   F10 restore drill wymaga danych -> musi być PO F9 (workload zasiewa isa_test);
#     na klastrze bez testów chaos zasiej je jawnie: make lab-seed-smoke CLUSTER=<name>
#   lab-monitoring-verify sprawdza świeżość backupu i reguły -> na samym końcu

# `cluster-deploy` instaluje i przeładowuje politykę firewalld przed bootstrapem.
# Nie uruchamiaj bootstrapu na własną rękę przed tym krokiem.

# F4 — initial bootstrap: JEDEN węzeł (galera[0]), wymaga jawnego CONFIRM=yes
make cluster-bootstrap CLUSTER=<nazwa> CONFIRM=yes


# F5 — dołącz pozostałe węzły (SST mariabackup, serial:1)
make cluster-join CLUSTER=<nazwa>
make lab-galera-verify CLUSTER=<nazwa>

# F7 — ProxySQL (mysql_galera_hostgroups, jeden aktywny writer)
make cluster-proxysql CLUSTER=<nazwa>
make lab-proxysql-verify CLUSTER=<nazwa>
# F11 — monitoring: node_exporter, PMM Inventory/QAN, metryki ProxySQL, logrotate
make cluster-monitoring CLUSTER=<nazwa>
# Przy każdej rotacji PMM_MONITOR_PASSWORD zwiększ także
# monitoring.pmm.credentials_revision w cluster.yml.

# F6 — hardening MariaDB (wymaga konta pmm_monitor z F11)
make cluster-harden CLUSTER=<nazwa>
make lab-hardening-verify CLUSTER=<nazwa>

# F8 — redundantny endpoint (Keepalived VIP, unicast VRRP, failover < RTO).
# NALEZY DO WARSTWY WSPOLNEJ, nie do klastra — patrz sekcja "Warstwa wspolna".
make platform-endpoint
make lab-endpoint-verify CLUSTER=<nazwa>

# F9 — testy chaos (destrukcyjne, tylko poza produkcją); zasiewają też isa_test
make lab-failover-test CLUSTER=<nazwa>        # ISC-27/28: kill writera, brak utraty tx
make lab-split-brain-test CLUSTER=<nazwa>     # ISC-30: partycja sieci, jeden Primary
make verify-no-mass-restart                     # ISC-31: brak masowego restartu Galery

# P2: jeden destrukcyjny pomiar utraty kworum.
# APP_DB_PASSWORD musi byc wyeksportowane; run ID generuje operator:
# run_id="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
# QUORUM_RUN_ID="$run_id" make lab-app-degradation-test CLUSTER=<nazwa> CONFIRM=yes
# Artefakt: /var/tmp/quorum-evidence-<nazwa>-${run_id}.json

# F10 — konfiguracja schedulera, ręczny backup i potwierdzany restore
make cluster-backup-configure CLUSTER=<nazwa>
make cluster-backup CLUSTER=<nazwa>                    # ISC-32/33/34/35
make lab-backup-verify CLUSTER=<nazwa>                 # S3: szyfr/checksum/metadata
make cluster-restore-drill CLUSTER=<nazwa> CONFIRM=yes

make lab-restore-verify CLUSTER=<nazwa>                 # integralność i świeżość
make lab-backup-impact CLUSTER=<nazwa>                  # ISC-39, lab-only
#
# Backend, scheduler, sekrety, rotacja i diagnostyka:
# docs/runbooks/backup.md

# F15 — reguły alertów (ISC-47); adres e-mail z monitoring.alerts.email w cluster.yml
make cluster-alerts CLUSTER=<nazwa>

# Kontrakt końcowy: PMM Server i pmm-client z aktywnego lockfile'a (oba 3.9.1);
# oczekiwane nodes/services wynikają z inventory i cluster.yml, dalej QAN,
# świeże metryki Galery/lifecycle ORAZ reguły ISC-47
make cluster-monitoring-refresh CLUSTER=<nazwa>
make lab-monitoring-verify CLUSTER=<nazwa>

# Pelna awaria klastra (cold recovery) wymaga kontrolowanego stopu i jednego
# bootstrapu; rownolegly `systemctl restart` zostawia NON_PRIM.
make cluster-recover CLUSTER=<name> CONFIRM=yes
#
# Jedna bramka po budowie (fail-closed):
# make lab-post-build-gate CLUSTER=<name>
```

PMM UI laboratorium: `http://127.0.0.1:8080`. Stan usług w PMM jest diagnostyczny: `Down` oznacza rzeczywiście nieosiągalną usługę, a nie błąd rejestracji.
`GF_SECURITY_ADMIN_PASSWORD` inicjalizuje tylko czysty `pmm-data`; istniejący volume zachowuje zapisane hasło. Rotację wykonaj w PMM UI, po czym ustaw tę samą wartość w `PMM_ADMIN_PASSWORD`.
Alerting (F15) jest wdrożony: `make cluster-alerts` provisionuje reguły zdrowia Galery, writera ProxySQL, backupu i restore, zamrożonych metryk oraz — gdy TLS jest włączony — ważności certyfikatu; reguły warstwy wspólnej (`isa-shared-*`) provisionuje `make platform-alerts`. Krytyczne reguły używają `noDataState: Alerting`; brak metryk nie przechodzi cicho. Contact point i notification policy (`managed_by=ansible` → e-mail) biorą adres z `monitoring.alerts.email` w `cluster.yml`. W laboratorium poczta trafia do `maildev`.

`lab-backup-verify` weryfikuje backend S3 i wymaga przypiętego SDK (`minio.sdk_version` z lockfile). Zarządzany SMB oraz wcześniej zamontowany filesystem weryfikuje `tests/live/probe-galera-backup-backends.py`; procedury i ograniczenia opisuje `docs/runbooks/backup.md`.

## Warstwa wspolna

Para wezlow ProxySQL, VIP Keepalived, host monitoringu (PMM, opcjonalnie MinIO
i maildev) oraz host aplikacyjny to **jednostka niezalezna od klastrow**, opisana
w `platform/<nazwa>/` (`platform.yml` + `inventory.yml`). Klastry Galera sa jej
**najemcami**: `make cluster-proxysql` rejestruje ich hostgroupy i uzytkownika,
i tylko tyle. Jedna warstwa obsluguje wielu najemcow, ktorych rozdziela wylacznie
rozlacznosc hostgroup i kont — pilnuje jej `make verify-proxysql-tenancy`.

Do 2026-08-21 wlascicielem warstwy byl klaster (`proxysql.role: owner`), wiec
jego skasowanie osierocilo by ProxySQL, VIP, PMM i MinIO. Pole `role` juz nie
istnieje; jego powrotu pilnuje `make verify-proxysql-tenancy`.

| Cel | Co robi |
|---|---|
| `make platform-validate` | schemat + inwarianty inwentarza + preflight |
| `make platform-trust-hosts` | re-skan kluczy SSH po re-provision |
| `make platform-deploy` | pakiety ProxySQL wg lockfile EL10 (sha256 + GPG) |
| `make platform-infra` | PMM, MinIO, maildev na `fcinfra` |
| `make platform-proxysql` | konfiguracja pary: TLS frontendu, tozsamosc admina, monitor |
| `make platform-endpoint` | Keepalived VIP — **wylacznie tutaj**, nigdy z klastra |
| `make platform-monitoring` | rejestracja wezlow i eksporterow w PMM |
| `make platform-alerts` | reguly `isa-shared-*` |
| `make platform-verify` | sonda warstwy jako calosci |
| `make platform-build` | wszystko powyzej jednym poleceniem |
| `make platform-adopt CONFIRM=yes` | migracja: przejmuje wpisy PMM po bylym ownerze |

**Kolejnosc jest wymogiem: `platform-build` przed pierwszym `cluster-build`.**

Warstwa daje sie zweryfikowac **bez ani jednego klastra** — `probe-platform.py`
nie loguje sie do bazy, tylko sprawdza lancuch certyfikatu endpointu przez
`openssl`, bo platforma z zerem najemcow nie ma zadnych uzytkownikow.
Udowodnione odbudowa od zera: `fcp1`/`fcp2` zniszczone Terraformem, postawione
jednym `make platform-build`, konfiguracja koncowa identyczna z baseline.

Odbudowa wspolnych hostow uniewaznia `known_hosts` **kazdego** najemcy (osobny
plik per klaster) — przed pierwszym `cluster-proxysql` uruchom
`make cluster-trust-hosts CLUSTER=<nazwa>`.

## Żywa flota

Przykłady statyczne używają szablonu `CLUSTER=example-cluster`. Realne klastry VM
działają na Proxmox VE na **tym samym kodzie** — różnice niesie wyłącznie
`versions.lock_file` w `cluster.yml`. Każda komenda ze Szybkiego startu działa na
nich przez `CLUSTER=<nazwa>`.

**Tego pliku nie pytaj, co teraz żyje.** Klastry powstają i znikają w każdej
sesji, więc wpisana tu tabela maszyn kłamie w ciągu godzin — dokładnie tak
skończył poprzedni spis, ogłaszając jako aktywny stack skasowany dwa dni
wcześniej. Podział źródeł prawdy:

| Pytanie | Źródło |
|---|---|
| Jaki jest zamiar? | `clusters/<nazwa>/` i `platform/<nazwa>/` — walidowane schematem |
| Co naprawdę teraz działa? | `make fleet-state` — odczyt z hypervisora |
| Jak było kiedyś? | `docs/records/<data>-*.md` — zamrożone, nieaktualizowane |

`make fleet-state` zestawia definicje z repo z maszynami w puli i pokazuje, które
są żywe, zatrzymane albo są już tylko archiwum, kto dzieli wspólny endpoint i czy
ten endpoint odpowiada. Zasady, które nie zależą od floty — limit zasobów, zakresy
VMID poza tą automatyzacją, reguła przynależności do puli — w
`docs/infrastructure-state.md`.

Że README pozostanie wolne od nazw instancji, pilnuje `make verify-zero-hardcode`:
nazwy czyta z katalogów `clusters/*/` i `platform/*/`, więc bramka obejmuje każdy
nowy klaster w chwili powstania.

## Struktura

```
clusters/<name>/     — inventory.yml + cluster.yml + secrets per klaster
versions/            — lockfile, discovered-versions, compatibility-report
profiles/            — specyfikacja profili srodowiskowych (README.md)
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
Stan floty na żywo — `make fleet-state`; zasady niezależne od floty —
`docs/infrastructure-state.md`; historia przebiegów — `docs/records/`.
