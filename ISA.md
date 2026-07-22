---
task: "Zbuduj fabrykę klastrów Galera i ProxySQL"
slug: "20260722-172704_galera-proxysql-cluster-factory"
effort: comprehensive
effort_source: explicit
phase: build
progress: 2/68
mode: iterate
started: "2026-07-22T15:27:04Z"
updated: "2026-07-22T15:59:38Z"
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
- [ ] ISC-3: Wersje MariaDB, mariadb-backup, Galera provider, ProxySQL i kolekcji Ansible są dokładnie zgodne z `versions.lock.yml`.
- [ ] ISC-4: SELinux pozostaje w trybie Enforcing po pełnym deploy.
- [ ] ISC-5: Firewalld działa i dopuszcza wyłącznie zadeklarowany ruch (Galera, ProxySQL, admin, monitoring) na wszystkich hostach.
- [ ] ISC-6: Anti: Nieudany preflight nie zostawia częściowych zmian — konfiguracja hostów pozostaje niezmieniona, gdy preflight FAIL.

### Galera
- [ ] ISC-7: W klastrze istnieje dokładnie jeden Primary Component.
- [ ] ISC-8: `wsrep_cluster_size` równa się `galera.nodes_expected` na każdym węźle.
- [ ] ISC-9: `wsrep_cluster_state_uuid` jest identyczny na wszystkich węzłach.
- [ ] ISC-10: Każdy węzeł raportuje `wsrep_connected=ON`, `wsrep_ready=ON`, `wsrep_local_state=4 (Synced)`.
- [ ] ISC-11: Zapis wykonany przez publiczny endpoint ProxySQL jest widoczny na pozostałych węzłach Galery (replikacja sync).
- [ ] ISC-12: Initial bootstrap wykonuje się tylko na jednym jawnie wybranym węźle i wymaga jawnego potwierdzenia.
- [ ] ISC-13: Anti: Zwykły `site.yml`/`converge.yml` nigdy nie wykonuje initial bootstrap.
- [ ] ISC-14: SST nowego węzła używa metody `mariadb-backup`.
- [ ] ISC-15: Powracający węzeł używa IST, gdy mieści się w zmierzonym oknie gcache.
- [ ] ISC-16: Brak klucza głównego na jakiejkolwiek tabeli użytkownika jest blockerem deploy.
- [ ] ISC-17: Utrata większości węzłów blokuje zapisy (cluster w stanie non-Primary, `wsrep_ready=OFF`).

### ProxySQL
- [ ] ISC-18: W runtime hostgroup istnieje dokładnie jeden aktywny writer.
- [ ] ISC-19: Węzeł non-Primary, non-Synced, not Ready lub przekraczający zatwierdzony lag jest wyłączony z ruchu ProxySQL.
- [ ] ISC-20: Monitorowanie Galery w ProxySQL osiąga poprawny stan w określonym progu czasu po deploy.
- [ ] ISC-21: Konfiguracja runtime i disk ProxySQL jest zgodna z repo (brak driftu po converge).
- [ ] ISC-22: Admin port ProxySQL (6032) nie jest osiągalny z application CIDR.
- [ ] ISC-23: Anti: Read/write splitting pozostaje wyłączony, dopóki osobna analiza aplikacji go nie zatwierdzi.

### Endpoint HA
- [ ] ISC-24: Endpoint Keepalived VIP działa, gdy oba ProxySQL są zdrowe.
- [ ] ISC-25: Awaria aktywnego ProxySQL nie przekracza RTO węzła (<2 min) — klient wznawia połączenie przez VIP.
- [ ] ISC-26: VIP nie kieruje ruchu do niesprawnego ProxySQL (health-check odrzuca węzeł).

### Failover i quorum
- [ ] ISC-27: Klient prowadzący numerowany workload wznawia zapis po utracie aktywnego writera w RTO (<2 min).
- [ ] ISC-28: Żadna potwierdzona (commitowana) transakcja nie znika po failover.
- [ ] ISC-29: Powracający węzeł dołącza do klastra bez ręcznych kroków.
- [ ] ISC-30: Anti: Split-brain nie powstaje — nigdy nie istnieją dwa niezależne Primary Components zapisujące.
- [ ] ISC-31: Anti: Żaden playbook nie restartuje wszystkich węzłów Galery jednocześnie.

### Backup i restore
- [ ] ISC-32: Backup opuszcza klaster (trafia na off-cluster zasób SMB; późniejsza opcja S3).
- [ ] ISC-33: Backup jest zaszyfrowany.
- [ ] ISC-34: Checksum backupu jest poprawna i weryfikowalna.
- [ ] ISC-35: Metadata backupu zawiera wersję MariaDB, czas, cluster name i pozycję wsrep/seqno.
- [ ] ISC-36: Restore na czysty izolowany host przechodzi test integralności (checksum + zapytanie).
- [ ] ISC-37: Restore drill działa według ustalonego harmonogramu.
- [ ] ISC-38: Nieudany backup lub przeterminowany restore test generuje alert.
- [ ] ISC-39: Backup nie degraduje aktywnego writera ponad uzgodniony threshold (queue/flow control).

### Bezpieczeństwo
- [ ] ISC-40: Brak anonimowych kont, testowej bazy i pustych haseł.
- [ ] ISC-41: Root nie loguje się zdalnie (tylko localhost/UNIX socket).
- [ ] ISC-42: Konta SST, monitor i app mają minimalne uprawnienia (least privilege).
- [ ] ISC-43: Anti: Sekrety nie występują w repo, logach CI, diffach ani argv procesu.
- [ ] ISC-44: W trybie `tls.mode=full` połączenie z niezaufanym lub nieważnym certyfikatem jest odrzucane.
- [ ] ISC-45: W trybie `tls.mode=disabled` w profilu production powstaje jawne ostrzeżenie i udokumentowane risk acceptance.

### Obserwowalność
- [ ] ISC-46: Metryki Galery, MariaDB i ProxySQL trafiają do istniejącego systemu monitoringowego.
- [ ] ISC-47: Alert powstaje po utracie quorum, utracie writera lub utracie węzła.
- [ ] ISC-48: Logi Galery/MariaDB/ProxySQL rotują się zgodnie z logrotate.
- [ ] ISC-49: Backup age, restore-test age i certificate expiry są monitorowane.

### Rolling operations i upgrade
- [ ] ISC-50: Rolling restart odbywa się z `serial: 1`.
- [ ] ISC-51: Kolejny węzeł nie jest ruszany przed odzyskaniem zdrowia (Synced + health check) przez poprzedni.
- [ ] ISC-52: Patching ma canary (jeden węzeł poza aktywnym writerem).
- [ ] ISC-53: Plan major upgrade jest read-only (generuje plan, nie modyfikuje hostów).
- [ ] ISC-54: Ścieżka major upgrade pochodzi z oficjalnej dokumentacji MariaDB/Galera.
- [ ] ISC-55: Upgrade zatrzymuje się po utracie zdrowia klastra.
- [ ] ISC-56: Anti: Major rollback nie wykonuje downgrade istniejącego datadir.
- [ ] ISC-57: ProxySQL aktualizuje się osobno, jedną instancję naraz.

### Multi-cluster
- [ ] ISC-58: Nowy klaster wymaga wyłącznie nowego katalogu `clusters/<name>/` (inventory.yml + cluster.yml).
- [ ] ISC-59: Role i playbooki nie zawierają danych konkretnego klastra (zero hardcodowanych IP/nazw/sekretów).
- [ ] ISC-60: Dwa klastry mają osobne nazwy, sieci, sekrety i endpointy.
- [ ] ISC-61: Uruchomienie drugiego klastra przechodzi te same testy co pierwszy.
- [ ] ISC-62: README i runbooki obejmują bootstrap, total outage, node replacement, backup, restore, upgrade i decommission.

### F0 Discovery
- [x] ISC-66: Raport discovery zawiera fakty: OS/kernel, CPU/RAM/NUMA, dyski/filesystem/mount/wolne miejsce, IOPS+fsync (fio), DNS/routing/osigalność portów, chrony/NTP, SELinux/firewalld, repozytoria+pakiety, istniejące MariaDB/ProxySQL, monitoring, secret backend, audyt PK, write rate.
- [x] ISC-67: Anti: F0 discovery nie modyfikuje stanu usług produkcyjnych (read-only względem usług).
- [ ] ISC-68: `gcache.size` jest wyliczony z mierzonego write rate i wymaganego okna IST i zapisany w raporcie/Decisions.

### Obowiązkowe Anti
- [ ] ISC-63: Anti: Żaden task produkcyjny nie używa `state: latest`.
- [ ] ISC-64: Anti: Dekonstrukcyjne testy (chaos, failover, restore drill) nie uruchamiają się na profilu production.
- [ ] ISC-65: Anti: Dwa węzły nigdy nie są bootstrapowane jako niezależne Primary Components.

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
| ISC-46 | literal | bash | scrape metryk z docelowym systemem | metryki obecne | prometheus/zabbix |
| ISC-47 | literal | bash | utrata quorum/writera/node → alert | alert dostarczony do celu | monitoring |
| ISC-48 | literal | bash | `logrotate -d` + sprawdzenie rotacji | rotuje się | logrotate |
| ISC-49 | literal | bash | metryki backup age / restore-test age / cert expiry | monitorowane | monitoring |
| ISC-50 | literal | bash | playbook rolling restart, `ansible` output | `serial: 1` | ansible |
| ISC-51 | literal | bash | kolejność + health check między node | Synced przed kolejnym | ansible + mariadb |
| ISC-52 | literal | bash | patch plan + wykonanie | canary na non-writer | ansible |
| ISC-53 | literal | bash | `upgrade-plan.yml` w check mode | brak zmian na hostach | ansible --check |
| ISC-54 | derived: official-upgrade-path | bash | plan vs oficjalna docs MariaDB/Galera | ścieżka zgodna | docs diff |
| ISC-55 | literal | bash | upgrade z wymuszonym utratą zdrowia | stop | ansible + health |
| ISC-56 | Anti: no-datadir-downgrade | bash | próba rollback na stary datadir | brak downgrade datadir | ansible + `stat` |
| ISC-57 | literal | bash | upgrade ProxySQL, obserwacja | jedna instancja naraz | ansible + proxysql |
| ISC-58 | literal | bash | drugi klaster: tylko `clusters/<name>/` + run | deploy przechodzi | ansible |
| ISC-59 | literal | bash | `grep` ról/playbooków po IP/hasłach/nazwach klastra | brak | grep |
| ISC-60 | literal | bash | porównanie dwóch klastrów | osobne nazwy/sieci/sekrety/endpointy | diff + mariadb |
| ISC-61 | literal | bash | drugi klaster te same sondy co pierwszy | PASS | wszystkie sondy |
| ISC-62 | literal | bash | lista runbooków + weryfikacja treści | bootstrap/outage/replace/backup/restore/upgrade/decommission | docs + linkcheck |
| ISC-63 | Anti: no-state-latest | bash | `grep -r "state: latest"` ról/playbooków produkcyjnych | brak | grep |
| ISC-64 | Anti: no-destruction-in-prod | bash | profil + playbook chaos/restore | produkcyjny profil nie wchodzi w chaos | ansible + profile guard |
| ISC-65 | Anti: no-dual-primary-bootstrap | bash | drugi bootstrap przy istniejącym Primary | zablokowany | ansible + wsrep |
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
| F11: Monitoring, logi i alerty | ISC-46, ISC-47, ISC-48, ISC-49 | F7 | nie | high |
| F12: Rolling operations, patch i upgrade planning | ISC-50, ISC-51, ISC-52, ISC-53, ISC-54, ISC-55, ISC-56, ISC-57 | F9 | nie | high |
| F13: Drift, node lifecycle i decommission | ISC-21 (drift), node lifecycle | F12 | nie | medium |
| F14: Drugi niezależny klaster i runbooki | ISC-58, ISC-59, ISC-60, ISC-61, ISC-62 | F12 | nie | high |

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

## Verification

- ISC-3: F1 research — versions/discovered-versions.json + versions/candidate.lock.yml wypełnione z oficjalnych źródeł (mariadb.org, proxysql.com, rockylinux.org, ansible.com); commit 2026-07-22. Host-dependent RPM release potwierdzenie w F0.
- ISC-43: probe-no-secrets-leak.sh PASS lokalnie 2026-07-22 — brak sekretów w repo, logach, argv. Probe gotowy do uruchomienia na CI.
- ISC-58: validate-cluster-schema.py PASS lokalnie 2026-07-22 — cluster.yml zgodny z schema + semantic checks (production locked, max_writers=1, R/W split off). Probe gotowy do CI.
- ISC-62: 7 runbook stubs utworzone (bootstrap, total-outage, node-replacement, backup, restore, upgrade, decommission) w docs/runbooks/; do uzupełnienia w F4/F9/F10/F12/F13/F14.
- ISC-63: probe-no-state-latest.sh PASS lokalnie 2026-07-22 — brak 'state: latest' w rolach i playbookach. Probe gotowy do CI.
- ISC-66: PASS — F0 discovery uruchomiony na 5/5 kontenerów Rocky 9.8 (lab-cluster); 29 tasków PASS każdy (PLAY RECAP ok=29 failed=0); raporty /var/tmp/f0-discovery-*.json zawierają OS/kernel, CPU/RAM/NUMA, dyski/fs/mount, DNS/routing/ports, SELinux/firewalld, repo/pakiety, istniejące MariaDB/ProxySQL (brak), monitoring (brak). F0 nie instalował fio (allow_bench=true tylko gnode1, ale fio nie było zainstalowane — lab ograniczenie). Commit 2026-07-22.
- ISC-67: PASS — F0 discovery read-only; changed=0 na wszystkich hostach (poza zapisem raportu changed=1); brak modyfikacji usług; ansible-playbook 2026-07-22.


## Blockers

- ~~BLK-1~~ ROZBLOKOWANY 2026-07-22 — 5 kontenerów Rocky Linux 9.8 (OrbStack/Docker): 3 Galera + 2 ProxySQL; SSH + sudo NOPASSWD; tests/lab/docker-compose.yml.
- ~~BLK-2~~ ROZBLOKOWANY 2026-07-22 — inventory lab-cluster (clusters/lab-cluster/inventory.yml) z SSH key (tests/lab/ssh_key); Ansible połączenie PASS na 5/5 hostów.
- ~~BLK-3~~ ROZSTRZYGNIĘTY 2026-07-22 — secret backend = Ansible Vault; backup = SMB teraz, S3 retencja 30d później (Decisions).
- ~~BLK-4~~ ROZSTRZYGNIĘTY 2026-07-22 — internet dostępny; F1 research wykonany z oficjalnych źródeł.

## Następny pojedynczy feature

F2: Preflight, repo, pakiety, time sync, SELinux, firewalld — F0 zakończone (ISC-66/67 PASS, 2/68). BLK-1/BLK-2 rozblokowane (OrbStack lab). F1 research wykonany. Lab ograniczenia: SELinux Disabled (kontener), firewalld DBUS (kontener) — ISC-4/5 pozostają otwarte w lab, zatwierdzane na produkcji (vmware_esxi). F2 instaluje repo MariaDB 11.4 + ProxySQL 3.0.9, pakiety z lockfile, konfiguruje time sync. Po F2: F3 (MariaDB/Galera config).
