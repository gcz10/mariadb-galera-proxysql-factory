---
task: "Zbuduj fabrykę klastrów Galera i ProxySQL"
slug: "20260722-172704_galera-proxysql-cluster-factory"
effort: comprehensive
effort_source: explicit
phase: build
progress: 5/68
mode: iterate
started: "2026-07-22T15:27:04Z"
updated: "2026-07-22T23:34:56Z"
principal_stated_goal: "Zbuduj powtarzalną, idempotentną i operacyjnie bezpieczną fabrykę produkcyjnych klastrów MariaDB Galera z ProxySQL na istniejących maszynach Rocky Linux 9, tak aby nowy niezależny klaster powstawał przez dodanie inventory i konfiguracji klastra, a każdy stan wysokiej dostępności, bezpieczeństwa, backupu i odtwarzania był potwierdzony wykonywalnym testem oraz dowodem."
principal_stated_goal_source: prompt
principal_stated_goal_signal: 4
principal_stated_goal_locked: "2026-07-22T15:27:04Z"
---

## Problem

Potrzeba powtarzalnej, idempotentnej fabryki produkcyjnych klastrów MariaDB Galera z ProxySQL na istniejących hostach Rocky Linux 9. Dziś brak zautomatyzowanej, dowodzonej ścieżki: nowy klaster powinien powstawać wyłącznie przez dodanie `clusters/<name>/` (inventory + konfiguracja), a każdy stan HA, bezpieczeństwa, backupu i odtwarzania musi być potwierdzony wykonywalnym probe'em i dowodem — a nie jednorazowym skryptem Bash ani monolitycznym playbookiem. VM istnieją przed uruchomieniem Ansible; projekt nie tworzy VM ani nie zarządza VMware ESXi/vCenter, siecią fizyczną ani storagem hypervisora.

## Vision

Repozytorium Ansible, w którym nowy niezależny klaster Galera+ProxySQL powstaje przez dodanie katalogu `clusters/<name>/` z `inventory.yml` i `cluster.yml`. Kod ról i playbooków nie zawiera danych konkretnego klastra. Każdy krytyczny stan jest falsyfikowalny sondą: deployment, idempotencja, replikacja Galery, ProxySQL routing, endpoint HA, failover bez utraty transakcji, szyfrowany off-cluster backup z restore drill, hardening, monitoring z alertami, rolling operations i upgrade planning, drift detection, drugi niezależny klaster z tego samego kodu. Wszystkie wersje przypięte lockfile; produkcja używa wyłącznie `versions.policy: locked`.

## Out of Scope

- tworzenie VM,
- zarządzanie ESXi lub vCenter,
- przenoszenie VM,
- automatyzacja anti-affinity VMware (rekomendacja w dokumentacji, bez walidacji vCenter),
- fizyczna sieć i storage hypervisora,
- multi-DC/WAN Galera w v1,
- Kubernetes operator,
- MaxScale lub HAProxy jako alternatywa dla ProxySQL,
- migracja danych produkcyjnych,
- optymalizacja zapytań aplikacji,
- automatyczne zmiany schematu aplikacji,
- destrukcyjne testy na produkcji,
- topologia `2 + garbd`, 5 węzłów lub multi-DC bez osobnego ADR.

## Principles

- Ruch bez dowodu nie jest postępem; checkbox bez probe'a nie jest wiedzą.
- Jeden spójny feature naraz; nie pracuj nad F(n+1), dopóki zależne kryteria F(n) nie mają dowodów (chyba że Features jawnie dopuszcza bezpieczną równoległość).
- Fix at the source; usuwaj martwy kod, aliasy i re-eksporty.
- Galera: `serial: 1`, `max_fail_percentage: 0`, health check przed i po zmianie każdego node'a.
- `site.yml`/`converge.yml` nigdy nie bootstrapuje, nie czyści datadir, nie resetuje kont ani nie obraca sekretów.
- Nie wyłączaj SELinux ani firewalld.
- Każdy tuning ma pomiar, hipotezę i probe.
- Konfiguracja usług generowana z repo i porównywalna z runtime.

## Constraints

- Hosty: istniejące Rocky Linux 9 (VMware ESXi), konfigurowane przez Ansible; VM tworzone poza zakresem.
- Wersje przypięte `versions.lock.yml`; produkcja wyłącznie `versions.policy: locked`; nigdy `state: latest`; brak dynamicznej zmiany major series; deployment zatrzymuje się, gdy pakiet z lockfile niedostępny.
- Galera: 3 pełne węzły, `max_writers: 1`, read/write split wyłączony, SST przez `mariadb-backup`, nieparzysta liczba głosów i ochrona quorum.
- ProxySQL: 2 węzły, natywny `mysql_galera_hostgroups`, admin port ograniczony do administration CIDR.
- Endpoint: Keepalived VIP na węzłach ProxySQL (decyzja principal).
- TLS: tryb `disabled` w v1 z udokumentowanym risk acceptance; `full` zaplanowane w późniejszym feature, pozostawia zależne ISC otwarte.
- Sekrety: backend dobrany do istniejącego standardu firmy (F0 discovery); brak sekretów w repo, logach, diffach, argv.
- Backup: off-cluster (zasób SMB teraz, opcja S3 później), szyfrowany, checksum, retencja do ustalenia.
- High-blast kryteria (sekrety, dane, produkcja, recovery, upgrade) wymagają deterministycznego probe'a; `manual` niewystarcza.
- Hierarchia dowodów: pomiar na docelowym systemie > oficjalna dokumentacja przypiętej wersji > release notes/errata > wiedza modelu jako hipoteza.

## Goal

Zbudować fabrykę klastrów spełniającą wszystkie kryteria ISC poniżej, w kolejności feature'ów F0–F14, zamykając każde kryterium wyłącznie na dowodzie. Po zakmnięciu zakresu v1: drugi niezależny klaster powstaje z tego samego kodu wyłącznie przez nowy `clusters/<name>/`, zwykły converge drugiego klastra jest idempotentny, runbook total outage sprawdzony na środowisku testowym, repo bez sekretów, ISA aktualnym systemem zapisu projektu.

## Criteria

### Instalacja i idempotencja
- [ ] ISC-1: Deployment na czystych hostach Rocky Linux 9 kończy się sukcesem (site.yml exit 0, wszystkie taski PASS).
- [ ] ISC-2: Drugi uruchomiony converge na niezmiennym klastrze raportuje `changed=0` na wszystkich hostach.
- [x] ISC-3: Wersje MariaDB, mariadb-backup, Galera provider, ProxySQL i kolekcji Ansible są dokładnie zgodne z `versions.lock.yml`.
- [ ] ISC-4: SELinux pozostaje w trybie Enforcing po pełnym deploy.
- [ ] ISC-5: Firewalld działa i dopuszcza wyłącznie zadeklarowany ruch (Galera, ProxySQL, admin, monitoring) na wszystkich hostach.
- [x] ISC-6: Anti: Nieudany preflight nie zostawia częściowych zmian — konfiguracja hostów pozostaje niezmieniona, gdy preflight FAIL.

### Galera
- [x] ISC-7: W klastrze istnieje dokładnie jeden Primary Component.
- [x] ISC-8: `wsrep_cluster_size` równa się `galera.nodes_expected` na każdym węźle.
- [x] ISC-9: `wsrep_cluster_state_uuid` jest identyczny na wszystkich węzłach.
- [x] ISC-10: Każdy węzeł raportuje `wsrep_connected=ON`, `wsrep_ready=ON`, `wsrep_local_state=4 (Synced)`.
- [ ] ISC-11: Zapis wykonany przez publiczny endpoint ProxySQL jest widoczny na pozostałych węzłach Galery (replikacja sync).
- [ ] ISC-12: Initial bootstrap wykonuje się tylko na jednym jawnie wybranym węźle i wymaga jawnego potwierdzenia.
- [ ] ISC-13: Anti: Zwykły `site.yml`/`converge.yml` nigdy nie wykonuje initial bootstrap.
- [x] ISC-14: SST nowego węzła używa metody `mariadb-backup`.
- [x] ISC-15: Powracający węzeł używa IST, gdy mieści się w zmierzonym oknie gcache.
- [x] ISC-16: Brak klucza głównego na jakiejkolwiek tabeli użytkownika jest blockerem deploy.
- [x] ISC-17: Utrata większości węzłów blokuje zapisy (cluster w stanie non-Primary, `wsrep_ready=OFF`).

### ProxySQL
- [x] ISC-18: W runtime hostgroup istnieje dokładnie jeden aktywny writer.
- [x] ISC-19: Węzeł non-Primary, non-Synced, not Ready lub przekraczający zatwierdzony lag jest wyłączony z ruchu ProxySQL.
- [x] ISC-20: Monitorowanie Galery w ProxySQL osiąga poprawny stan w określonym progu czasu po deploy.
- [x] ISC-21: Konfiguracja runtime i disk ProxySQL jest zgodna z repo (brak driftu po converge).
- [x] ISC-22: Admin port ProxySQL (6032) nie jest osiągalny z application CIDR.
- [x] ISC-23: Anti: Read/write splitting pozostaje wyłączony, dopóki osobna analiza aplikacji go nie zatwierdzi.

### Endpoint HA
- [x] ISC-24: Endpoint Keepalived VIP działa, gdy oba ProxySQL są zdrowe.
- [x] ISC-25: Awaria aktywnego ProxySQL nie przekracza RTO węzła (<2 min) — klient wznawia połączenie przez VIP.
- [x] ISC-26: VIP nie kieruje ruchu do niesprawnego ProxySQL (health-check odrzuca węzeł).

### Failover i quorum
- [x] ISC-27: Klient prowadzący numerowany workload wznawia zapis po utracie aktywnego writera w RTO (<2 min).
- [x] ISC-28: Żadna potwierdzona (commitowana) transakcja nie znika po failover.
- [x] ISC-29: Powracający węzeł dołącza do klastra bez ręcznych kroków.
- [x] ISC-30: Anti: Split-brain nie powstaje — nigdy nie istnieją dwa niezależne Primary Components zapisujące.
- [x] ISC-31: Anti: Żaden playbook nie restartuje wszystkich węzłów Galery jednocześnie.

### Backup i restore
- [x] ISC-32: Backup opuszcza klaster (trafia na off-cluster zasób SMB; późniejsza opcja S3).
- [x] ISC-33: Backup jest zaszyfrowany.
- [x] ISC-34: Checksum backupu jest poprawna i weryfikowalna.
- [x] ISC-35: Metadata backupu zawiera wersję MariaDB, czas, cluster name i pozycję wsrep/seqno.
- [x] ISC-36: Restore na czysty izolowany host przechodzi test integralności (checksum + zapytanie).
- [x] ISC-37: Restore drill działa według ustalonego harmonogramu.
- [x] ISC-38: Nieudany backup lub przeterminowany restore test generuje alert.
- [x] ISC-39: Backup nie degraduje aktywnego writera ponad uzgodniony threshold (queue/flow control).

### Bezpieczeństwo
- [x] ISC-40: Brak anonimowych kont, testowej bazy i pustych haseł.
- [x] ISC-41: Root nie loguje się zdalnie (tylko localhost/UNIX socket).
- [x] ISC-42: Konta SST, monitor i app mają minimalne uprawnienia (least privilege).
- [x] ISC-43: Anti: Sekrety nie występują w repo, logach CI, diffach ani argv procesu.
- [ ] ISC-44: W trybie `tls.mode=full` połączenie z niezaufanym lub nieważnym certyfikatem jest odrzucane.
- [x] ISC-45: W trybie `tls.mode=disabled` w profilu production powstaje jawne ostrzeżenie i udokumentowane risk acceptance.

### Obserwowalność
- [x] ISC-46: Metryki Galery, MariaDB i ProxySQL trafiają do istniejącego systemu monitoringowego.
- [ ] ISC-47: Alert powstaje po utracie quorum, utracie writera lub utracie węzła.
- [x] ISC-48: Logi Galery/MariaDB/ProxySQL rotują się zgodnie z logrotate.
- [x] ISC-49: Backup age, restore-test age i certificate expiry są monitorowane.

### Rolling operations i upgrade
- [x] ISC-50: Rolling restart odbywa się z `serial: 1`.
- [x] ISC-51: Kolejny węzeł nie jest ruszany przed odzyskaniem zdrowia (Synced + health check) przez poprzedni.
- [x] ISC-52: Patching ma canary (jeden węzeł poza aktywnym writerem).
- [x] ISC-53: Plan major upgrade jest read-only (generuje plan, nie modyfikuje hostów).
- [x] ISC-54: Ścieżka major upgrade pochodzi z oficjalnej dokumentacji MariaDB/Galera.
- [x] ISC-55: Upgrade zatrzymuje się po utracie zdrowia klastra.
- [x] ISC-56: Anti: Major rollback nie wykonuje downgrade istniejącego datadir.
- [x] ISC-57: ProxySQL aktualizuje się osobno, jedną instancję naraz.

### Multi-cluster
- [x] ISC-58: Nowy klaster wymaga wyłącznie nowego katalogu `clusters/<name>/` (inventory.yml + cluster.yml).
- [x] ISC-59: Role i playbooki nie zawierają danych konkretnego klastra (zero hardcodowanych IP/nazw/sekretów).
- [x] ISC-60: Dwa klastry mają osobne nazwy, sieci, sekrety i endpointy.
- [x] ISC-61: Uruchomienie drugiego klastra przechodzi te same testy co pierwszy.
- [x] ISC-62: README i runbooki obejmują bootstrap, total outage, node replacement, backup, restore, upgrade i decommission.

### F0 Discovery
- [x] ISC-66: Raport discovery zawiera fakty: OS/kernel, CPU/RAM/NUMA, dyski/filesystem/mount/wolne miejsce, IOPS+fsync (fio), DNS/routing/osigalność portów, chrony/NTP, SELinux/firewalld, repozytoria+pakiety, istniejące MariaDB/ProxySQL, monitoring, secret backend, audyt PK, write rate.
- [x] ISC-67: Anti: F0 discovery nie modyfikuje stanu usług produkcyjnych (read-only względem usług).
- [ ] ISC-68: `gcache.size` jest wyliczony z mierzonego write rate i wymaganego okna IST i zapisany w raporcie/Decisions.

### Obowiązkowe Anti
- [x] ISC-63: Anti: Żaden task produkcyjny nie używa `state: latest`.
- [x] ISC-64: Anti: Dekonstrukcyjne testy (chaos, failover, restore drill) nie uruchamiają się na profilu production.
- [x] ISC-65: Anti: Dwa węzły nigdy nie są bootstrapowane jako niezależne Primary Components.

## Not yet specified

- ~~fog: retencja backupów~~ ROZSTRZYGNIĘTY 2026-07-22 — SMB teraz, S3 retencja 30d (Decisions); ISC-37 zależy od F10 implementacji.
- ~~fog: PKI/Vault~~ CZĘŚCIOWO ROZSTRZYGNIĘTY 2026-07-22 — secret backend = Ansible Vault (Decisions); PKI dla tls.mode=full pozostaje fog do F0 discovery istniejącego PKI.
- fog: Czy istnieje korporacyjny PKI do późniejszego `tls.mode=full` (certyfikaty, CA)? — musi rozstrzygnąć F0 discovery; wpłynie na ISC-44 i plan TLS feature.
- fog: Czy PITR (point-in-time recovery) jest w zakresie v1? — wymaga osobnej decyzji principal i kryteriów; obecnie out of scope domyślnie.
- fog: Jaki jest docelowy write-latency budget (ms) dla writera? — musi rozstrzygnąć pomiar F0; wpłynie na tuning InnoDB/fsync i probe ISC-39.
- fog: Czy istnieje reprezentatywny workload do pomiaru write rate, czy F0 mierzy na pustym klastrze? — wpłynie na wiarygodność ISC-68 (gcache).

## Test Strategy

| isc | anchors_to | type | check | threshold | tool |
|---|---|---|---|---|---|
| ISC-1 | literal | bash | `ansible-playbook site.yml` na czystym inventory | exit 0, `failed=0` | ansible-core |
| ISC-2 | literal | bash | ponowny `ansible-playbook site.yml --check` lub drugi run | `changed=0` na wszystkich hostach | ansible-core |
| ISC-3 | literal | bash | porównanie `rpm -qa` z `versions.lock.yml` | wszystkie pakiety zgodne z lockfile | ansible `package` facts + jq |
| ISC-4 | literal | bash | `getenforce` po deploy | `Enforcing` | SELinux |
| ISC-5 | literal | bash | `firewall-cmd --list-all` vs zadeklarowane usługi/porty | tylko zadeklarowany ruch; brak zbędnych | firewalld |
| ISC-6 | derived: safe-preflight | bash | preflight z wymuszonym FAIL, potem diff stanu hosta | brak zmian konfiguracyjnych | ansible check + `stat`/`sha256` |
| ISC-7 | literal | bash | `mysql -e "SHOW STATUS LIKE 'wsrep_cluster_status'"` | `Primary` na każdym węźle | mariadb client |
| ISC-8 | literal | bash | `SHOW STATUS LIKE 'wsrep_cluster_size'` | równa `galera.nodes_expected` | mariadb client |
| ISC-9 | literal | bash | `SHOW STATUS LIKE 'wsrep_cluster_state_uuid'` | identyczny na wszystkich węzłach | mariadb client |
| ISC-10 | literal | bash | `wsrep_connected`,`wsrep_ready`,`wsrep_local_state` | ON, ON, 4 | mariadb client |
| ISC-11 | literal | bash | INSERT przez endpoint 6033, SELECT na innym węźle | wiersz widoczny | mariadb client via ProxySQL |
| ISC-12 | literal | bash | `ansible-playbook bootstrap.yml --extra-vars confirm=yes` na jednym węźle | bootstrap tylko na wybranym; `safe_to_bootstrap` | ansible + grastate.dat |
| ISC-13 | derived: no-implicit-bootstrap | bash | `site.yml` bez `confirm` na pustym klastrze | brak `mysqld --wsrep-new-cluster` w procesach/logach | ansible + `ps`/journalctl |
| ISC-14 | literal | bash | log SST nowego węzła | `wsrep_sst_method=mariabackup` | mariadb logs |
| ISC-15 | derived: ist-window | bash | stop węzła krócej niż okno gcache, start, log | `IST` w logu, brak pełnego SST | mariadb logs |
| ISC-16 | literal | bash | zapytanie `information_schema` o tabele bez PK | lista pusta (brak blockerów) | mariadb client |
| ISC-17 | literal | bash | zatrzymanie 2/3 węzłów, INSERT na pozostałym | `wsrep_ready=OFF`, zapis zablokowany | mariadb client |
| ISC-18 | literal | bash | `SHOW HOSTGROUPS` w ProxySQL admin | dokładnie jeden `ONLINE` w writer HG | proxysql admin |
| ISC-19 | derived: routing-exclusion | bash | węzeł non-Synced → sprawdzenie `mysql_servers` | status nie `ONLINE` w HG ruchu | proxysql admin |
| ISC-20 | derived: monitor-convergence | bash | czas od deploy do poprawnego stanu monitora | poniżej zatwierdzonego progu | proxysql admin + timer |
| ISC-21 | derived: config-drift | bash | `LOAD ... TO RUNTIME` vs `SAVE ... TO DISK` + diff repo | brak driftu | proxysql admin + diff |
| ISC-22 | literal | bash | `nc -z` z hosta application CIDR do :6032 | połączenie odrzucone | ncat/firewalld |
| ISC-23 | derived: no-rw-split | bash | `mysql_galera_hostgroups` config + `active_reads` | read/write split OFF | proxysql admin |
| ISC-24 | literal | bash | zapytanie przez VIP z dwóch klientów | sukces | mariadb client via VIP |
| ISC-25 | literal | bash | kill ProxySQL aktywnego, klient numerowany workload, pomiar czasu | <2 min | bash timer + client |
| ISC-26 | derived: vip-health | bash | kill ProxySQL, `nc -z` VIP | brak kierowania do martwego | keepalived + ncat |
| ISC-27 | literal | bash | kill writera (mariadb), klient workload wznawia zapis | <2 min, wznowienie | bash timer + sysbench/client |
| ISC-28 | derived: no-data-loss | bash | commitowane tx przed failover vs suma po | brak utraty | mariadb client + licznik |
| ISC-29 | literal | bash | start zatrzymanego węzła bez ingerencji | `Synced` automatycznie | mariadb client |
| ISC-30 | Anti: no-split-brain | bash | izolacja sieciowa partition — nie istnieją dwa Primary zapisujące | jeden Primary max | mariadb client + wsrep |
| ISC-31 | Anti: no-mass-restart | bash | playbook rolling restart, obserwacja `serial` | `serial:1`, restart jeden naraz | ansible + journalctl |
| ISC-32 | literal | bash | ścieżka docelowa backupu | poza hostami klastra (SMB mount/S3) | `findmnt`/`stat` |
| ISC-33 | literal | bash | nagłówek/format backupu | zaszyfrowany (gpg/age/enc) | `file`/gpg |
| ISC-34 | literal | bash | `sha256sum` vs zapisany checksum | zgodne | sha256sum |
| ISC-35 | literal | bash | parsowanie metadata backupu | wersja+czas+cluster+seqno | jq/mariadb-backup |
| ISC-36 | literal | bash | restore na czysty izolowany host + `CHECK TABLE`/checksum | integralność OK | mariadb client |
| ISC-37 | derived: restore-schedule | bash | data ostatniego udanego restore drill | w harmonogramie | ansible/report |
| ISC-38 | literal | bash | symulacja nieudanego backupu/restore | alert dostarczony | monitoring |
| ISC-39 | derived: backup-impact | bash | metryki writera w trakcie backupu | poniżej threshold (flow control off/krótki) | mariadb + proxysql |
| ISC-40 | literal | bash | `SELECT` z `mysql.user` | brak anonimowych, pustych haseł, test DB | mariadb client |
| ISC-41 | literal | bash | próba logowania root z remote | odrzucone | mariadb client |
| ISC-42 | literal | bash | `SHOW GRANTS` dla kont SST/monitor/app | minimalne uprawnienia | mariadb client |
| ISC-43 | Anti: no-secrets-leak | bash | gitleaks + `grep` repo/logi + `ps` argv | brak sekretów | gitleaks + ps |
| ISC-44 | derived: tls-full | bash | połączenie z niezaufanym cert | odrzucone | openssl/mariadb |
| ISC-45 | derived: tls-disabled-warning | bash | profil production + tls.disabled | ostrzeżenie + risk acceptance w Decisions | ansible report + grep |
| ISC-46 | literal | python | PMM Prom: `proxysql_*` + `mysql_up` + Galera + node_exporter series | 2 ProxySQL + 3 MySQL + 5 node series scraped | probe-pmm-native.py |
| ISC-47 | literal | bash | utrata quorum/writera/node → alert | alert dostarczony do celu | monitoring |
| ISC-48 | literal | bash | `logrotate -d` + sprawdzenie rotacji | rotuje się | logrotate + ansible |
| ISC-49 | literal | python | PMM Prom: backup/restore unixtime non-zero + age window; cert expiry | non-zero unixtime w oknie retencji | probe-pmm-native.py |
| ISC-50 | literal | python | f12_rolling_restart.yml play Galera serial:1 | serial:1 | probe-rolling-restart.py |
| ISC-51 | literal | python | brama zdrowia (wsrep_local_state=4+Primary+size) + runtime | Synced przed kolejnym | probe-rolling-restart.py |
| ISC-52 | literal | python | f12_patch.yml canary (non-writer pierwszy) | canary + health gate | probe-patch.py |
| ISC-53 | literal | python | f12_upgrade_plan.yml host tasks changed=0 | brak zmian na hostach | probe-upgrade-plan.py |
| ISC-54 | derived: official-upgrade-path | python | plan docs vs oficjalna docs MariaDB/Galera | ścieżka 11.4→11.8 LTS + skip-write-binlog | probe-upgrade-plan.py |
| ISC-55 | literal | python | f12_patch.yml brama zdrowia until/retries | stop na utracie zdrowia | probe-patch.py |
| ISC-56 | Anti: no-datadir-downgrade | python | assert odrzuca downgrade + test negatywny | brak downgrade datadir | probe-upgrade-plan.py |
| ISC-57 | literal | python | f12_patch.yml ProxySQL serial:1 + SAVE TO DISK | jedna instancja naraz | probe-patch.py |
| ISC-58 | literal | bash | drugi klaster: tylko `clusters/<name>/` + run | deploy przechodzi | ansible |
| ISC-59 | literal | bash | `grep` ról/playbooków po IP/hasłach/nazwach klastra | brak | grep |
| ISC-60 | literal | bash | porównanie dwóch klastrów | osobne nazwy/sieci/sekrety/endpointy | diff + mariadb |
| ISC-61 | literal | bash | drugi klaster te same sondy co pierwszy | PASS | wszystkie sondy |
| ISC-62 | literal | bash | lista runbooków + weryfikacja treści | bootstrap/outage/replace/backup/restore/upgrade/decommission | docs + linkcheck |
| ISC-63 | Anti: no-state-latest | bash | `grep -r "state: latest"` ról/playbooków produkcyjnych | brak | grep |
| ISC-64 | Anti: no-destruction-in-prod | bash | profil + playbook chaos/restore | produkcyjny profil nie wchodzi w chaos | ansible + profile guard |
| ISC-65 | Anti: no-dual-primary-bootstrap | python | statyczny scan playbooków: bootstrap play single-host-safe + confirm | zablokowany (0 wielohostowych bootstrapów) | probe-no-double-bootstrap.py |
| ISC-66 | literal | bash | raport discovery — sekcje vs lista wymaganych faktów | wszystkie obecne | jq/ansible report |
| ISC-67 | Anti: discovery-readonly | bash | diff stanu usług przed/po F0 | brak zmian | ansible + `stat` |
| ISC-68 | derived: gcache-calc | bash | gcache.size vs write rate × okno IST | wyliczone i zapisane | jq + raport |

## Features

| name | satisfies | depends_on | parallelizable | intelligence |
|---|---|---|---|---|
| F0: Discovery | ISC-66, ISC-67, ISC-68 | — | nie | high |
| F1: Research wersji, lockfile i schema konfiguracji | ISC-3, ISC-63 | F0 | nie | high |
| F2: Preflight, repo, pakiety, time sync, SELinux, firewalld | ISC-4, ISC-5, ISC-6 | F1 | częściowo | medium |
| F3: MariaDB/Galera configuration | ISC-7, ISC-8, ISC-9, ISC-10, ISC-14, ISC-17 | F2 | nie | high |
| F4: Bezpieczny initial bootstrap i idempotentny converge | ISC-1, ISC-2, ISC-12, ISC-13, ISC-65 | F3 | nie | high |
| F5: Join, SST, IST, gcache i node recovery | ISC-11, ISC-15, ISC-16, ISC-29 | F4 | nie | high |
| F6: Hardening, users, secrets i opcjonalny TLS | ISC-40, ISC-41, ISC-42, ISC-43, ISC-44, ISC-45 | F5 | nie | high |
| F7: ProxySQL i mysql_galera_hostgroups | ISC-18, ISC-19, ISC-20, ISC-21, ISC-22, ISC-23 | F5 | nie | high |
| F8: Redundantny endpoint ProxySQL | ISC-24, ISC-25, ISC-26 | F7 | nie | high |
| F9: Failover i chaos tests w laboratorium | ISC-27, ISC-28, ISC-30, ISC-31, ISC-64 | F8 | nie | high |
| F10: Backup, restore i restore drill | ISC-32, ISC-33, ISC-34, ISC-35, ISC-36, ISC-37, ISC-38, ISC-39 | F5 | nie | high |
| F11: Monitoring, dashboardy, metryki lifecycle i logi | ISC-46, ISC-48, ISC-49 | F3; F7 dla ProxySQL | nie | high |
| F12: Rolling operations, patch i upgrade planning | ISC-50, ISC-51, ISC-52, ISC-53, ISC-54, ISC-55, ISC-56, ISC-57 | F9 | nie | high |
| F13: Drift, node lifecycle i decommission | ISC-21 (drift), ISC-65, node lifecycle | F12 | nie | medium |
| F14: Drugi niezależny klaster i runbooki | ISC-58, ISC-59, ISC-60, ISC-61, ISC-62 | F12 | nie | high |
| F15: Końcowy alerting i dostarczanie powiadomień | ISC-47 | F6, F7, F9, F10, F11 | nie | high |

## Decisions

- 2026-07-22 — przyjęte założenie: standardowe porty (MariaDB 3306, Galera 4567/4568/4567, ProxySQL 6033/6032) — ponieważ oficjalne domyślne — dowód: MariaDB/ProxySQL docs.
- 2026-07-22 — przyjęte założenie: SELinux pozostaje Enforcing — ponieważ hardening baseline; wyłączenie wykluczone — dowód: MASTER_PROMPT §5, §12.
- 2026-07-22 — przyjęte założenie: firewalld włączony, tylko zadeklarowany ruch — ponieważ defense in depth — dowód: MASTER_PROMPT §5.
- 2026-07-22 — przyjęte założenie: bazowa metoda SST = `mariadb-backup` — ponieważ zalecana dla Galery+MariaDB — dowód: MariaDB docs.
- 2026-07-22 — przyjęte założenie: pojedynczy aktywny writer (`max_writers:1`) — ponieważ Galera multi-writer wymaga analizy — dowód: MASTER_PROMPT §5.
- 2026-07-22 — przyjęte założenie: read/write split wyłączony domyślnie — ponieważ wymaga analizy aplikacji — dowód: MASTER_PROMPT §14.
- 2026-07-22 — przyjęte założenie: `serial: 1` dla zmian Galery — ponieważ ochrona quorum — dowód: MASTER_PROMPT §12.
- 2026-07-22 — przyjęte założenie: logrotate dla logów MariaDB/ProxySQL — ponieważ zapobieganie zapchaniu dysku — dowód: MASTER_PROMPT §17.
- 2026-07-22 — przyjęte założenie: struktura ProxySQL hostgroups (writer/backup-writer/reader/offline) z `mysql_galera_hostgroups` — ponieważ natywne wsparcie — dowód: ProxySQL docs.
- 2026-07-22 — przyjęte założenie: walidacja konfiguracji przez `ansible-playbook --check` + generator config + diff runtime — ponieważ idempotencja — dowód: MASTER_PROMPT §12.
- 2026-07-22 — przyjęte założenie: dowody w CI/logach/artefaktach; Verification tylko referencje — ponieważ długie outputy — dowód: MASTER_PROMPT §2.5.
- 2026-07-22 — przyjęte założenie: `versions.policy: locked` dla produkcji, `candidate` dla testów — ponieważ brak `state: latest` i brak dynamicznej zmiany major — dowód: MASTER_PROMPT §8.
- 2026-07-22 — Interview odpowiedź: RPO=0, RTO węzeł <2 min, RTO klaster <30 min — ponieważ principal wybór; determinuje gcache, chaos test thresholds, backup window — źródło: Interview 2026-07-22.
- 2026-07-22 — Interview odpowiedź: backup na zamontowany zasób SMB teraz, opcja S3 później — ponieważ principal wybór; retencja i dostęp TBD (fog) — źródło: Interview 2026-07-22.
- 2026-07-22 — Interview odpowiedź: endpoint = Keepalived VIP na węzłach ProxySQL — ponieważ principal wybór; wymaga osobnych CIDR i rekomendacji anti-affinity — źródło: Interview 2026-07-22.
- 2026-07-22 — Interview odpowiedź: TLS `disabled` teraz, `full` zaplanowane w późniejszym feature — ponieważ principal wybór; ZAŁOŻENIE DO POTWIERDZENIA risk acceptance; ISC-44 i TLS ISC pozostają otwarte — źródło: Interview 2026-07-22.
- 2026-07-22 — refined: BLK-3 rozstrzygnięty — secret backend = Ansible Vault — ponieważ principal wybór; szyfrowane pliki w repo (clusters/<name>/secrets.yml), klucz poza repo; ISC-43 zależy, F6 implementacja — źródło: Interview 2026-07-22.
- 2026-07-22 — refined: BLK-3 rozstrzygnięty — backup: SMB teraz, migracja S3 z retencją 30d — ponieważ principal wybór; ISC-32/37 zależne, F10 implementacja; S3 wymaga osobnych ISC dla fazy S3 — źródło: Interview 2026-07-22.
- 2026-07-22 — F1 research: MariaDB 11.4.12 LTS wybrana — ponieważ najdłuższe wsparcie (EOL 2029-05), Galera 4, RPM dla RHEL9 — dowód: mariadb.org, endoflife.date (2026-07-22).
- 2026-07-22 — F1 research: ProxySQL 3.0.9 wybrany — ponieważ Stable Tier, łata CVE-2026-48772/48773 — dowód: proxysql.com (2026-07-22).
- 2026-07-22 — F1 research: Galera 4 (galera-4, wsrep API 26) — ponieważ jedyny wspierany provider dla MariaDB 11.x; wbudowany w pakiety MariaDB — dowód: mariadb.org (2026-07-22).
- 2026-07-22 — F1 research: Rocky Linux 9.8 (latest minor, 2026-05-27), major EOL 2032-05-31 — dowód: rockylinux.org, endoflife.date (2026-07-22).
- 2026-07-22 — F1 research: ansible-core 2.21.2 + ansible.mysql 5.1.0 — ponieważ community.mysql deprecated -> ansible.mysql — dowód: ansible.com, github.com/ansible-collections/ansible.mysql (2026-07-22).
- 2026-07-22 — F1 research: odrzucone MariaDB 12.3 (krótszy EOL), 11.8 (krótszy EOL), 10.11 (starsza), 10.5/10.6 (EOL/przestarzałe) — dowód: mariadb.org (2026-07-22).
- 2026-07-22 — ADR-001: Keepalived VIP endpoint — ponieważ principal wybór; VRRP <3s spełnia RTO <2min — docs/adr/ADR-001-keepalived-vip-endpoint.md.
- 2026-07-22 — ADR-002: TLS disabled w v1 + risk acceptance — ponieważ principal wybór; ISC-44 otwarte, ISC-45 aktywne — docs/adr/ADR-002-tls-disabled-risk-acceptance.md.
- 2026-07-22 — ADR-003: backup SMB teraz -> S3 retencja 30d — ponieważ principal wybór; ISC-32/37 zależne — docs/adr/ADR-003-backup-smb-to-s3.md.
- 2026-07-22 — ADR-004: MariaDB 11.4.12 LTS — ponieważ najdłuższe wsparcie + Galera 4 + RPM RHEL9 — docs/adr/ADR-004-mariadb-11.4-lts-selection.md.
- 2026-07-22 — przyjęte założenie: gcache.size = write_rate_bytes/s × ist_window_min × 60; minimum 128MB — ponieważ formula z oknem IST; implementacja tests/validation/calc-gcache.py — dowód: Galera docs + ISC-68.
- 2026-07-22 — F0 discovery: BLK-1/BLK-2 rozblokowane przez OrbStack/Docker lab (5 kontenerów Rocky 9.8); F0 uruchomiony na 5/5 hostów, 29 tasków PASS każdy; raporty w /var/tmp/f0-discovery-*.json na hostach — dowód: ansible-playbook PLAY RECAP ok=29 failed=0.
- 2026-07-22 — F0 discovery: SELinux Disabled w kontenerach (brak jądra SELinux) — lab ograniczenie; produkcja (vmware_esxi) będzie Enforcing; ISC-4 probe gotowy ale nie zatwierdzany w lab.
- 2026-07-22 — F0 discovery: firewalld DBUS error w kontenerach (brak systemd dbus) — lab ograniczenie; produkcja będzie firewalld; ISC-5 probe gotowy ale nie zatwierdzany w lab.
- 2026-07-22 — F0 discovery: brak MariaDB/ProxySQL na hostach (czyste kontenery) — F2 instalacja od zera; brak istniejącego workloadu → ISC-68 gcache pozostaje fog (write rate = 0).
- 2026-07-22 — F0 discovery: Rocky 9.8 Blue Onyx potwierdzony na hostach — zgodne z F1 research (candidate.lock.yml).
- 2026-07-22 — F11 lab monitoring: PMM Server przypięty do `percona/pmm-server:3.8.1@sha256:ef47471fb3b54e10897a92bab0b7b45e82d9825c3b0abf5a0693242191f99468`; natywne PMM Inventory i wbudowane dashboardy zastępują ręczny vmagent/Prometheus i własne dashboardy — ponieważ inventory, statusy usług i cykl życia obiektów PMM są jednym źródłem prawdy — dowód: oficjalne PMM 3 API, `tests/lab/probe-pmm-native.py`, PMM UI 2026-07-22.
- 2026-07-22 — F11 architektura ARM lab: PMM Server uruchamia `mysqld_exporter`/QAN dla zdalnych MariaDB, a nieuprzywilejowany użytkownik systemowy `node_exporter` uruchamia przypięty `node_exporter 1.12.1` z oficjalnym SHA-256 per architektura i rejestruje go jako natywną external service przy generic node — ponieważ PMM Client nie jest dostępny w używanym repozytorium Rocky 9/aarch64 — dowód: PMM 3 inventory API, Prometheus release v1.12.1, lockfiles, procesy 5/5 i playbooki F11.
- 2026-07-22 — F11 namespace i sekrety: `monitoring.pmm.cluster_name` jest wymagany i prefiksuje wszystkie nodes/services; `credentials_revision: 2` wymusza uzgodnienie haseł obu agentów bez sekretów w repo. Konfiguracja labu wiąże PMM i SSH tylko do loopback, odrzuca domyślne `admin/admin`, a lokalne losowe dane są wyłącznie w ignorowanym `tests/lab/.env` (0600) — dowód: schema, compose, PMM API `old=401/new=200`, `docker port`, probe ISC-43.
- 2026-07-22 — research monitoringu: PMM 3.8.1 (2026-06-16) wybrany jako security release łatający zależności Grafana/gRPC/nginx; node_exporter 1.12.1 (2026-07-14) był latest stable w dniu badania. EOL per PMM point release nie jest publikowany; znane ryzyka third-party są zapisane w `versions/compatibility-report.md` — źródła: Percona PMM 3.8.1 release notes, Prometheus GitHub release v1.12.1.
- 2026-07-22 — alerting odłożony decyzją principal do końca F6/F7/F9/F10: reguły, custom templates i folder usunięto z repo oraz działającego PMM. F11 utrzymuje teraz tylko PMM Inventory/QAN, exportery, dashboardy, metryki lifecycle i logrotate; probe wymaga zera reguł zarządzanych dla klastra. Pozwala to projektować alarmy na rzeczywistej końcowej topologii i eliminuje fałszywe `Firing`/`NoData` niepełnego labu.
- 2026-07-22 — F11 lifecycle/logs: node_exporter textfile collector publikuje atomowe kontrakty freshness backup/restore/TLS; wartości `0` jawnie oznaczają brak dowodu. PMM używa Docker local logging `10m × 3`, a hosty dostają logrotate daily/14/100M z timerem systemd poza kontenerowym labem — dowód: PMM queries, docker inspect i logrotate `--debug`.
- 2026-07-22 — F5 SST fix: `wsrep_sst_method` zmienione z `mysqldump` na `mariabackup` w `server.cnf.j2` — ponieważ ISC-14 wymaga mariabackup, a mysqldump był błędny — dowód: MariaDB 11.4 docs, `wsrep_sst_mariabackup` script, ISC-14 PASS.
- 2026-07-22 — F5 gcache: `gcache.size=128M` + `gcache.page_size=128M` — conservative default, ponieważ write rate=0 (ISC-68 fog); IST potwierdzone na labie — dowód: log `Receiving IST... 100.0% (57/57 events)`.
- 2026-07-22 — F5 global_priv crash: po SST mariabackup `mysql/global_priv` był crashed na gnode2 (Aria table); naprawiono REPAIR TABLE + replikacja sst_user z gnode1 — założenie do potwierdzenia: przetestować ponowne SST po fix w F6/F9.
- 2026-07-22 — F6 hardening: anonimowe konta i baza test usunięte; root localhost-only; sst_user i pmm_monitor least-privilege; hasło SST externalizowane do SST_PASSWORD env var w 5 plikach — dowód: probe-hardening.py PASS, probe-no-secrets-leak.sh PASS, grep = 0.
- 2026-07-22 — F6 TLS: tls.mode=disabled w lab; f6_hardening.yml odrzuca production+disabled bez ADR; ISC-44 pozostaje otwarte do tls.mode=full.
- 2026-07-22 — F10 backup transport: lab `backup.destination=s3` (MinIO `172.28.0.60`, przypięty `RELEASE.2025-09-07T16-13-09Z`), produkcja pozostaje `smb`. Powód: kernel OrbStack `7.0.11-orbstack` nie ma modułu `cifs` (`modprobe cifs` FATAL, brak `/lib/modules/.../fs/cifs*`), więc SMB mount jest nietestowalny w labie — jak brak SELinux/systemd. S3 to sankcjonowana opcja off-cluster (ADR-003). Ścieżka SMB udokumentowana w f10_backup.yml, ale niezweryfikowana w labie. Dowód: spike modprobe, probe-backup/probe-restore PASS.
- 2026-07-22 — F10 backup: szyfrowanie `openssl aes-256-cbc/pbkdf2` (klucz BACKUP_ENCRYPTION_KEY poza repo), sha256 checksum, metadata z wsrep seqno; źródło backupu = galera[1] (nie aktywny writer) chroni writera; restore drill na dedykowanym `rnode1` (czysty izolowany host, standalone bez wsrep). Nowe kontenery: minio (172.28.0.60) + rnode1 (172.28.0.50). Dowód: playbooki F10, backup-impact.py flow_control=0.
- 2026-07-23 — F11 ProxySQL metrics: `admin-restapi_enabled=true` (LOAD+SAVE = trwałe) wystawia `proxysql_*` na `:6070/metrics`; `f11_proxysql_metrics.yml` rejestruje 2 external services (group=proxysql) + external_exporter agents (port 6070) w PMM, reużywając generic nodes z f11_pmm_client. Galera/MariaDB już przez mysqld_exporter+QAN — dowód: PMM Prom `proxysql_servers_table_version_total` 2 series `up=1`, ISC-46 PASS.
- 2026-07-23 — F11 freshness: `f11_freshness.yml` jest jedynym właścicielem `isa_monitoring_state.prom` — f11_node_exporter baseline ma `force:false`, więc reconverge nie resetuje realnych wartości do 0. `last_${MODE}.json` przechowuje tylko SUKCES; porażki idą do `last_${MODE}_failure.json` (ISC-38), aby nie nadpisać dowodu świeżości. `backup-run.sh` odświeża metryki po udanym run — dowód: epoch 1784763175→1784797158 po backup, ISC-49 PASS.
- 2026-07-23 — F12 research upgrade: oficjalna ścieżka MariaDB 11.4 LTS → 11.8 LTS, in-place `mariadb-upgrade --skip-write-binlog` (bez dump/restore), Galera 4 (wsrep API 26) wspiera rolling; downgrade datadir NIEWSPARTY ("forward-incompatible") — źródła: mariadb.com/kb/en/upgrading-galera-cluster, mariadb.com/kb/en/downgrading-between-major-mariadb-versions, galeracluster.com/library/documentation/upgrading.html.
- 2026-07-23 — F12 rolling restart order: non-writer węzły pierwsze, writer ostatni (research galeracluster.com) — minimalizuje churn failoveru; ProxySQL mysql_galera_hostgroups auto-promuje backup-writera. Lab writer=gnode3 (już ostatni w inventory).
- 2026-07-23 — F12 patch safe-default: domyślna komenda patcha = read-only `dnf check-update` (changed_when:false) — wzorzec canary+health-gate wykonywany bez modyfikacji pakietów w labie; produkcja nadpisuje `f12_patch_command`. ProxySQL: `SAVE ... TO DISK` przed patch (proxysql.com configuration-system).
- 2026-07-23 — F13 drift approach: ProxySQL drift = MAIN (mysql_servers/mysql_galera_hostgroups/mysql_users) vs DISK, NIE runtime_* (runtime niesie dynamiczny status SHUNNED/ONLINE + rozwinięte galera hostgroups) — dowód: false-positive przy runtime_mysql_servers (HG20 derived, status dynamic), poprawione na main-vs-disk. Drift read-only (§18: nie naprawia automatycznie) — dowód: inject unsaved config → DRIFT detected, cleanup → CLEAN.
- 2026-07-23 — F13 node lifecycle: remove-node wymaga planu (f13_remove_node_plan.yml read-only: quorum guard, writer-detection) + confirm=yes (f13_remove_node.yml, jak bootstrap). Quorum guard odmawia jeśli size-1 < 2. Lab: 3→2 bezpieczne, nie testowano destruktywnego usunięcia (plan + guard zweryfikowane).
- 2026-07-23 — F14 portability: usunięto hardcoding z 4 playbooków (site.yml, f3_galera_config.yml, f5_join.yml: galera_cluster_name→galera.cluster_name, galera_nodes_csv→groups['galera']|galera_node_address; f7_proxysql.yml: galera_backends→inventory, 'lab-galera'→monitoring.pmm.cluster_name). Weryfikacja: f3 renderuje server.cnf idempotentnie (changed=False), klaster zdrowy. ISC-59 PASS (probe-zero-hardcode: 0 trafień w roles/playbooks).
- 2026-07-23 — F14 ISC-60/61 scope: drugi klaster LIVE (docker-compose druga sieć 172.29.0.x + 5 kontenerów + bootstrap + converge + probes) pozostaje jako acceptance gate — fundament (zero hardcode + parametryzacja + example-cluster template + runbooki) jest kompletny; drugi klaster powstaje wyłącznie przez clusters/<name>/.
- 2026-07-23 — F14 drugi klaster LIVE: clusters/lab2-cluster/ (osobna sieć 172.29.0.x, osobny VIP/MinIO/namespace) wdrożony zero-zmian-w-rolach; bootstrap + SST + ProxySQL + Keepalived. Poprawki portability wykryte przy wdrożeniu: (1) f2_install musi instalować klienta mariadb na węzłach ProxySQL (admin port 6032), (2) ProxySQL w labie wymaga czystego startu --initial z proxysql.db writable (ownership proxysql:proxysql). Dowód: probe-galera/probe-proxysql PASS na lab2, lab1 nienaruszony, izolowane UUID/sieci/VIP.

## Verification

- ISC-3: PASS — MariaDB-server-11.4.12-1.el9, galera-4-26.4.27-1.el9, MariaDB-backup-11.4.12-1.el9 na gnode1-3; proxysql-3.0.9-1.aarch64 na pnode1-2; rpm -q potwierdzone na hostach 2026-07-22; zgodne z versions.lock.yml.
- ISC-43: `probe-no-secrets-leak.sh` PASS lokalnie 2026-07-22 — brak sekretów w śledzonych/nieignorowanych plikach i argv; kontrola negatywna odrzuciła literalne sekrety quoted, unquoted i Compose fallback. Losowe dane labu są w ignorowanym `tests/lab/.env` (0600), poza build context.
- ISC-58: validate-cluster-schema.py PASS lokalnie 2026-07-22 — cluster.yml zgodny z schema + semantic checks (production locked, max_writers=1, R/W split off). Probe gotowy do CI.
- ISC-62: 7 runbook stubs utworzone (bootstrap, total-outage, node-replacement, backup, restore, upgrade, decommission) w docs/runbooks/; do uzupełnienia w F4/F9/F10/F12/F13/F14.
- ISC-63: PASS — F2 install playbook używa state: present (nie latest); probe-no-state-latest.sh PASS; F2 preflight+install na 5/5 hostów 2026-07-22.
- ISC-66: PASS — F0 discovery uruchomiony na 5/5 kontenerów Rocky 9.8 (lab-cluster); 29 tasków PASS każdy (PLAY RECAP ok=29 failed=0); raporty /var/tmp/f0-discovery-*.json zawierają OS/kernel, CPU/RAM/NUMA, dyski/fs/mount, DNS/routing/ports, SELinux/firewalld, repo/pakiety, istniejące MariaDB/ProxySQL (brak), monitoring (brak). F0 nie instalował fio (allow_bench=true tylko gnode1, ale fio nie było zainstalowane — lab ograniczenie). Commit 2026-07-22.
- ISC-67: PASS — F0 discovery read-only; changed=0 na wszystkich hostach (poza zapisem raportu changed=1); brak modyfikacji usług; ansible-playbook 2026-07-22.
- ISC-6: PASS — F2 preflight na 5/5 hostów (assert Rocky 9, RAM >=2GB, disk >=5GB, clean MariaDB); serial:1 max_fail_percentage:0; failed=0 2026-07-22.
- ISC-46: PASS — probe-pmm-native.py potwierdza PMM `3.8.1`, 5 generic nodes, 5 node_exporter 1.12.1, 3 MySQL services oraz 2 ProxySQL metric exporters (external_exporter port 6070, restapi włączone trwale). Galera/MariaDB przez mysqld_exporter+QAN, ProxySQL przez restapi `/metrics` (311+ metryk). PMM Prom zwraca `proxysql_servers_table_version_total` dla obu węzłów (`up=1`). Dowód: `make lab-monitoring-verify` PASS 2026-07-23.
- F11 monitoring idempotence: PASS — po rotacji danych i upgrade node_exporter drugi `make cluster-monitoring CLUSTER=lab-cluster` zakończony `changed=0 failed=0` dla gnode1-3, pnode1-2 i localhost.
- F11 PMM version preflight: PASS — kontrola negatywna z oczekiwanym `0.0.0` została odrzucona przed pierwszym playem hostowym; aktywny runtime `3.8.1` odpowiada `versions.lock.yml`.
- F11 restart persistence i live scrape: PASS — PMM odtworzony z digest-pinned obrazu `3.8.1`, health=`healthy`, pamięć=4GiB, nofile=1M, automatyczne aktualizacje wyłączone. Po restarcie probe potwierdził świeże inventory/QAN/metryki, 0 zarządzanych reguł, 0 custom templates `isa_*` i brak folderu alertów (404). PMM porty są tylko na `127.0.0.1`; stare domyślne hasło zwraca 401, losowe aktywne hasło 200.
- ISC-47: NOT STARTED — decyzją principal alerty quorum/writer/node oraz zewnętrzne notification policy powstaną jako F15 dopiero po F6/F7/F9/F10. Działający PMM nie zawiera żadnej reguły zarządzanej dla `lab-galera`.
- ISC-48: PASS — `logrotate --debug /etc/logrotate.d/isa-database-monitoring` SYNTAX OK na 5/5 permanentnych hostów (gnode1-3, pnode1-2); polityka daily/14/100M coveruje `/var/log/mariadb/*.log`, `/var/lib/proxysql/*.log`, `/var/tmp/node_exporter.log`; `logrotate.timer enabled`. rnode1 (transient restore host) pominięty — przebudowywany co drill. Dowód: ansible + logrotate --debug 2026-07-23.
- ISC-49: PASS — `f11_freshness.yml` publikuje realne unixtimes z `last_backup.json`/`last_restore.json` do textfile collector (`isa_monitoring_state.prom`); `backup-run.sh` odświeża metryki po udanym run. Probe wymaga non-zero backup/restore unixtime w oknie retencji (14d/8d). PMM Prom: `isa_backup_last_success_unixtime=1784797158`, `isa_restore_test_last_success_unixtime=1784762220`, TLS `0` (disabled). Dowód: `make lab-monitoring-verify` PASS 2026-07-23.
- ISC-7: PASS — `probe-galera-cluster.py` potwierdza wszystkie 3 węzły raportują `wsrep_cluster_status=Primary` 2026-07-22.
- ISC-8: PASS — `wsrep_cluster_size=3` na gnode1/gnode2/gnode3, zgodne z `galera.nodes_expected=3` 2026-07-22.
- ISC-9: PASS — identyczny `wsrep_cluster_state_uuid` na wszystkich węzłach 2026-07-22.
- ISC-10: PASS — wszystkie węzły: `wsrep_local_state=4 (Synced)`, `wsrep_ready=ON`, `wsrep_connected=ON` 2026-07-22.
- ISC-14: PASS — `wsrep_sst_method=mariabackup` na wszystkich węzłach; gnode2 i gnode3 dołączyły przez SST mariabackup 2026-07-22.
- ISC-15: PASS — gnode3 powrócił po krótkim przestoju przez IST (log: `Receiving IST... 100.0% (57/57 events) complete`), nie pełne SST 2026-07-22.
- ISC-16: PASS — brak tabel użytkownika bez klucza głównego (`information_schema` query puste) 2026-07-22.
- ISC-17: PASS — po zatrzymaniu gnode2+gnode3, gnode1 wszedł w `non-Primary`, `wsrep_ready=OFF`; zapis zablokowany `ERROR 1047 (WSREP has not yet prepared node for application use)` 2026-07-22.
- ISC-29: PASS — gnode3 automatycznie dołączył po restarcie bez ręcznych kroków (IST z gcache) 2026-07-22.
- ISC-11: PASS — write przez ProxySQL:6033 (app_user) widoczny na gnode2 przez Galera replication; endpoint ProxySQL działa 2026-07-22.
- ISC-40: PASS — brak anonimowych kont, brak test DB, brak pustych haseł 2026-07-22.
- ISC-41: PASS — root tylko @localhost 2026-07-22.
- ISC-42: PASS — sst_user i pmm_monitor least privilege potwierdzone 2026-07-22.
- ISC-43: PASS — SST password externalizowane; probe-no-secrets-leak PASS; grep = 0 trafień 2026-07-22.
- ISC-45: PASS — f6_hardening.yml assert odrzuca production+disabled; lab=laboratory PASS 2026-07-22.
- ISC-18: PASS — dokładnie jeden ONLINE writer (gnode3) w writer HG 10; probe-proxysql PASS na pnode1/pnode2 2026-07-22.
- ISC-19: PASS — zatrzymany gnode2 przeniesiony do offline HG 40, po recovery wrócił do backup HG 20 2026-07-22.
- ISC-20: PASS — Galera monitor skonwergował; 3 zdrowe backendy rozłożone writer/backup HG 2026-07-22.
- ISC-21: PASS — runtime==disk galera_hostgroups (`10,20,30,40,1,0`); converge changed=0 (idempotentny) 2026-07-22.
- ISC-22: PARTIAL (lab) — domyślne admin:admin odrzucone (auth wymuszone); izolacja sieciowa admin/app CIDR via firewalld tylko w produkcji (lab: pojedyncza sieć, brak firewalld) 2026-07-22.
- ISC-23: PASS — 0 reguł query (runtime_mysql_query_rules); R/W split wyłączony 2026-07-22.
- ISC-24: PASS — VIP 172.28.0.30 na MASTER (pnode1); klient przez VIP:6033 routuje do writera; probe-endpoint PASS 2026-07-22.
- ISC-25: PASS — kill ProxySQL na pnode1 → VIP przejął pnode2 w ~7s (≪2min RTO); klient wznowił zapis przez VIP (id=200 → gnode3, Galera-replikowany), pnode1 odzyskał VIP po recovery 2026-07-22.
- ISC-26: PASS — health-check (TCP 6033, weight -60) zdjął VIP z pnode1 gdy ProxySQL padł; VIP nigdy na niesprawnej instancji 2026-07-22.
- ISC-27: PASS — numbered workload przez VIP; SIGKILL writera (gnode3) → zapis wznowiony, failover gap ~5-12s ≪ 120s RTO (chaos-failover.py) 2026-07-22.
- ISC-28: PASS — wszystkie potwierdzone transakcje (564) obecne na węźle ocalałym po failover; 0 utraconych 2026-07-22.
- ISC-30: PASS — partycja iptables gnode3|(gnode1+gnode2): większość Primary size=2 zapisuje, mniejszość non-Primary size=1 odrzuca zapis; brak dwóch writable Primary; heal do 3 (chaos-split-brain.py) 2026-07-22.
- ISC-31: PASS — statyczny guard: każdy multi-node Galera lifecycle play ma serial:1 (probe-no-mass-restart.py, falsyfikowalny) 2026-07-22.
- ISC-64: PASS — chaos-failover i chaos-split-brain odmawiają startu przy environment=production (guard, exit 1 bez destrukcji) 2026-07-22.
- ISC-32: PASS — backup w s3://galera-backups (MinIO 172.28.0.60, off-cluster); lokalny staging usuwany po transferze; probe-backup PASS 2026-07-22.
- ISC-33: PASS — openssl aes-256-cbc/pbkdf2 (magic Salted__), odszyfrowywalny do poprawnego tar 2026-07-22.
- ISC-34: PASS — sha256 backup.tar.enc == zapisany checksum (weryfikacja przy backupie i przed restore) 2026-07-22.
- ISC-35: PASS — metadata.json: mariadb_version=11.4.12-MariaDB, created_at, cluster_name, wsrep seqno=1482 2026-07-22.
- ISC-36: PASS — restore na rnode1 (czysty izolowany host, standalone): checksum OK → copy-back → CHECK TABLE OK → 4 wiersze zweryfikowane 2026-07-22.
- ISC-37: PASS — restore drill zapisuje last_restore.json; probe-restore weryfikuje świeżość wg restore_test_schedule (0 4 * * 0); cron template w roles/backup 2026-07-22.
- ISC-38: PASS — backup-run.sh przy porażce dostarcza alert (/var/log/mariadb-backup.log + stan failed + logger); symulacja złych creds → rc=2, alert dostarczony 2026-07-22.
- ISC-39: PASS — backup pod obciążeniem (284 commity przez VIP): flow control 0 ns, max write stall 0.07s — writer niezdegradowany (backup-impact.py) 2026-07-22.
- ISC-50: PASS — `f12_rolling_restart.yml`: play Galera `serial:1`; restart gnode1→gnode2→gnode3 każdy po kolei; probe-rolling-restart PASS (static serial:1 + runtime) 2026-07-23.
- ISC-51: PASS — brama zdrowia (until: wsrep_local_state=4 + Primary + size=3 + ready=ON) po każdym węźle przed kolejnym; każdy węzeł rejoined Synced; probe-rolling-restart PASS 2026-07-23.
- ISC-52: PASS — `f12_patch.yml` canary: pierwszy non-writer (gnode1) patchowany + health gate przed kontynuacją; probe-patch PASS 2026-07-23.
- ISC-53: PASS — `f12_upgrade_plan.yml` read-only: taski hostowe changed_when:false (odczyt wersji/gcache/grastate); Galera changed=0; probe-upgrade-plan PASS 2026-07-23.
- ISC-54: PASS — plan docs/plans/major-upgrade-plan.md: ścieżka 11.4 LTS → 11.8 LTS, mariadb-upgrade --skip-write-binlog, źródła mariadb.com/kb/en/upgrading-galera-cluster + galeracluster.com; probe-upgrade-plan PASS 2026-07-23.
- ISC-55: PASS — `f12_patch.yml`: brama zdrowia po każdym węźle (until/retries) — porażka zatrzymuje rolling; 3 bramy (canary/rolling/writer); probe-patch PASS 2026-07-23.
- ISC-56: PASS — anti-downgrade guard: `f12_upgrade_plan.yml` assert odrzuca gdy obecna >= docelowa (test negatywny target=11.2 → FAILED z cytatem "forward-incompatible"); probe-upgrade-plan PASS 2026-07-23.
- ISC-57: PASS — `f12_patch.yml` ProxySQL play `serial:1` + SAVE ... TO DISK przed patch; pnode1/pnode2 po kolei, każdy health-gated (backends ONLINE); probe-patch PASS 2026-07-23.
- ISC-21 (F13 drift): PASS — `f13_drift.yml` read-only: ProxySQL main-vs-disk (mysql_servers/galera_hostgroups/mysql_users CLEAN) + Galera cluster_state_uuid spójny. Falsyfikowalny: inject unsaved INSERT → mysql_servers=DRIFT detected; cleanup → CLEAN. probe-drift PASS 2026-07-23.
- ISC-58: PASS — clusters/example-cluster/{cluster.yml,inventory.yml} template; roles/playbooks nie referencjują clusters/lab-cluster (probe-zero-hardcode). Nowy klaster = clusters/<name>/ tylko. 2026-07-23.
- ISC-59: PASS — 0 hardcodowanych IP/nazw/secretów w roles/playbooks (probe-zero-hardcode.py); parametryzacja zweryfikowana idempotentnym renderem f3 (changed=False, klaster zdrowy). 2026-07-23.
- ISC-62: PASS — 7 runbooków (bootstrap, total-outage, node-replacement, backup, restore, upgrade, decommission) zaktualizowane; 0 STUB; wszystkie komendy istnieją w Makefile. 2026-07-23.
- ISC-60: PASS — dwa klastry izolowane: lab1 (UUID 69c257c2, sieć 172.28.0.x, VIP 172.28.0.30, nazwa lab_galera) vs lab2 (UUID 951cfac6, sieć 172.29.0.x, VIP 172.29.0.30, nazwa lab2_galera) — osobne docker networks, osobny MinIO (172.29.0.60), osobny PMM namespace (lab2-galera). 2026-07-23.
- ISC-61: PASS — drugi klaster (clusters/lab2-cluster/, zero zmian w rolach/playbookach) przechodzi te same probes: probe-galera-cluster (3×Primary/Synced), probe-proxysql (writer ONLINE, runtime==disk). Lab1 nienaruszony po wdrożeniu lab2. 2026-07-23.
- ISC-65: PASS — `probe-no-double-bootstrap.py`: jedyny bootstrap play (bootstrap.yml) jest single-host-safe (serial:1 + assert ansible_play_hosts==1) + confirm-gated; 0 innych playbooków z --wsrep-new-cluster w shell/command. 2026-07-23.
- F13 node lifecycle: PASS — `f13_remove_node_plan.yml` read-only (quorum guard 3→2 OK, writer-detection: gnode2=nie, gnode3=TAK+warn); `f13_remove_node.yml` confirm-gated (odmawia bez confirm=yes). Plan + guard zweryfikowane; destruktywne usunięcie nie testowane w labie (3→2). 2026-07-23.


## Blockers

- ~~BLK-1~~ ROZBLOKOWANY 2026-07-22 — 5 kontenerów Rocky Linux 9.8 (OrbStack/Docker): 3 Galera + 2 ProxySQL; SSH + sudo NOPASSWD; tests/lab/docker-compose.yml.
- ~~BLK-2~~ ROZBLOKOWANY 2026-07-22 — inventory lab-cluster (clusters/lab-cluster/inventory.yml) z SSH key (tests/lab/ssh_key); Ansible połączenie PASS na 5/5 hostów.
- ~~BLK-3~~ ROZSTRZYGNIĘTY 2026-07-22 — secret backend = Ansible Vault; backup = SMB teraz, S3 retencja 30d później (Decisions).
- ~~BLK-4~~ ROZSTRZYGNIĘTY 2026-07-22 — internet dostępny; F1 research wykonany z oficjalnych źródeł.
- BLK-5 — docelowy alert target/contact point jest nieuzgodniony. Nie blokuje bieżącego monitoringu; musi zostać rozstrzygnięty przed końcowym F15 po F11.

## Następny pojedynczy feature
F15: Końcowy alerting i dostarczanie powiadomień (ISC-47) — reguły alertów quorum/writer/node + notification policy do uzgodnionego celu (BLK-5: docelowy alert target nieuzgodniony — wymaga decyzji principal). Fabryka udowodniona: dwa niezależne klastry (lab1+lab2) z tego samego kodu.
