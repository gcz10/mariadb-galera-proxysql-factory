---
task: "Zbuduj fabrykę klastrów Galera i ProxySQL"
slug: "20260722-172704_galera-proxysql-cluster-factory"
effort: comprehensive
effort_source: explicit
phase: build
progress: 68/68
# 64 kryteria w pelni spelnione na biezacym dowodzie, 4 z zastrzezeniem (`[~]`):
# ISC-1 (dowod historyczny z odbudowy 2026-08-02, powtorzenie wymaga teardownu),
# ISC-22 (izolacja admin/app CIDR dowiedziona tylko w produkcji), ISC-44
# (nieważny cert nieprzetestowany), ISC-66 (fio nigdy nie uruchomione w F0).
# Zastrzezenia sa rozpisane w Verification przy kazdym ISC.
mode: iterate
started: "2026-07-22T15:27:04Z"
updated: "2026-08-14T23:10:00Z"
principal_stated_goal: "Zbuduj powtarzalną, idempotentną i operacyjnie bezpieczną fabrykę produkcyjnych klastrów MariaDB Galera z ProxySQL na istniejących maszynach Rocky Linux 9, tak aby nowy niezależny klaster powstawał przez dodanie inventory i konfiguracji klastra, a każdy stan wysokiej dostępności, bezpieczeństwa, backupu i odtwarzania był potwierdzony wykonywalnym testem oraz dowodem."
principal_stated_goal_source: prompt
principal_stated_goal_signal: 4
principal_stated_goal_locked: "2026-07-22T15:27:04Z"
---

## Problem

Potrzeba powtarzalnej, idempotentnej fabryki produkcyjnych klastrów MariaDB Galera z ProxySQL na istniejących hostach Rocky Linux 9. Dziś brak zautomatyzowanej, dowodzonej ścieżki: nowy klaster powinien powstawać wyłącznie przez dodanie `clusters/<name>/` (inventory + konfiguracja), a każdy stan HA, bezpieczeństwa, backupu i odtwarzania musi być potwierdzony wykonywalnym probe'em i dowodem — a nie jednorazowym skryptem Bash ani monolitycznym playbookiem. VM tworzy Terraform w Proxmox VE przed uruchomieniem Ansible (`terraform/`, cel `infra-provision`); projekt nie zarządza VMware ESXi/vCenter, nie przenosi VM oraz nie zarządza siecią fizyczną ani storagem hypervisora.

## Vision

Repozytorium Ansible, w którym nowy niezależny klaster Galera+ProxySQL powstaje przez dodanie katalogu `clusters/<name>/` z `inventory.yml` i `cluster.yml`. Kod ról i playbooków nie zawiera danych konkretnego klastra. Każdy krytyczny stan jest falsyfikowalny sondą: deployment, idempotencja, replikacja Galery, ProxySQL routing, endpoint HA, failover bez utraty transakcji, szyfrowany off-cluster backup z restore drill, hardening, monitoring z alertami, rolling operations i upgrade planning, drift detection, drugi niezależny klaster z tego samego kodu. Wszystkie wersje przypięte lockfile; produkcja używa wyłącznie `versions.policy: locked`.

## Out of Scope

Tworzenie VM jest w zakresie: maszyny klastrów i warstwy współdzielonej powstają przez Terraform na Proxmox VE (`terraform/`, cel `infra-provision`, provider `bpg/proxmox`). Poza zakresem pozostaje:

- zarządzanie ESXi lub vCenter,
- przenoszenie VM (przebudowa klastra to destroy + provision od zera, nie migracja),
- automatyzacja anti-affinity VM (rekomendacja operacyjna bez walidacji; flota działa na pojedynczym węźle PVE — świadomie przyjęte ryzyko),
- fizyczna sieć, storage i konfiguracja samego węzła PVE (istniejące `vmbr0`, `local-zfs`, zaimportowane obrazy cloud-init i pool poprzedzają `infra-provision`),
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
- Zachowanie cudzego oprogramowania ustalaj z jego dokumentacji lub kodu źródłowego, nie z pamięci ani z rozumowania przez analogię. Twierdzenie o API, formacie pliku czy semantyce parametru bez cytatu ze źródła jest hipotezą, nie faktem — i jako hipotezę należy je oznaczyć.
- Test musi wykonywać dokładnie to, co robi kod produkcyjny. Ręczne wywołanie uproszczone względem produkcyjnego (inne argumenty, zaszyta wartość zamiast szablonu) nie jest dowodem poprawności.
- Ścieżka, której nigdy nie uruchomiono, jest niesprawna do czasu przećwiczenia. Dotyczy to zwłaszcza gałęzi warunkowych odpalanych rzadko: rotacji sekretów, odtwarzania po awarii, migracji.
- Konfiguracja usług generowana z repo i porównywalna z runtime.

## Constraints

- Hosty: VM Rocky Linux 9/10 na Proxmox VE tworzone Terraformem (`make infra-provision`) i konfigurowane przez Ansible; ESXi/vCenter oraz przenoszenie VM poza zakresem.
- Wersje przypięte `versions.lock.yml`; produkcja wyłącznie `versions.policy: locked`; nigdy `state: latest`; brak dynamicznej zmiany major series; deployment zatrzymuje się, gdy pakiet z lockfile niedostępny.
- Galera: 3 pełne węzły, `max_writers: 1`, read/write split wyłączony, SST przez `mariadb-backup`, nieparzysta liczba głosów i ochrona quorum.
- ProxySQL: 2 węzły, natywny `mysql_galera_hostgroups`, admin port ograniczony do administration CIDR.
- Endpoint: Keepalived VIP na węzłach ProxySQL (decyzja principal).
- TLS: tryb `disabled` w v1 z udokumentowanym risk acceptance; `full` zaplanowane w późniejszym feature, pozostawia zależne ISC otwarte.
- Sekrety: backend dobrany do istniejącego standardu firmy (F0 discovery); brak sekretów w repo, logach, diffach, argv.
- Backup: szyfrowany, checksumowany i izolowany per klaster; backend wybierany jawnie spośród S3, zarządzanego SMB albo wcześniej zamontowanego filesystemu; retencja i scheduler są częścią `cluster.yml`.
- High-blast kryteria (sekrety, dane, produkcja, recovery, upgrade) wymagają deterministycznego probe'a; `manual` niewystarcza.
- Hierarchia dowodów: pomiar na docelowym systemie > oficjalna dokumentacja przypiętej wersji > release notes/errata > wiedza modelu jako hipoteza.

## Goal

Zbudować fabrykę klastrów spełniającą wszystkie kryteria ISC poniżej, w kolejności feature'ów F0–F14, zamykając każde kryterium wyłącznie na dowodzie. Po zakmnięciu zakresu v1: drugi niezależny klaster powstaje z tego samego kodu wyłącznie przez nowy `clusters/<name>/`, zwykły converge drugiego klastra jest idempotentny, runbook total outage sprawdzony na środowisku testowym, repo bez sekretów, ISA aktualnym systemem zapisu projektu.

## Criteria
Legenda stanów: `[x]` — kryterium w pełni spełnione na aktualnym dowodzie; `[ ]` — otwarte; `[~]` — PASS z zastrzeżeniem: kontrakt dotrzymany, ale dowód obejmuje tylko część kryterium lub część środowisk albo jest historyczny i nieodtwarzalny bez destrukcji — dokładne zastrzeżenie w Verification przy danym ISC.

### Instalacja i idempotencja
- [~] ISC-1: Deployment na czystych hostach Rocky Linux 9 kończy się sukcesem (site.yml exit 0, wszystkie taski PASS).
- [x] ISC-2: Drugi uruchomiony converge na niezmiennym klastrze raportuje `changed=0` na wszystkich hostach.
- [x] ISC-3: Wersje MariaDB, mariadb-backup, Galera provider, ProxySQL i kolekcji Ansible są dokładnie zgodne z `versions.lock.yml`.
- [x] ISC-4: SELinux pozostaje w trybie Enforcing po pełnym deploy.
- [x] ISC-5: Firewalld działa i dopuszcza wyłącznie zadeklarowany ruch (Galera, ProxySQL, admin, monitoring) na wszystkich hostach.
- [x] ISC-6: Anti: Nieudany preflight nie zostawia częściowych zmian — konfiguracja hostów pozostaje niezmieniona, gdy preflight FAIL.

### Galera
- [x] ISC-7: W klastrze istnieje dokładnie jeden Primary Component.
- [x] ISC-8: `wsrep_cluster_size` równa się `galera.nodes_expected` na każdym węźle.
- [x] ISC-9: `wsrep_cluster_state_uuid` jest identyczny na wszystkich węzłach.
- [x] ISC-10: Każdy węzeł raportuje `wsrep_connected=ON`, `wsrep_ready=ON`, `wsrep_local_state=4 (Synced)`.
- [x] ISC-11: Zapis wykonany przez publiczny endpoint ProxySQL jest widoczny na pozostałych węzłach Galery (replikacja sync).
- [x] ISC-12: Initial bootstrap wykonuje się tylko na jednym jawnie wybranym węźle i wymaga jawnego potwierdzenia.
- [x] ISC-13: Anti: Zwykły `site.yml`/`converge.yml` nigdy nie wykonuje initial bootstrap.
- [x] ISC-14: SST nowego węzła używa metody `mariadb-backup`.
- [x] ISC-15: Powracający węzeł używa IST, gdy mieści się w zmierzonym oknie gcache.
- [x] ISC-16: Brak klucza głównego na jakiejkolwiek tabeli użytkownika jest blockerem deploy.
- [x] ISC-17: Utrata większości węzłów blokuje zapisy (cluster w stanie non-Primary, `wsrep_ready=OFF`).

### ProxySQL
- [x] ISC-18: W runtime hostgroup istnieje dokładnie jeden aktywny writer.
- [x] ISC-19: Węzeł non-Primary, non-Synced, not Ready lub przekraczający zatwierdzony lag jest wyłączony z ruchu ProxySQL.
- [x] ISC-20: Monitorowanie Galery w ProxySQL osiąga poprawny stan w określonym progu czasu po deploy.
- [x] ISC-21: Konfiguracja runtime i disk ProxySQL jest zgodna z repo (brak driftu po converge).
- [~] ISC-22: Admin port ProxySQL (6032) nie jest osiągalny z application CIDR.
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
- [x] ISC-32: Backup opuszcza hosty Galery do skonfigurowanego S3, SMB albo wcześniej zamontowanego filesystemu.
- [x] ISC-33: Backup jest zaszyfrowany.
- [x] ISC-34: Checksum backupu jest poprawna i weryfikowalna.
- [x] ISC-35: Metadata backupu zawiera wersję MariaDB, czas, cluster name i pozycję wsrep/seqno.
- [x] ISC-36: Restore na czysty izolowany host przechodzi test integralności (checksum + zapytanie).
- [x] ISC-37: Restore drill jest uruchamiany jawnie i weryfikowany względem `restore_test_schedule`; pole jest SLA świeżości, nie automatycznym cronem restore.
- [x] ISC-38: Nieudany lub przeterminowany backup generuje fail-closed stan, metrykę i zarządzany alert.
- [x] ISC-39: Backup nie degraduje aktywnego writera ponad uzgodniony threshold (queue/flow control).

### Bezpieczeństwo
- [x] ISC-40: Brak anonimowych kont, testowej bazy i pustych haseł.
- [x] ISC-41: Root nie loguje się zdalnie (tylko localhost/UNIX socket).
- [x] ISC-42: Konta SST, monitor i app mają minimalne uprawnienia (least privilege).
- [x] ISC-43: Anti: Sekrety nie występują w repo, logach CI, diffach ani argv procesu.
- [~] ISC-44: W trybie `tls.mode=full` połączenie z niezaufanym lub nieważnym certyfikatem jest odrzucane.
- [x] ISC-45: W trybie `tls.mode=disabled` w profilu production powstaje jawne ostrzeżenie i udokumentowane risk acceptance.

### Obserwowalność
- [x] ISC-46: Metryki Galery, MariaDB i ProxySQL trafiają do istniejącego systemu monitoringowego.
- [x] ISC-47: Alert powstaje po utracie quorum, utracie writera lub utracie węzła.
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
- [~] ISC-66: Raport discovery zawiera fakty: OS/kernel, CPU/RAM/NUMA, dyski/filesystem/mount/wolne miejsce, IOPS+fsync (fio), DNS/routing/osigalność portów, chrony/NTP, SELinux/firewalld, repozytoria+pakiety, istniejące MariaDB/ProxySQL, monitoring, secret backend, audyt PK, write rate.
- [x] ISC-67: Anti: F0 discovery nie modyfikuje stanu usług produkcyjnych (read-only względem usług).
- [x] ISC-68: `gcache.size` jest wyliczony z mierzonego write rate i wymaganego okna IST i zapisany w raporcie/Decisions.

### Obowiązkowe Anti
- [x] ISC-63: Anti: Żaden task produkcyjny nie używa `state: latest`.
- [x] ISC-64: Anti: Dekonstrukcyjne testy (chaos, failover, restore drill) nie uruchamiają się na profilu production.
- [x] ISC-65: Anti: Dwa węzły nigdy nie są bootstrapowane jako niezależne Primary Components.

## Not yet specified

- ~~fog: retencja backupów~~ ROZSTRZYGNIĘTY 2026-07-29 — `retention_days` jest ustawiane per klaster i stosowane identycznie przez S3, SMB i filesystem; nie oznacza automatycznie immutability/off-site.
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
| ISC-44 | derived: tls-full | bash | połączenie z niezaufanym cert (wrong CA) | odrzucone (ERROR 2026) | probe-app-conformance.py |
| ISC-45 | derived: tls-disabled-warning | bash | profil production + tls.disabled | ostrzeżenie + risk acceptance w Decisions | ansible report + grep |
| ISC-46 | literal | python | PMM Prom: `proxysql_*` + `mysql_up` + Galera + node_exporter series | 2 ProxySQL + 3 MySQL + 5 node series scraped | probe-pmm-native.py |
| ISC-47 | literal | bash | utrata quorum/writera/node → alert | alert dostarczony do celu | monitoring |
| ISC-48 | literal | bash | `logrotate -d` + sprawdzenie rotacji | rotuje się | logrotate + ansible |
| ISC-49 | literal | python | PMM Prom: backup/restore unixtime non-zero + age window; cert expiry | non-zero unixtime w oknie retencji | probe-pmm-native.py |
| ISC-50 | literal | python | f12_rolling_restart.yml play Galera serial:1 | serial:1 | probe-rolling-restart.py |
| ISC-51 | literal | python | brama zdrowia (wsrep_local_state=4+Primary+size) + runtime | Synced przed kolejnym | probe-rolling-restart.py |
| ISC-52 | literal | python | f12_patch.yml canary (non-writer pierwszy) | canary + health gate | probe-patch.py |
| ISC-53 | literal | python | f12_upgrade_plan.yml host tasks changed=0 | brak zmian na hostach | probe-upgrade-plan.py |
| ISC-54 | derived: official-upgrade-path | python | plan docs vs oficjalna docs MariaDB/Galera | ścieżka 11.4→12.3 LTS + skip-write-binlog + brak regresji EOL | probe-upgrade-plan.py |
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
- 2026-07-22 — historyczna odpowiedź Interview: backup na zamontowany zasób SMB teraz, opcja S3 później — zastąpiona uniwersalnym kontraktem backendów 2026-07-29.
- 2026-07-22 — Interview odpowiedź: endpoint = Keepalived VIP na węzłach ProxySQL — ponieważ principal wybór; wymaga osobnych CIDR i rekomendacji anti-affinity — źródło: Interview 2026-07-22.
- 2026-07-22 — Interview odpowiedź: TLS `disabled` teraz, `full` zaplanowane w późniejszym feature — ponieważ principal wybór; ZAŁOŻENIE DO POTWIERDZENIA risk acceptance; ISC-44 i TLS ISC pozostają otwarte — źródło: Interview 2026-07-22.
- 2026-07-22 — refined: BLK-3 rozstrzygnięty — secret backend = Ansible Vault — ponieważ principal wybór; szyfrowane pliki w repo (clusters/<name>/secrets.yml), klucz poza repo; ISC-43 zależy, F6 implementacja — źródło: Interview 2026-07-22.
- 2026-07-22 — historyczne rozstrzygnięcie BLK-3: SMB teraz, migracja S3 z retencją 30d — zastąpione implementacją S3/SMB/filesystem i retencją per klaster 2026-07-29.
- 2026-07-22 — F1 research: MariaDB 11.4.12 LTS wybrana — ponieważ najdłuższe wsparcie (EOL 2029-05), Galera 4, RPM dla RHEL9 — dowód: mariadb.org, endoflife.date (2026-07-22).
- 2026-07-22 — F1 research: ProxySQL 3.0.9 wybrany — ponieważ Stable Tier, łata CVE-2026-48772/48773 — dowód: proxysql.com (2026-07-22).
- 2026-07-22 — F1 research: Galera 4 (galera-4, wsrep API 26) — ponieważ jedyny wspierany provider dla MariaDB 11.x; wbudowany w pakiety MariaDB — dowód: mariadb.org (2026-07-22).
- 2026-07-22 — F1 research: Rocky Linux 9.8 (latest minor, 2026-05-27), major EOL 2032-05-31 — dowód: rockylinux.org, endoflife.date (2026-07-22).
- 2026-07-22 — F1 research: ansible-core 2.21.2 + ansible.mysql 5.1.0 — ponieważ community.mysql deprecated -> ansible.mysql — dowód: ansible.com, github.com/ansible-collections/ansible.mysql (2026-07-22).
- 2026-07-22 — F1 research: odrzucone jako bieżący baseline MariaDB 12.3 (mniejsza dojrzałość), 11.8 (krótszy EOL), 10.11 (starsza), 10.5/10.6 (EOL/przestarzałe) — dowód: mariadb.org (2026-07-22).
- 2026-07-22 — ADR-001: Keepalived VIP endpoint — ponieważ principal wybór; VRRP <3s spełnia RTO <2min — docs/adr/ADR-001-keepalived-vip-endpoint.md.
- 2026-07-22 — ADR-002: TLS disabled w v1 + risk acceptance — ponieważ principal wybór; ISC-44 otwarte, ISC-45 aktywne — docs/adr/ADR-002-tls-disabled-risk-acceptance.md.
- 2026-07-22 — ADR-003 (historyczny): backup SMB teraz -> S3 retencja 30d; bieżący kontrakt wielobackendowy i jego ograniczenia są w `docs/runbooks/backup.md`.
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
- 2026-07-22 — historyczny F10 transport: lab używał wyłącznie S3, ponieważ kernel OrbStack nie udostępniał CIFS; zastąpione rzeczywistym dowodem SMB na Rocky Linux 10 z kernelem CIFS 2026-07-29.
- 2026-07-22 — historyczny F10 runner wybierał `galera[1]` i używał `BACKUP_ENCRYPTION_KEY`; zastąpione schedulerem per klaster, `GALERA_BACKUP_*`, jednym runnerem backup/restore i trzema backendami 2026-07-29.
- 2026-07-29 — F10 uniwersalny backup: jeden runner obsługuje S3, zarządzany SMB i wcześniej zamontowany filesystem; konfiguracja i artefakty są izolowane per klaster, owner marker blokuje przejęcie cudzego storage, a lock obejmuje backup i restore. Zarządzany MinIO tworzy scoped konto usługowe i rozprowadza tę samą parę na scheduler/restore bez sekretów w argv. Dowód: unit/static suite oraz live Rocky 10 S3, SMB/CIFS i pre-mounted filesystem backup/restore.
- 2026-07-29 — F10 fail-closed: backend preflight poprzedza `mariadb-backup`; błędne S3 credentials zapisują `E_STORAGE_AUTH` bez metadata i stagingu, SMB nie zapisuje sukcesu przed poprawnym unmount, pre-mounted filesystem blokuje zmianę tożsamości mountu. Repozytoryjny MinIO jest off-cluster, ale bez niezależnej ochrony storage nie jest immutable ani off-site.
- 2026-07-23 — F11 ProxySQL metrics: `admin-restapi_enabled=true` (LOAD+SAVE = trwałe) wystawia `proxysql_*` na `:6070/metrics`; `f11_proxysql_metrics.yml` rejestruje 2 external services (group=proxysql) + external_exporter agents (port 6070) w PMM, reużywając generic nodes z f11_pmm_client. Galera/MariaDB już przez mysqld_exporter+QAN — dowód: PMM Prom `proxysql_servers_table_version_total` 2 series `up=1`, ISC-46 PASS.
- 2026-07-29 — F11 freshness: scheduler publikuje atomowo pięć metryk `galera_backup_*` z per-klastrowego `state.json`; porażka zachowuje timestamp ostatniego sukcesu i ustawia `last_run_success=0`. F15 ma osobne reguły „Backup run failed” i „Backup freshness stale”, obie fail-closed przy braku danych.
- 2026-07-23 — F12 research upgrade (zaktualizowane 2026-07-25): cel 11.4 LTS → 12.3 LTS, ponieważ 11.8 ma wcześniejszy EOL niż 11.4; strażnik odrzuca regresję wsparcia. Metoda in-place `mariadb-upgrade --skip-write-binlog` (bez dump/restore), Galera 4 (wsrep API 26) wspiera rolling; downgrade datadir NIEWSPARTY ("forward-incompatible") — źródła: mariadb.com/kb/en/upgrading-galera-cluster, mariadb.com/kb/en/downgrading-between-major-mariadb-versions, galeracluster.com/library/documentation/upgrading.html.
- 2026-07-23 — F12 rolling restart order: non-writer węzły pierwsze, writer ostatni (research galeracluster.com) — minimalizuje churn failoveru; ProxySQL mysql_galera_hostgroups auto-promuje backup-writera. Lab writer=gnode3 (już ostatni w inventory).
- 2026-07-23 — F12 patch safe-default: domyślna komenda patcha = read-only `dnf check-update` (changed_when:false) — wzorzec canary+health-gate wykonywany bez modyfikacji pakietów w labie; produkcja nadpisuje `f12_patch_command`. ProxySQL: `SAVE ... TO DISK` przed patch (proxysql.com configuration-system).
- 2026-07-23 — F13 drift approach: ProxySQL drift = MAIN (mysql_servers/mysql_galera_hostgroups/mysql_users) vs DISK, NIE runtime_* (runtime niesie dynamiczny status SHUNNED/ONLINE + rozwinięte galera hostgroups) — dowód: false-positive przy runtime_mysql_servers (HG20 derived, status dynamic), poprawione na main-vs-disk. Drift read-only (§18: nie naprawia automatycznie) — dowód: inject unsaved config → DRIFT detected, cleanup → CLEAN.
- 2026-07-23 — F13 node lifecycle: remove-node wymaga planu (f13_remove_node_plan.yml read-only: quorum guard, writer-detection) + confirm=yes (f13_remove_node.yml, jak bootstrap). Quorum guard odmawia jeśli size-1 < 2. Lab: 3→2 bezpieczne, nie testowano destruktywnego usunięcia (plan + guard zweryfikowane).
- 2026-07-23 — F14 portability: usunięto hardcoding z 4 playbooków (site.yml, f3_galera_config.yml, f5_join.yml: galera_cluster_name→galera.cluster_name, galera_nodes_csv→groups['galera']|galera_node_address; f7_proxysql.yml: galera_backends→inventory, 'lab-galera'→monitoring.pmm.cluster_name). Weryfikacja: f3 renderuje server.cnf idempotentnie (changed=False), klaster zdrowy. ISC-59 PASS (probe-zero-hardcode: 0 trafień w roles/playbooks).
- 2026-07-23 — F14 ISC-60/61 scope: drugi klaster LIVE (docker-compose druga sieć 172.29.0.x + 5 kontenerów + bootstrap + converge + probes) pozostaje jako acceptance gate — fundament (zero hardcode + parametryzacja + example-cluster template + runbooki) jest kompletny; drugi klaster powstaje wyłącznie przez clusters/<name>/.
- 2026-07-23 — F14 drugi klaster LIVE: clusters/lab2-cluster/ (osobna sieć 172.29.0.x, osobny VIP/MinIO/namespace) wdrożony zero-zmian-w-rolach; bootstrap + SST + ProxySQL + Keepalived. Poprawki portability wykryte przy wdrożeniu: (1) f2_install musi instalować klienta mariadb na węzłach ProxySQL (admin port 6032), (2) ProxySQL w labie wymaga czystego startu --initial z proxysql.db writable (ownership proxysql:proxysql). Dowód: probe-galera/probe-proxysql PASS na lab2, lab1 nienaruszony, izolowane UUID/sieci/VIP.
- 2026-07-24 — ISC-68 gcache: write rate zmierzony = ~55000 B/s (workload RPAD('x',1024) na writerze, delta wsrep_replicated_bytes); gcache.size = 55000 × 30min × 60 = ~96MB → floored 128M (minimum bezpieczne). Wdrożone gcache.size=128M pokrywa wymóg (IST dla 30min okna). Mechanizm: measure (probe-gcache.py) → compute (calc-gcache.py) → verify (deployed ≥ computed). Produkcja: real workload z F0.
- 2026-07-24 — from-zero rebuild (ISC-1/61): lab1 skasowany (`docker compose rm -sf` + `docker volume rm`) i odtworzony od zera wyłącznie przez `make` (lab-up → cluster-deploy → bootstrap → join → proxysql → endpoint → monitoring → harden → alerts → backup → restore-drill). Cała suita probe'ów PASS na świeżym klastrze. Bugi wykryte i naprawione przez test from-zero: (1) Makefile `cluster-deploy`+`cluster-bootstrap` nie przekazywały `-e @clusters/$(CLUSTER)/cluster.yml` → `galera` undefined w site.yml; (2) f2_install chrony systemd task bez guardu → dodano `when: ansible_service_mgr == 'systemd'` (no-systemd containers); (3) chaos-failover.py + backup-impact.py `CREATE TABLE isa_test.*` bez `CREATE DATABASE IF NOT EXISTS isa_test` → fail na świeżym klastrze; (4) dodano helper `make lab-start-services` (restart ProxySQL bez systemd, idempotentny pgrep-guarded). Caveaty: cluster-deploy na świeżym klastrze czeka ~6min na mariadbd przed bootstrap (converge/bootstrap coupling, ignore_errors — funkcjonalne); restore-drill wymaga zaseedowania canary isa_test (replication_probe+isa_failover); f6 harden musi biec PO f11 monitoring (zależność pmm_monitor, kolejność w README).
- 2026-07-24 — lab2 gnode3b recovery (no-systemd node loss): po wcześniejszym chaos-failover na lab2 gnode3b został DOWN (brak systemd → brak auto-restartu). Full SST rejoin ujawnił root cause: `wsrep_sst_auth = "sst_user:"` (puste hasło — lab2 wdrożony zanim SST_PASSWORD trafił do .env; wcześniejsze rejoiny używały IST/gcache więc maskowały problem). Fix: `SET GLOBAL wsrep_sst_auth` (runtime, dynamiczny) + lineinfile persist na server.cnf + wipe/fresh SST gnode3b → Synced, size=3. Wniosek: przy zmianie hasła sst_user regeneruj konfig (site.yml) — DB i wsrep_sst_auth muszą być spójne dla full SST.
- 2026-07-24 — audyt produkcyjny + systemd MariaDB: przegląd całego kodu pod kątem produkcji. Główny blocker: MariaDB startowana surowym `mariadbd &` (site/bootstrap/join), bez systemd — w przeciwieństwie do ProxySQL/Keepalived/exporterów które mają gałęzie systemd. Skutek: brak boot-persistence, brak auto-restartu po crashu (= awaria lab2 gnode3b). NAPRAWIONE: dodano gałęzie systemd guardowane `when: ansible_service_mgr == 'systemd'` (var `use_systemd`) w site.yml (converge: enable + restart-handler na zmianę configu, destrukcyjny kill/aria-delete tylko lab), bootstrap.yml (`galera_new_cluster` w produkcji), f5_join.yml (`systemctl start` → SST/IST), f2_install.yml (enable mariadb.service). Lab (`ansible_service_mgr=sshd`) używa ścieżki raw bez zmian — REGRESJA ZWERYFIKOWANA: re-converge lab1 → 3/3 Synced, probe galera/proxysql/endpoint/hardening PASS. Ścieżka systemd wymaga walidacji na stagingowej VM Rocky 9 (kontenerowy lab nie ma systemd/PID1 — nie da się jej tu uruchomić). Inne poprawki: server.cnf.j2 tuning sparametryzowany (mariadb_tuning; 256M/50conn to wartości LAB), f2_install ProxySQL RPM arch-auto + when-guard + gpg konfigurowalny. Otwarte (raport): TLS full niezaimplementowane (ADR-002 disabled), S3_SECURE default false, flush_log_at_trx_commit=0 vs RPO=0 (konfigurowalne), gcache.page_size=size.
- 2026-07-24 — TLS opcjonalne (ISC-44) ZAIMPLEMENTOWANE + zweryfikowane na lab2. `tls.mode=full` per-klaster wpina: (1) MariaDB server TLS (ssl_ca/cert/key), (2) szyfrowanie replikacji Galera (`wsrep_provider_options socket.ssl_*`), (3) SST przez TLS (`[sst] tca/tcert/tkey`), (4) ProxySQL→backend TLS (`mysql_servers.use_ssl=1` + `mysql-ssl_p2s_ca`). Certy rozprowadzane przez `playbooks/tls_certs.yml` (include w site/bootstrap/join/f7; galera=owner mysql+klucz, ProxySQL=owner root, tylko CA). `tls.require_secure_transport` (default false) wymusza TLS na wszystkich poł. TCP. server.cnf.j2 warunkowy; disabled=brak dyrektyw (lab1 render bez zmian funkcjonalnych). Fix przy okazji: `gcache.page_size` odseparowany od gcache.size (default 128M, `mariadb_tuning.gcache_page_size`). DOWÓD (lab2 tls.mode=full, lab1 disabled — per-klaster): have_ssl=YES; klient cipher `TLS_AES_256_GCM_SHA384` (app_user/TCP); replikacja `ssl://172.29.0.12:4567` stable; SST przez TLS (gnode2b/3b join OK); ProxySQL use_ssl=1 na 3 backendach; app write przez VIP→ProxySQL→TLS backend OK; probe galera/proxysql/endpoint/hardening PASS; backup OK. Enable TLS na działającym klastrze wymaga skoordynowanego restartu (replikacja TLS↔non-TLS niekompatybilna) + `safe_to_bootstrap=1` — jak total-outage. Certy lab: `tests/lab/tls/` (gitignored). S3_SECURE: już poprawnie sterowane `backup.s3.secure` (lab false/prod true — nie bug). flush_log_at_trx_commit: sparametryzowane (prod krytyczna=1).
- 2026-08-01 — galera-backup runner: regresja z commita "harden Galera backup readiness" powodowała, że KAŻDY backup padał na `E_SECRET_IN_ARGV` przed utworzeniem procesu — `secret_values = set(secrets.values())` wciągało nazwę użytkownika ProxySQL do zbioru bramkującego argv, a strażnik odrzucał własne `-u admin` writer guarda. Rozdzielono zbiory: `SENSITIVE_SECRET_KEYS` (4 poświadczenia) bramkuje argv, `REDACT_ONLY_SECRET_KEYS` (klucz dostępu S3, login SMB) tylko maskuje wyjście, nazwa użytkownika w żadnym (jest podciągiem `mariadb-admin`, `admin_host`, `admin-check.cnf`). Przy okazji: aborty przed lockiem zapisują state/event/metrykę, `tar` nie zakleszcza się na nieczytanym stderr, prune nie degraduje opublikowanego backupu, `fetch_latest` nie cofa się po cichu do starszego artefaktu, mount SMB idzie przez CommandRunner — dowód: commit 8aa40f8, backup 47 MiB i drill PASS na żywym klastrze.
- 2026-08-01 — ProxySQL nie wpuszcza użytkownika `admin` spoza loopbacku ("ERROR 1040: User 'admin' can only connect locally"), a writer guard runnera łączy się z węzła Galera po sieci — guard był więc niewykonalny na każdym klastrze. `f7_proxysql.yml` provisionuje drugą tożsamość w `admin-admin_credentials` (`proxysql_remote_admin_user: isa_admin` w `playbooks/vars/proxysql_hostgroups.yml`, jedno źródło prawdy dla zapisującego i czytających), lokalne `admin` zostaje nietknięte — dowód: zdalne zapytanie z gnode jako `isa_admin` zwraca writera, jako `admin` daje 1040.
- 2026-08-01 — drill restore zakleszczał się na ~50 min: `mariadb-admin shutdown` czeka, aż PID serwera zniknie z tablicy procesów, a PID zostaje `<defunct>`, bo jedyny proces mogący go pochować blokuje się na `mariadb-admin`. Sam timeout tylko przyspieszyłby porażkę i emitował fałszywe zdarzenie na każdym udanym drillu, więc teardown wysyła SIGTERM (mariadbd traktuje go jak czysty shutdown) i sam zbiera dziecko; logika wydzielona do `stop_standalone_server()` — dowód: 4 testy regresji + drill kończy się w 2 s zamiast wisieć.
- 2026-08-01 — testy były antyskorelowane z produkcją: fixtures workflow pomijały blok `proxysql`, którego `config.json.j2` zawsze emituje, i wycinały writer guarda `patch`em, więc 100 testów przechodziło na kodzie, w którym backup nie mógł ruszyć. Fixtures modelują teraz realny kształt configu, happy-path wykonuje prawdziwy strażnik argv przez `CommandRunner._exec`, doszły regresje na allow-listę sekretów, obcego writera i widoczność abortów przedlockowych — dowód: 109 testów, każdy nowy pada na kodzie sprzed poprawki.
- 2026-08-01 — `make lab-seed-smoke` (`playbooks/lab_seed_smoke.yml`): drill restore słusznie odrzuca odtworzenie bez ani jednej bazy użytkownika, ale świeży klaster laboratoryjny jest pusty i drill padał, dopóki ktoś ręcznie czegoś nie zapisał — to była JEDYNA ręczna czynność w odtwarzaniu klastra. Cel zakłada `isa_test.restore_probe`, jest idempotentny (ISC-2) i odmawia pracy poza `profile: laboratory`. Świadomie NIE wpięty w f10_backup/f10_restore: automatyka wdrożeniowa nie zakłada schematów na klastrze z danymi — dowód: DROP → `changed=3` → ponowny przebieg `changed=0` → backup + drill PASS.
- 2026-08-01 — klaster `r10n` zdekomisjonowany i odtworzony jako `claude-r10c` (VMID 9193-9195, IP .71-.73, węzły `gnode7-9`, bo `gnode1-3` należą do claude-r10, a `gnode4-6` do claude-r10b i nazwy muszą być globalnie unikalne w PMM Inventory). `r10n` łamał konwencję floty `claude-<tag>`. Usunięto jego definicje z repo oraz obiekty PMM i 8 reguł alertowych PRZED postawieniem nowego klastra, bo adresy `.71-.73` są reużyte — inaczej powstałoby to samo skażenie duplikatami, które wcześniej naprawiono dla r10b — dowód: commit d123980, `qm list` + PMM pokazują wyłącznie `r10c`.
- 2026-08-01 — `make infra-provision` żądał `PROXMOX_VE_PASSWORD`, choć provider `bpg/proxmox` uwierzytelnia się tokenem API; nowy `pve_auth_guard` przyjmuje dowolne z dwóch. Ma to znaczenie operacyjne: skodyfikowana ścieżka wymusza `-parallelism=1`, a jej ominięcie (zapisany plan z domyślną równoległością) wywaliło locki ZFS na PVE — `HTTP 596 Broken pipe` i VM utworzona poza stanem Terraform. Przez cel z Makefile trzy VM powstają w 74 s bez błędu — dowód: commit d123980.
- 2026-08-01 — wyłączone węzły Galera (`claude-r10b` gnode4-6, `claude-r9g` g9node1-3) mają w kodzie `started = false` zamiast rozjazdu stan-vs-rzeczywistość; `terraform plan` = 0 zmian dla obu modułów, więc apply nie wskrzesi ich po cichu. Snapshot całej infrastruktury i lista rozbieżności wymagających decyzji: `docs/infrastructure-state.md`.

- 2026-08-02 — QAN na `finalclaude-r9` czerpie ze slow logu z progiem `long_query_time: 0.1`, nie z `0`. Zmierzone na f9g1: przy progu 0 slow log rosnie 276 B/zapytanie (1.07 MB na 4053 zapytania), co ekstrapoluje sie na ~1 GB/h NA WEZEL przy 1000 q/s — i jest to koszt per wezel, nie per klaster, bo ruch monitoringu uderza w kazdy osobno (logi trzech wezlow miescily sie w 1 MB od siebie). Przy progu 0.1 ten sam benchmark pisze ZERO bajtow. SWIADOMY KOSZT: w oknie 6 minut fc9 pokazuje 2 ksztalty zapytan wobec 31 na fc10 (perfschema bez progu) — szybkie, ale czeste zapytania znikaja z QAN calkowicie, a to zwykle one sumuja sie w obciazenie. Operator wybral mniejszy wolumen. ZAKRES: decyzja dotyczy WYLACZNIE fc9 — to jedyny klaster z QAN ze slowloga. fc10 pozostaje agentless z QAN z perfschema, bez slow logu i bez progu, wiec nadal widzi ksztalty szybkich zapytan; nie jest to konfiguracja floty, tylko jednego klastra. Alternatywa, gdyby pokrycie bylo wazniejsze: `log_slow_rate_limit` > 1 probkuje co N-te zapytanie niezaleznie od czasu, wiec QAN zachowuje pelny rozklad ksztaltow ze skalowanymi licznikami; zostawione na 1 — dowod: przebieg benchmarku przed i po zmianie progu, plus SELECT SLEEP(0.25) potwierdzajacy, ze sciezka przechwytywania dziala (283 B w logu, dane w QAN).
- 2026-08-15 — PMM podniesiony 3.8.1 -> 3.9.0 na calej flocie (serwer na `fcinfra` + klienci `n3g1-3`), przypiety po digescie `percona/pmm-server:3.9.0@sha256:ec0391338420a019a45ab33c101a2b62c3e2e0908bf6748056a9793b97c6d094` w OBU lockfile'ach wskazywanych przez klastry — ponieważ PMM-14193/PMM-15145 naprawiaja alerty *Down* wracajace do `Normal`, gdy baza I jej agent padaja jednoczesnie, czyli dokladnie te gwarancje fail-closed, ktorej pilnuje ISC-47; dodatkowo CVE-2026-39822 (HIGH, symlink traversal w stdlib Go) oraz usuniecie preinstalowanego datasource'u PostgreSQL, przez ktory rola Viewer mogla wykonywac dowolny SQL na wewnetrznej bazie PMM. Kolejnosc serwer->klient wymuszona przez dokumentacje Percony i od poczatku zapisana w komentarzu lockfile. Upgrade z UI i Watchtower usuniete w 3.9.0 (PMM-14969) — jedyna wspierana sciezka to podmiana obrazu kontenera, czyli to, co repo robi od poczatku przez `infra_services.yml`. `candidate.lock.yml` i `discovered-versions.json` CELOWO nietkniete: to artefakty badawcze F1 z 2026-07-22, ktorych nikt nie wskazuje (CI je pomija), a nadpisanie zafalszowaloby zapis tamtego researchu — dowod: kopia wolumenu `/var/backups/pmm/pmm-data-3.8.1.tgz` (969 MB) zrobiona przy zatrzymanym kontenerze przed zmiana; po upgradzie 12 wezlow / 15 uslug / 24 reguly alertowe / 3 contact pointy / 45 serii `mysql_up` w 24 h — wartosci identyczne ze stanem sprzed; `docker inspect` potwierdza uruchomienie po digescie i `healthy`, brak Watchtowera w obrazie, lista datasource'ow bez PostgreSQL; `pmm-admin status` = 3.9.0 i `Connected: true` na wszystkich trzech wezlach; QAN zweryfikowany przez wygenerowanie `SELECT SLEEP(0.6)` i odczytanie go z API (`max=0.600s`); 4/4 sondy akceptacyjne `newclaude3-r9`, a `finalclaude-r10` z tymi samymi 2 znaleziskami co przed upgradem (przeterminowany drill z 2026-08-02, nie regresja).
- 2026-08-20 — kontrakt cluster.yml ograniczony do pol z potwierdzonymi konsumentami: usunieto automation_release, virtualization, secrets.backend, monitoring.system, nieuzywane storage/availability, proxysql flags i tls.certificate_source; zachowano availability.rto_node_failure oraz storage.data_directory/expected_database_size_gb — dowod: tests/unit/test_cluster_schema_contract.py i grep repo. Parametry wsrep_slave_threads/wsrep_log_conflicts dodano do schema, bo czyta je server.cnf.j2.
- 2026-08-21 — warstwa wspolna wyniesiona z wlasnosci klastra do samodzielnej jednostki `platform/shared/` (`platform.yml` + `inventory.yml` + `platform/schema/`), zarzadzanej wylacznie celami `make platform-*`; klastry sa najemcami. Pole `proxysql.role: owner|consumer` USUNIETE ze schematu i wszystkich cluster.yml, cel `cluster-endpoint` usuniety, `f8_keepalived.yml` odrzuca definicje klastra asercja fail-closed. Powod: `finalclaude-r10` mial `role: owner`, wiec jego skasowanie osierocilo by ProxySQL, VIP, PMM i MinIO — a `probe-proxysql-tenancy.py` to sprzezenie WYMUSZALA ("zero ownerow oznacza, ze nikt tej warstwy nie instaluje"). Ograniczenie "dokladnie jeden wlasciciel" bylo sluszne; bledem bylo to, ze wlascicielem byl konsument. Dowod live: `platform-proxysql` i `platform-endpoint` `changed=0` na dzialajacej parze (definicja odtwarza stan ownera co do bitu), f8 z definicja najemcy rc=2, bramka n16 13/13 — PR #60.
- 2026-08-21 — odbudowa pary ProxySQL OD ZERA (`terraform destroy` fcp1/fcp2 + `make platform-build`) wykryla trzy defekty niewidoczne na istniejacych maszynach. (1) `check_proxysql.sh` mylil "zero najemcow" z "najemca bez writera": swieza warstwa ma puste `runtime_mysql_galera_hostgroups`, wiec bramka zwracala 1, Keepalived nigdy nie bral VIP-a i platforma NIE MOGLA wstac bez uprzedniego zarejestrowania klastra — zaprzeczenie wlasnej tezy. (2) `monitoring.agent_groups` w platform.yml bylo polem-widmem: natywny pmm-agent na fcp1/fcp2 istnial tylko ubocznie, bo mial to pole owner; po jego usunieciu znikly metryki `proxysql_connection_pool_*`, a regula ISC-47 z `noDataState: Alerting` palila sie na stale, czyli przestala odrozniac sprawny klaster od zepsutego. (3) Rola `pmm` byla przywiazana do bloku `cluster`; dyskryminatorem jest teraz `platform.name`, bo blok `platform` istnieje TAKZE w kazdym cluster.yml, a `dict.get('k', cluster.x)` wywala sie na nieistniejacym `cluster` (Jinja liczy argument domyslny zachlannie). Dowod: `platform-build` przeszedl jednym poleceniem przy zerze najemcow, konfiguracja koncowa vs baseline = zero roznic, `count(proxysql_connection_pool_status)` BRAK -> 20 — PR #61.
- 2026-08-24 — wycofanie topologii kontenerowej z gałęzi main: usunięto dualną ścieżkę not use_systemd z 6 playbooków Galery i 4 pomocniczych oraz definicje klastrów kontenerowych; lab kontenerowy zachowany na dedykowanym branchu `lab/docker-podman`. W main pozostaje wyłącznie topologia VM (Proxmox VE, Rocky Linux 9/10 z systemd) — ponieważ utrzymywanie dualnej ścieżki w operacjach niszczących (rolling restart, recover, remove node) fałszowało dowody bramek dla środowisk produkcyjnych. Usługi warstwy wspólnej na VM fcinfra (PMM, MinIO, Maildev) nadal działają w kontenerach Docker.
- 2026-09-04 — P1-A (audyt): lancuch dostaw percona-release w roles/pmm był jedynym niekontrolowanym pobraniem w repo — mutowalny URL `percona-release-latest.noarch.rpm` i `disable_gpg_check: true`. Wzorzec z platform_install.yml (audit#6): pinned URL `percona-release-1.0-34.noarch.rpm` + sha256 RPM (`48d3a6d6…ae9d8e`) + sha256 klucza Percona (`55909a2d…72a2`) + import klucza po odcisku palca `4D1BB29D63D98E422B2113B19334A25F8507EFA5` (rsa4096 PERCONA-PACKAGING-KEY, zweryfikowany plik klucza + issuer-fingerprint w sygnaturze RSA naglowka sygnatur, tag 268) + instalacja lokalnego pliku Z sprawdzeniem podpisu. Piny w `pmm_client.*` czterech lockfile'ach, walidacja kluczy w validate-lockfile.py, sonda verify-no-state-latest rozszerzona o `disable_gpg_check: true` i URL-e `-latest` (z filtrem komentarzy).
- 2026-09-04 — P1-B: format backupu 3 (`GB3G`) używa przyrostowego `Cipher(AES, GCM)` zamiast one-shot `AESGCM`, więc szyfrowanie i odtwarzanie nie buforują archiwum w RAM. Nagłówek jest AAD, tag 16 B leży w przyczepie, a plaintext jest publikowany przez atomową podmianę dopiero po poprawnym `finalize()`. Czytniki v2 (`GB2G`) i v1 (CBC) pozostają aktywne; storage i restore akceptują jawnie wersje `{1,2,3}`. Dowód lokalny: test roundtrip/tamper/złego klucza, fixture istniejącej kopii v2, legacy v1 oraz 24 MiB regression z limitem peak Python heap poniżej połowy rozmiaru pliku. API: https://github.com/pyca/cryptography/blob/main/docs/hazmat/primitives/symmetric-encryption.rst
- 2026-09-04 — P1-B wdrożony na żywo na `orionv15-r10` (kanarek: jedyny Terraform-owany klaster z niezależnie przypiętą tożsamością hosta restore w stanie TF). Runner `crypto.py` na wszystkich węzłach Galery i hoście restore ma sumę `641f1b0f…4ad` zgodną z repozytorium (przed wdrożeniem: `b95044b1…627`). Pomiar RAM na żywo, nie w teście: `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` całego drzewa runnera wyniósł 57 088 KiB (≈55,8 MiB) przy artefakcie 49 264 688 B (≈47 MiB) — czyli szczyt pamięci nie skaluje się z rozmiarem kopii. Ten sam artefakt odtworzony przez `cluster-restore-drill` na izolowanym `o15r1`; klaster i endpoint po operacji bez zmian.
- 2026-09-04 — ProxySQL 3.0.11 (Stable, 2026-08-27) na warstwie wspólnej `xenonv12`, w miejsce 3.0.10. Powód: wydanie usuwa odczyt poza buforem przy krótkiej odpowiedzi uwierzytelniania (native-password/caching-SHA2), przenosi materiał handshake na `RAND_bytes` OpenSSL i naprawia use-after-free wątków przy zamykaniu z `--idle-threads`. Wdrożone przez `f12_patch.yml` (ISC-57: serial:1, `SAVE ... TO DISK` przed patchem, brama backendów po); pakiet pobrany z `repo.proxysql.com` i zweryfikowany dwukrotnie: sha256 z lockfile oraz `rpm -K` (digests signatures OK) na obu węzłach. Kopia `proxysql.db.pre-3.0.11` została na obu hostach jako punkt powrotu. Nowe funkcje 3.0.11 (`mysql-user_variable_tracking`, `mysql-update_gtid_from_ok`) zostają wyłączone — są domyślnie `0` i nie zmieniamy ich w tym oknie. Źródło: https://github.com/sysown/proxysql/releases/tag/v3.0.11
- 2026-09-04 — P1-B rozszerzony z kanarka na całą żywą flotę: `cassiopeiav14-r9` ma teraz `crypto.py` `641f1b0f…4ad` na trzech węzłach Galery i na `c14r1`, kopia `galera-cassiopeiav14-r9-20260904-224925` jest formatu 3 (`GB3G`, 49 264 688 B), a drill restore na izolowanym `c14r1` zweryfikował 7 wierszy. Oba żywe klastry są na tym samym formacie; nie zostaje węzeł buforujący archiwum w RAM.
- 2026-09-04 — ISC-39 był bramką niefalsyfikowalną i został naprawiony. `tests/lab/backup-impact.py` wywoływał `f10_backup.yml` bez `galera_backup_action=run`, a playbook defaultuje akcję na `configure` i bramkuje realny backup przez `when: galera_backup_action == 'run'`. Okno pomiarowe nie zawierało więc żadnego backupu: flow control i write stall wychodziły zerowe **z konstrukcji pomiaru**, a sonda nie mogła zapalić się na czerwono. Kanoniczna ścieżka operatora (`Makefile`, cel `cluster-backup`) ten przełącznik podaje — wypadł wyłącznie z sondy. Naprawa jest dwuczęściowa, bo sam przełącznik nie chroni przed nawrotem: sonda dodatkowo czyta `last_success` z `state.json` wszystkich węzłów Galery przed i po oknie i wymaga, żeby wartość wzrosła. Bez nowej kopii wynik jest teraz FAIL, a nie ciche PASS.
- 2026-09-04 — okno rollbacku do ProxySQL 3.0.10 zamknięte świadomie, na decyzję operatora („3.0.10 mnie nie interesuje, ma działać 3.0.11"). Z `x12p1` i `x12p2` usunięte oba artefakty powrotu: `proxysql.db.pre-3.0.11` oraz `/var/tmp/proxysql-3.0.10-1.x86_64.rpm`. Uzasadnienie techniczne dla kopii bazy jest niezależne od decyzji operatora: `proxysql.db` trzyma CAŁĄ konfigurację (`mysql_servers`, `mysql_users`, `mysql_galera_hostgroups`), a zamrożony snapshot z 2026-09-03 01:18 z każdą zmianą najemcy coraz bardziej rozjeżdżał się z żywym plikiem — jego przywrócenie po miesiącu cofnęłoby hostgroupy, i to bez błędu przy starcie. Dowód, że powrót nie jest potrzebny: `make platform-deploy PLATFORM=xenonv12` (kanoniczna ścieżka świeżej instalacji: `get_url` z sumą z lockfile → import klucza GPG po odcisku → `dnf install` przypiętego RPM) zbiega się z `changed=0` na obu węzłach, czyli stan z lockfile == stan zainstalowany.
- 2026-09-04 — `versions/candidate.lock.yml` podniesiony 3.0.9 → 3.0.11 (decyzja operatora; wariant alternatywny — usunięcie martwego pliku — odrzucony). Plik nie był wskazywany przez żadną definicję, ale był miną: `rpm_release` pozostawał placeholderem `to-confirm-F0` (URL RPM-u nieskładalny), `repo_baseurl` i `repositories[].url` wskazywały `rocky/9` (404 u wydawcy; oficjalna ścieżka to `centos/<major>`), a brak `rpm_sha256` powodował, że `platform_install.yml` odmawiał instalacji. Po zmianie plik ma ten sam kontrakt integralności co lockfile'e LOCKED: przypięta suma `41f0d7ae…8fff`, `source` wskazujący wydanie i odcisk GPG `653F85BB…C97E` zamiast `to-verify-F2`. Dowód: URL złożony z pól pliku dokładnie tak, jak robi to `platform_install.yml`, pobrany i przeliczony — suma zgodna z pinem.
- 2026-09-04 — ZNANA LUKA, świadomie niezamknięta: rygor `validate-lockfile.py` zależy od samodeklaracji pliku (`is_locked` z regexu na stopce `# Status: LOCKED`, linia 94). Lockfile ze stopką `candidate` przechodzi ścieżką łagodną — bez kontroli placeholderów ISC-63, kompletu kluczy i formatu sum — nawet gdy WSKAZUJE go definicja klastra. CI waliduje lockfile'e po referencji, więc taki plik zostałby sprawdzony, ale przeszedłby na zielono, a fail-closed przyszedłby dopiero na hoście (asercja o braku `rpm_sha256` dla architektury). Poprawka klasy błędu to uzależnienie rygoru od faktu bycia wskazanym, nie od stopki; operator wybrał podniesienie samego pliku (wariant B) zamiast zmiany kontraktu walidatora (wariant C).

## Verification

- ISC-1: PASS — lab2-cluster wdrożony na czystych kontenerach (f2_install + site.yml + bootstrap + f5_join, wszystkie taski PASS, failed=0). 2026-07-24.

- ISC-2: PASS — idempotentny converge: f3_galera_config re-run → config changed=False (server.cnf stabilny); F11 monitoring changed=0 na wszystkich hostach. 2026-07-24.

- ISC-11: PASS — chaos-failover.py: numbered workload przez VIP:6033 → transakcje widoczne na węźle ocalałym (Galera sync replikacja); e2e_test.orders zreplikowane na gnode2/gnode3. 2026-07-24.

- ISC-12/13: PASS — bootstrap.yml wymaga -e confirm=yes + assert ansible_play_hosts==1 (single-node); site.yml NIE zawiera --wsrep-new-cluster (ISC-65 guard). 2026-07-24.
- ISC-3: PASS — MariaDB-server-11.4.12-1.el9, galera-4-26.4.27-1.el9, MariaDB-backup-11.4.12-1.el9 na gnode1-3; proxysql-3.0.9-1.aarch64 na pnode1-2; rpm -q potwierdzone na hostach 2026-07-22; zgodne z versions.lock.yml.
- ISC-43: `probe-no-secrets-leak.sh` PASS lokalnie 2026-07-22 — brak sekretów w śledzonych/nieignorowanych plikach i argv; kontrola negatywna odrzuciła literalne sekrety quoted, unquoted i Compose fallback. Losowe dane labu są w ignorowanym `tests/lab/.env` (0600), poza build context.
- ISC-58: validate-cluster-schema.py PASS lokalnie 2026-07-22 — cluster.yml zgodny z schema + semantic checks (production locked, max_writers=1, R/W split off). Probe gotowy do CI.
- ISC-58 (aktualizacja 2026-08-20): PASS — macierz schema reject/accept (10 testow) oraz validate-cluster-schema.py dla 5/5 cluster.yml; pola-widma sa odrzucane, rto_node_failure i oba parametry wsrep maja potwierdzonych konsumentow, a invariant max_writers=1/read-write split OFF pozostaje w f7. Dowod: tests/unit/test_cluster_schema_contract.py, fokusowe wyniki PASS.
- ISC-62: 7 runbook stubs utworzone (bootstrap, total-outage, node-replacement, backup, restore, upgrade, decommission) w docs/runbooks/; do uzupełnienia w F4/F9/F10/F12/F13/F14.
- ISC-63: PASS — F2 install playbook używa state: present (nie latest); probe-no-state-latest.sh PASS; F2 preflight+install na 5/5 hostów 2026-07-22.
- ISC-66: PASS — F0 discovery uruchomiony na 5/5 kontenerów Rocky 9.8 (lab-cluster); 29 tasków PASS każdy (PLAY RECAP ok=29 failed=0); raporty /var/tmp/f0-discovery-*.json zawierają OS/kernel, CPU/RAM/NUMA, dyski/fs/mount, DNS/routing/ports, SELinux/firewalld, repo/pakiety, istniejące MariaDB/ProxySQL (brak), monitoring (brak). F0 nie instalował fio (allow_bench=true tylko gnode1, ale fio nie było zainstalowane — lab ograniczenie). Commit 2026-07-22.
- ISC-67: PASS — F0 discovery read-only; changed=0 na wszystkich hostach (poza zapisem raportu changed=1); brak modyfikacji usług; ansible-playbook 2026-07-22.
- ISC-6: PASS — F2 preflight na 5/5 hostów (assert Rocky 9, RAM >=2GB, disk >=5GB, clean MariaDB); serial:1 max_fail_percentage:0; failed=0 2026-07-22.
- ISC-46: PASS — probe-pmm-native.py potwierdza PMM `3.8.1`, 5 generic nodes, 5 node_exporter 1.12.1, 3 MySQL services oraz 2 ProxySQL metric exporters (external_exporter port 6070, restapi włączone trwale). Galera/MariaDB przez mysqld_exporter+QAN, ProxySQL przez restapi `/metrics` (311+ metryk). PMM Prom zwraca `proxysql_servers_table_version_total` dla obu węzłów (`up=1`). Dowód: `make lab-monitoring-verify` PASS 2026-07-23.
- F11 monitoring idempotence: PASS — po rotacji danych i upgrade node_exporter drugi `make cluster-monitoring CLUSTER=lab-cluster` zakończony `changed=0 failed=0` dla gnode1-3, pnode1-2 i localhost.
- F11 PMM version preflight: PASS — kontrola negatywna z oczekiwanym `0.0.0` została odrzucona przed pierwszym playem hostowym; aktywny runtime `3.8.1` odpowiada `versions.lock.yml`.
- F11 restart persistence i live scrape: PASS — PMM odtworzony z digest-pinned obrazu `3.8.1`, health=`healthy`, pamięć=4GiB, nofile=1M, automatyczne aktualizacje wyłączone. Po restarcie probe potwierdził świeże inventory/QAN/metryki, 0 zarządzanych reguł, 0 custom templates `isa_*` i brak folderu alertów (404). PMM porty są tylko na `127.0.0.1`; stare domyślne hasło zwraca 401, losowe aktywne hasło 200.
- ISC-47 (F15): PASS — `f15_alerts.yml` provisionuje 4 reguły + email contact point + notification policy. Dowód detekcji: stop gnode3 → reguła node-loss state=Alerting (cluster_size=2<3). Dowód delivery: alert → 1 email dostarczony do maildev (SMTP 172.28.0.70:1025, GF_SMTP_* na pmm-server). BLK-5 rozstrzygnięty (Email/SMTP). 2026-07-24.
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
- ISC-32: PASS — Rocky 10: artefakt opublikowany i odtworzony przez S3, zarządzany SMB/CIFS oraz wcześniej zamontowany filesystem; staging usunięty po operacji 2026-07-29.
- ISC-33: PASS — bieżący runner zapisuje strumieniowy AES-256-GCM v3 (`GB3G`, nagłówek jako AAD, atomowa publikacja po weryfikacji tagu) i odczytuje v2/v1. Dowód live 2026-09-04 na `orionv15-r10`: artefakt `galera-orionv15-r10-20260904-220010` ma magic `GB3G` i `encryption_method: aes-256-gcm-stream-pbkdf2-sha256`, a szczyt RSS całego drzewa runnera to 57 088 KiB przy pliku 49 264 688 B. Kompatybilność v2/v1 oraz tamper/wrong-key pokrywają testy.
- ISC-34: PASS — sha256 `backup.tar.enc` jest zgodny z `backup.sha256`; read-back przechodzi na wszystkich trzech backendach 2026-07-29.
- ISC-35: PASS — nowe metadata mają `format_version: 3`; restore i oba backendy akceptują wyłącznie wersje artefaktu `{1,2,3}`, a marker ownership zachowuje osobny format 1. Testy `fetch_latest` potwierdzają wybór istniejących artefaktów v2. Dowód live 2026-09-04: `lab-backup-verify` na `orionv15-r10` przeszedł na artefakcie v3 z metadanymi `11.4.12`, `seqno=0`, `cluster=orionv15-r10`.
- ISC-36: PASS — confirmation-gated restore na izolowanym hoście grupy `restore` przechodzi checksum, bezpieczny tar, copy-back, `mariadb-check` i dodatnią liczbę wierszy. Ostatni dowód 2026-09-04: `o15r1` odtworzył artefakt v3 `galera-orionv15-r10-20260904-220010`, `lab-restore-verify` zweryfikował 5 wierszy.
- ISC-37: PASS — restore zapisuje sukces w per-klastrowym `state.json`; probe porównuje świeżość z `restore_test_schedule`, bez automatycznego crona restore 2026-07-29.
- ISC-38: PASS — błędne scoped S3 credentials kończą preflight kodem `E_STORAGE_AUTH`, bez nowych obiektów i stagingu; metryka porażki jest natychmiastowa, a F15 zarządza regułami failure/stale 2026-07-29.
- ISC-39: PASS — backup pod obciążeniem nie degraduje writera. Poprzedni dowód (2026-07-22) był nieważny: sonda mierzyła okno bez backupu, więc zera były wymuszone konstrukcją pomiaru. Po naprawie `tests/lab/backup-impact.py` (jawne `galera_backup_action=run` + wymóg wzrostu `last_success`) zmierzone na żywo 2026-09-04 na `orionv15-r10`: flow control **0 ns** (próg 2 000 000 000 ns), max write stall **0,04 s** (próg 8 s) przy **415 commitach** przez VIP w oknie backupu, a kopia realnie powstała (`last_success 1788559211 → 1788562333`). Falsyfikowalność potwierdzona obserwacją czerwonego stanu w tej samej sesji: przebieg `configure` (bez przełącznika) pozostawia `last_success` bez zmian i „Execute manual backup runner" ze statusem `skipping` na wszystkich węzłach — dokładnie ten warunek nowa asercja odrzuca.
- ISC-50: PASS — `f12_rolling_restart.yml`: play Galera `serial:1`; restart gnode1→gnode2→gnode3 każdy po kolei; probe-rolling-restart PASS (static serial:1 + runtime) 2026-07-23.
- ISC-51: PASS — brama zdrowia (until: wsrep_local_state=4 + Primary + size=3 + ready=ON) po każdym węźle przed kolejnym; każdy węzeł rejoined Synced; probe-rolling-restart PASS 2026-07-23.
- ISC-52: PASS — `f12_patch.yml` canary: pierwszy non-writer (gnode1) patchowany + health gate przed kontynuacją; probe-patch PASS 2026-07-23.
- ISC-53: PASS — `f12_upgrade_plan.yml` read-only: taski hostowe changed_when:false (odczyt wersji/gcache/grastate); Galera changed=0; probe-upgrade-plan PASS 2026-07-23.
- ISC-54: PASS — plan docs/plans/major-upgrade-plan.md: ścieżka 11.4 LTS → 12.3 LTS, mariadb-upgrade --skip-write-binlog, bez regresji EOL; źródła mariadb.com/kb/en/upgrading-galera-cluster + galeracluster.com; probe-upgrade-plan PASS 2026-07-26.
- ISC-55: PASS — `f12_patch.yml`: brama zdrowia po każdym węźle (until/retries) — porażka zatrzymuje rolling; 3 bramy (canary/rolling/writer); probe-patch PASS 2026-07-23.
- ISC-56: PASS — anti-downgrade guard: `f12_upgrade_plan.yml` assert odrzuca gdy obecna >= docelowa (test negatywny target=11.2 → FAILED z cytatem "forward-incompatible"); probe-upgrade-plan PASS 2026-07-23.
- ISC-57: PASS — `f12_patch.yml` ProxySQL play `serial:1` + SAVE ... TO DISK przed patch; każdy węzeł po kolei, health-gated (backends ONLINE); probe-patch PASS 2026-07-23. Dowód live 2026-09-04 na parze `xenonv12`: rolling upgrade 3.0.10 → 3.0.11 wykonany po jednym węźle (x12p1, potem x12p2), oba przeszły bramę backendów. Wersję potwierdza sam działający proces przez własny interfejs admina (`global_variables.admin-version` = `3.0.11-797-g7c91137`, zgodne z commitem wydania `7c91137`), a nie tylko `proxysql --version` z dysku; `ActiveEnterTimestamp` usługi jest 5 s po `INSTALLTIME` pakietu na obu hostach, więc restart nastąpił po instalacji.
- ISC-21 (F13 drift): PASS — `f13_drift.yml` read-only: ProxySQL main-vs-disk (mysql_servers/galera_hostgroups/mysql_users CLEAN) + Galera cluster_state_uuid spójny. Falsyfikowalny: inject unsaved INSERT → mysql_servers=DRIFT detected; cleanup → CLEAN. probe-drift PASS 2026-07-23.
- ISC-58: PASS — clusters/example-cluster/{cluster.yml,inventory.yml} template; roles/playbooks nie referencjują clusters/lab-cluster (probe-zero-hardcode). Nowy klaster = clusters/<name>/ tylko. 2026-07-23.
- ISC-59: PASS — 0 hardcodowanych IP/nazw/secretów w roles/playbooks (probe-zero-hardcode.py); parametryzacja zweryfikowana idempotentnym renderem f3 (changed=False, klaster zdrowy). 2026-07-23.
- ISC-62: PASS — 7 runbooków (bootstrap, total-outage, node-replacement, backup, restore, upgrade, decommission) zaktualizowane; 0 STUB; wszystkie komendy istnieją w Makefile. 2026-07-23.
- ISC-60: PASS — dwa klastry izolowane: lab1 (UUID 69c257c2, sieć 172.28.0.x, VIP 172.28.0.30, nazwa lab_galera) vs lab2 (UUID 951cfac6, sieć 172.29.0.x, VIP 172.29.0.30, nazwa lab2_galera) — osobne docker networks, osobny MinIO (172.29.0.60), osobny PMM namespace (lab2-galera). 2026-07-23.
- ISC-61: FULL PASS — lab2 przechodzi PEŁNĄ suitę probe'ów (jak lab1): galera, proxysql, endpoint(VIP), hardening, drift, gcache, backup(minio2), restore(rnode1b), backup-impact(flow_control 0, 650 commits), chaos-failover(writer kill, gap 5.1s <120s RTO, 606 txn 0 lost). Monitoring: proxysql+galera series scrapeowane w PMM (lab2-galera). Probe-portability fix: de-hardcoded gnode1/pnode1 w probe-hardening/gcache/chaos-failover/backup-impact → inventory-derived. 2026-07-24.
- ISC-65: PASS — `probe-no-double-bootstrap.py`: jedyny bootstrap play (bootstrap.yml) jest single-host-safe (serial:1 + assert ansible_play_hosts==1) + confirm-gated; 0 innych playbooków z --wsrep-new-cluster w shell/command. 2026-07-23.
- F13 node lifecycle: PASS — `f13_remove_node_plan.yml` read-only (quorum guard 3→2 OK, writer-detection: gnode2=nie, gnode3=TAK+warn); `f13_remove_node.yml` confirm-gated (odmawia bez confirm=yes). Plan + guard zweryfikowane; destruktywne usunięcie nie testowane w labie (3→2). 2026-07-23.
- ISC-68: PASS — `probe-gcache.py`: write_rate=55666 B/s (zmierzony workload RPAD('x',1024), delta wsrep_replicated_bytes); gcache.size=128M pokrywa wymóg (55000×30min×60≈96MB → floored 128M). deployed=128M=required. calc-gcache.py + probe-gcache.py. 2026-07-24.
- ISC-4: PASS — `probe-selinux.sh` uruchomiona przez `ansible ... -m script` na KAŻDYM hoście obu klastrów produkcyjnych: `getenforce` = `Enforcing` na 14/14 (finalclaude-r9: f9g1-3, fcp1-2, fcinfra, f9r1; finalclaude-r10: f10g1-3, fcp1-2, fcinfra, f10r1). 2026-08-14.
- ISC-5: PASS — dwa niezależne dowody. (1) `probe-firewalld.sh` z allowlistą per rola na 14/14 hostów: galera 7 portów (22,3306,4444,4567,4568/tcp, 4567/udp, 9100), proxysql 5 (22,6032,6033,6070,9100), infra 6 (22,80,443,8025,9000,9001), restore 1 (22) — żadnego portu spoza allowlisty, żadnej usługi poza `dhcpv6-client`, żadnej aktywnej strefy z przypisaniem `sources:` (taka wyprzedza public i ominęłaby allowlistę). (2) `firewall.yml --check --diff` na obu klastrach: `changed=0` na 14/14 → żywa strefa jest bajt w bajt wyrenderowanym `public.xml.j2`, brak dryfu runtime; w tym samym przebiegu przeszły bramki playbooka (`--state`=running, target=`default`, każdy zarządzany interfejs w `public`). Kontrola negatywna na żywym hoście: `firewall-cmd --add-port=9999/tcp` (runtime) → sonda FAIL „port poza allowlistą: 9999/tcp"; po `--reload` → PASS. 2026-08-14.
- `probe-firewalld.sh` — naprawa sondy przy okazji ISC-5. Poprzednia wersja czytała wyłącznie linie `ports:` z `--list-all-zones`, a cała polityka tego projektu to rich-rule powiązane ze źródłowym CIDR: zbiór otwartych portów wychodził PUSTY i sonda meldowała PASS nie zmierzywszy niczego (próżny zielony). Dodatkowo skanowała strefy nieaktywne (`home`, `internal`), zapalając się na domyślnych usługach dystrybucji (`cockpit`, `dhcp`, `dns`, `mdns`), których nikt nie wystawia. Po zmianie: czyta rich-rule i porty proste wyłącznie ze stref AKTYWNYCH, odrzuca strefy źródłowe, wymaga allowlisty jako argumentu (wariant bezargumentowy dawał PASS bez pomiaru — usunięty, `rc=2`) i traktuje „allowlista podana, zero wykrytych portów" jako FAIL, nie sukces. 2026-08-14.
- ISC-12 / ISC-13: PASS — `probe-no-double-bootstrap.py`: jedyny play wykonujący bootstrap to `playbooks/bootstrap.yml`, jest single-host-safe (`serial: 1` + assert `ansible_play_hosts == 1`) i confirm-gated; żaden inny playbook nie wywołuje `--wsrep-new-cluster` w `shell`/`command`, więc `site.yml` nie może go uruchomić ubocznie. Sonda uruchomiona 2026-08-14.
- ISC-44: PASS — na `finalclaude-r9` (`tls.mode=full`) połączenie klienta z obcym CA jest odrzucane przez serwer: `mariadb --ssl-ca=<rogue> --ssl-verify-server-cert -h 192.168.1.150` → `ERROR 2026 (HY000): TLS/SSL error: self-signed certificate in certificate chain`. Certyfikat rogue generowany jednorazowo na hoście i kasowany. Zweryfikowana jest klauzula „niezaufany certyfikat"; wariant „nieważny (wygasły)" wymagałby podmiany certyfikatu serwera i nie był testowany. 2026-08-14.
- ISC-1: PASS (dowód historyczny) — flota `finalclaude` została zbudowana OD ZERA na czystych VM Rocky 9 i Rocky 10 i przeszła komplet 8 kryteriów akceptacji Fazy 5 bez ręcznej interwencji: `docs/plans/from-scratch-revalidation.md:3-4` („✅ WYKONANY 2026-08-02"). Że był to realny przebieg, a nie papier, dowodzi 5 defektów możliwych do wykrycia WYŁĄCZNIE przy budowie od zera (`svcaccs: null` na świeżym MinIO, zaszyty `rnode1` w sondzie restore, nieaktualna lista reguł alertowych, RAM trafiający w próg preflightu, brak pojęcia własności warstwy współdzielonej w f2/f11) — commity `7e1429e`, `9eed120`, `a9ab24a`. 2026-08-02.
- ISC-2: PASS (zmierzone świeżo na bieżącym drzewie) — 2026-08-14, po zmergowaniu PR #5/#6/#7, realny `site.yml` uruchomiony DWUKROTNIE na obu klastrach. Pierwszy przebieg nanosi zaległy stan (`changed=2` na węzeł: drop-iny systemd z PR #3), drugi raportuje **`changed=0` na 6/6 węzłów Galery** (finalclaude-r9: f9g1-3; finalclaude-r10: f10g1-3), `failed=0`, `unreachable=0`. Po converge klastry nietknięte: `wsrep_cluster_size=3`, `Synced` na 6/6; VIP 192.168.1.133 nadal na `fcp1`; limity z PR #3 obowiązują na żywych procesach (`/proc/<pid>/limits`: `nofile=1048576` dla mariadbd i proxysql). Dowód historyczny (poprzedni): `docs/plans/rocky10-dual-platform-plan.md:222` i `:286` — `changed=0` na 7/7, 2026-08-02.
- OTWARTE — ISC-1 pozostaje na dowodzie historycznym z 2026-08-02: budowy od zera nie da się powtórzyć na działającej flocie bez teardownu, a ten wymaga poświadczeń Proxmoxa i osobnej decyzji. Sprawdzone 2026-08-15: `PROXMOX_VE_ENDPOINT`/`PROXMOX_VE_API_TOKEN` nie występują ani w środowisku, ani w plikach `.env`, a API nie odpowiada na `:8006` — teardown jest niewykonalny. ISC-2 został zamknięty świeżym pomiarem (wpis wyżej), więc `--check` nie jest już potrzebny jako substytut — dla porządku pozostaje ustalenie z 2026-08-14, że nie nadaje się na taki substytut: brama zdrowia Galery przerywa play w check mode komunikatem `Command would have run if not in check mode` (`failed=1`).
- TLS runda 3 na `finalclaude-r9` — WYKONANA 2026-08-15. Zdjęty `socket.dynamic` (fallback dopuszczający nieszyfrowaną replikację) i włączone `[sst] encrypt=3`. Runtime po zmianie na 3/3 węzłach: `socket.ssl = YES`, brak `socket.dynamic`. Kolejność miała znaczenie: konfiguracja trafiła na WSZYSTKIE węzły przed jakimkolwiek restartem (`f3_galera_config.yml --skip-tags restart`), bo skrypt SST czyta `[sst]` z pliku w momencie transferu — dzięki temu dawca i przyjmujący zgadzają się co do szyfrowania nawet zanim `mariadbd` przeładuje `wsrep_provider_options`. To usunęło powód, dla którego procedura wymagała jednoczesnego restartu trójki, więc aktywacja poszła rolling (`f12_rolling_restart.yml`, `serial: 1`) — klaster ani razu nie zszedł poniżej `size=3`, zero przestoju. Wybór rolling zamiast pełnego postoju był istotny: wszystkie trzy węzły miały `safe_to_bootstrap: 0`, więc jednoczesny stop groziłby wejściem w runbook `total-outage` z `--wsrep-recover`.
- TLS runda 3 — DOWÓD SZYFROWANEGO SST (nie tylko konfiguracji). Wymuszono PEŁNY SST na non-writerze: `systemctl stop mariadb` + usunięcie `grastate.dat` na `f9g1`, następnie start. Log dawcy `f9g2` pokazuje strumień przez `socat -u stdio openssl-connect:192.168.1.150:4444,no-sni=1,cert=/etc/mysql/tls/server-cert.pem,key=...,cafile=/etc/mysql/tls/ca.pem` — przed zmianą byłby to goły `socat` po TCP. Joiner: `Proceeding with SST`, `mariabackup SST completed on joiner`, `SST succeeded for position ...:4340`, zero `Broken pipe`. Po dołączeniu klaster 3/3 `Synced`, `f9g1` wrócił do hostgroup 120 ProxySQL jako ONLINE, `mysql_up=1` na 6/6 węzłów floty. 2026-08-15.
- NIE ZROBIONE świadomie — `require_secure_transport` zostaje `false`. To osobny krok, nie część rundy 3: ProxySQL łączy się do backendów z `use_ssl=0`, a `mysqld_exporter` i agent QAN idą TCP na `127.0.0.1:3306` (wyjątek dla gniazda unixowego ich nie obejmuje). Włączenie przed przełączeniem tamtych trzech konsumentów zshunnowałoby hostgrupy i wygasiło metryki. Kolejność w `docs/records/2026-08-02-session-handoff.md`.


## Blockers

- ~~BLK-1~~ ROZBLOKOWANY 2026-07-22 — 5 kontenerów Rocky Linux 9.8 (OrbStack/Docker): 3 Galera + 2 ProxySQL; SSH + sudo NOPASSWD; tests/lab/docker-compose.yml.
- ~~BLK-2~~ ROZBLOKOWANY 2026-07-22 — inventory lab-cluster (clusters/lab-cluster/inventory.yml) z SSH key (tests/lab/ssh_key); Ansible połączenie PASS na 5/5 hostów.
- ~~BLK-3~~ ROZSTRZYGNIĘTY 2026-07-29 — sekrety pozostają poza repo; backup ma backend S3, SMB lub filesystem, scoped credentials per klaster i retencję z `cluster.yml` (Decisions).
- ~~BLK-4~~ ROZSTRZYGNIĘTY 2026-07-22 — internet dostępny; F1 research wykonany z oficjalnych źródeł.
- ~~BLK-5~~ ROZSTRZYGNIĘTY 2026-07-24 — alert delivery = Email (SMTP). Lab: maildev SMTP catcher (172.28.0.70:1025) + GF_SMTP_* na pmm-server; contact point "ISA Email Alerts" + notification policy (managed_by=ansible → email). Dowód: node-loss alert → 1 email dostarczony do maildev.

## Następny pojedynczy feature
Wszystkie kryteria fabryki (ISC) PASS: 64 w pełni, 4 z zastrzeżeniem (`[~]` — ISC-1, ISC-22, ISC-44, ISC-66; powód przy każdym w Verification). Wliczając ISC-44 (TLS full: zaimplementowane + zweryfikowane na lab2 — server TLS, replikacja Galera/SST/ProxySQL→backend przez TLS, niezaufany cert odrzucony; nieważny/wygasły cert pozostaje nieprzetestowany). Fabryka: produkcyjne klastry VM (Rocky 9/10 na Proxmox VE), monitoring, backup/restore, alerty (email), rolling ops, drift detection, runbooki, gcache z pomiaru. Ścieżka systemd MariaDB obowiązuje we wszystkich playbookach po wycofaniu labu kontenerowego (zachowanego na branchu `lab/docker-podman`).
