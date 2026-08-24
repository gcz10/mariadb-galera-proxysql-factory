# Plan: uszczelnienie granic własności i bezpieczników

**Status:** W TOKU — P0/P1/P2-6/P2-8/P2-9/P3 zamknięte; P2-7 ma blocker integralności, otwarte P2-10 i dług testowy.
**Zrobione:** P0-1 (`85ff115`), P0-2 (`2d7df6d`), P1-3/P1-4/P1-5, P2-6/P2-8/P2-9, P3 (higiena, komplet). P2-7 funkcjonalnie PASS, security BLOCKED.
**Baza:** `main` @ `f1a3068`. Każda pozycja poniżej została zweryfikowana na kodzie;
tezy recenzentów, których kod nie potwierdził, są wypisane na końcu.

Zasada porządkująca: najpierw granice własności i klasyfikatory stanu, potem
poświadczenia, potem bezpieczne wartości domyślne, na końcu ergonomia i higiena.
Kolejność wynika z tego, że dwa pierwsze punkty mogą uszkodzić cudzy klaster,
a reszta kosztuje czas operatora, nie dane.

---

## P0-1. Najemca przepisuje firewall warstwy wspólnej — ZROBIONE

**Problem.** `make cluster-deploy` (`Makefile:265`) uruchamia `playbooks/firewall.yml`
z inwentarzem i `cluster.yml` najemcy. Playbook celuje w
`{{ firewall_target_hosts | default('all') }}`, a inwentarze najemców deklarują
również `fcp1`, `fcp2`, `fcinfra`, `fcapp` jako pełnoprawne hosty. Szablon
`roles/firewall/templates/public.xml.j2` wybiera reguły po `group_names`, więc na
`fcp1/fcp2` generuje porty `6033`, `6032`, `6070`, `9100` i VRRP z `network.*`
**bieżącego najemcy**, nadpisuje `/etc/firewalld/zones/public.xml` i przeładowuje
firewalld.

**Dlaczego dziś tego nie widać.** Oba żywe klastry mają identyczne, szerokie
`192.168.1.0/24`. Trzeci najemca z węższym CIDR-em odetnie pozostałych od
wspólnego ProxySQL.

**Dodatkowo.** `tests/lab/probe-firewall.py` sprawdza hosty z inwentarza najemcy
przeciw konfiguracji najemcy — czyli obecna bramka **potwierdza** błędny stan
zamiast go łapać.

**Precedens do skopiowania.** `playbooks/f8_keepalived.yml`, `platform_proxysql.yml`,
`platform_adopt.yml` i `infra_services.yml` odrzucają konfigurację najemcy
warunkiem `platform.name is defined` / `galera is not defined`.

**Zmiana.**
1. `cluster-firewall` i `cluster-deploy` przekazują `firewall_target_hosts: galera:restore`.
2. Nowy cel `platform-firewall` obejmuje `proxysql:infra:app` i jest wywoływany z `platform-build`.
3. `firewall.yml` dostaje bramkę własności: mutacja hostów warstwy wspólnej wymaga definicji platformy.
4. `probe-firewall.py` przestaje sprawdzać hosty wspólne z perspektywy najemcy.

**Akceptacja (falsyfikowalna).** Sonda statyczna: żaden mutujący cel `cluster-*`
nie może rozwinąć się do hostów z grup `proxysql`/`infra`/`app`. RED przed zmianą
na obecnym `cluster-deploy`, GREEN po. Dodatkowo test: najemca z CIDR-em
`10.40.8.0/24` nie zmienia `public.xml` na `fcp1`.

**Koszt:** średni.

**Wynik (`85ff115`).** `firewall.yml` ma bramkę własności sprawdzaną per host;
`cluster-deploy` i `cluster-firewall` przekazują
`firewall_target_hosts=galera:restore`; nowy `platform-firewall` (wpięty w
`platform-build`) obejmuje `proxysql:infra:app`; `probe-firewall.py` weryfikuje
wyłącznie hosty należące do danej warstwy. Dowód live na `fcp1`: definicja
najemcy odrzucona przed mutacją (`changed=0`), definicja platformy przechodzi.

---

## P0-2. Klasyfikator stanu Galery jest fail-open — ZROBIONE

**Problem.** `playbooks/bootstrap.yml:30-77` sonduje węzły komendą z
`failed_when: false` i `ignore_unreachable: true`, a następnie klasyfikuje wyniki
tylko dwiema regułami: `stdout` pasuje do `Primary` → żywy Primary, `stdout`
niezdefiniowany → nieosiągalny. Host osiągalny po SSH, którego sonda zwróciła
`rc != 0` i pusty `stdout` (błąd uprawnień do socketu, zła ścieżka, błąd auth),
**nie trafia do żadnej z tych kategorii**. Obie asercje przechodzą i playbook
bootstrapuje `galera[0]` — przy żywym Primary na niesklasyfikowanym węźle daje to
drugi Primary Component.

`playbooks/cluster_recover.yml:44-88` ma tę samą lukę z odwróconym skutkiem:
niejednoznaczna sonda przechodzi jako „klaster stoi", a Play 2 zatrzymuje
wszystkie węzły — awaria wyprodukowana z fałszywego alarmu.

**Zmiana.** Jeden wspólny klasyfikator, konsumowany przez oba playbooki:

```
PRIMARY          stdout pasuje do wzorca Primary
NON_PRIMARY      poprawna odpowiedź, inny stan
DOWN_VERIFIED    rc != 0 ORAZ stderr wskazuje brak socketu (ERROR 2002)
UNREACHABLE      unreachable = true
UNKNOWN          wszystko pozostałe
```

`UNKNOWN` blokuje każdą operację destrukcyjną, bez wyjątku i bez flagi
potwierdzenia. `DOWN_VERIFIED` jest jedyną podstawą do bootstrapu.

**Akceptacja.** Tabela stanów w teście jednostkowym renderująca **wyrażenia
wyjęte z playbooka** (wzorzec z `tests/unit/test_platform_pmm_upgrade_contract.py`),
z wierszami: `rc=1/stdout=""/stderr="Access denied"` → `UNKNOWN` → asercja blokuje.

**Koszt:** mały.

**Wynik (`2d7df6d`).** `playbooks/tasks/galera_state_probe.yml` jest jedynym
klasyfikatorem; `bootstrap.yml` i `cluster_recover.yml` konsumują go przez
`include_tasks`. Każda kategoria wymaga dowodu pozytywnego, a `UNKNOWN` liczy
się różnicą względem grupy — model jest totalny. Statyczna bramka ISC-65 wchodzi
w `include_tasks` i wymaga obu asercji (`primary`, `unknown`).

Smoke test na żywym n17 ujawnił defekt samej poprawki: wynik pominięty w
`--check` ma `rc: 0` i puste `stdout`, więc trzy węzły Primary trafiały do
`non_primary`. Klasyfikator wymaga teraz dopasowania `wsrep_cluster_status`
w `stdout`; brak odpowiedzi to `UNKNOWN`. Dowód live: bootstrap na działającym
klastrze odmawia, wskazując węzły o nieznanym stanie.

---

## P1-3. Backup trzyma poświadczenia admina ProxySQL do odczytu jednej informacji — ZROBIONE

**Problem.** `playbooks/platform_proxysql.yml:191-201` rejestruje `isa_admin`
w `admin-admin_credentials`, czyli w puli read-write. `playbooks/f10_backup.yml:51-53`
i `roles/galera_backup/templates/secrets.env.j2` zapisują to hasło na węźle
Galery pełniącym rolę schedulera. Runner
(`roles/galera_backup/files/galera_backup/pipeline.py`) używa go wyłącznie do
ustalenia, czy scheduler jest aktualnym writerem.

Kompromitacja jednego węzła bazy daje więc pełne prawa zapisu do wspólnego
ProxySQL całej floty.

**Korekta względem recenzji.** Recenzent zaproponował użycie
`admin-stats_credentials` do odczytu `runtime_mysql_servers`. To nie zadziała:
dokumentacja ProxySQL mówi wprost, że konta z `admin-stats_credentials`
„are only allowed to read from the statistics and monitoring tables" i **nie**
mogą czytać tabel konfiguracyjnych
(https://proxysql.com/documentation/global-variables/admin-variables).
`runtime_mysql_servers` należy do schematu konfiguracyjnego.

Tożsamość writera da się natomiast wyprowadzić z tabeli statystycznej
`stats_mysql_connection_pool`, która ma kolumny `hostgroup`, `srv_host`, `status`
(https://proxysql.com/documentation/the-admin-schemas/stats/stats-mysql).

**Zmiana.**
1. Platforma rejestruje osobne konto read-only w `admin-stats_credentials`.
2. Scheduler dostaje wyłącznie to konto; `GALERA_BACKUP_PROXYSQL_ADMIN_*` znika z węzłów bazy.
3. Guard writera pyta `stats_mysql_connection_pool` o hostgroupę writera najemcy.

**Akceptacja.** Test: uruchomienie guardu z poświadczeniem stats kończy się
sukcesem; próba `UPDATE` tym samym poświadczeniem jest odrzucona przez ProxySQL.
Sonda sekretów: brak `PROXYSQL_ADMIN_PASSWORD` w plikach na hostach `galera`.

**Koszt:** średni.

**Wynik.** Platforma rejestruje `isa_stats` w `admin-stats_credentials`
(`platform_proxysql.yml`, idempotentnie); przy okazji znika fabryczne
`stats:stats`. Strażnik writera czyta `stats_mysql_connection_pool`
(kolumny `hostgroup`/`srv_host`), bo konto read-only nie widzi schematu
konfiguracyjnego. `secrets.env` na węźle Galery zawiera wyłącznie
`GALERA_BACKUP_PROXYSQL_STATS_*`.

Zmierzone na żywym green (2026-08-24), z hosta runnera `grg1` przez VIP `.27:6032`:

| Próba kontem `isa_stats` | Wynik |
|---|---|
| `SELECT srv_host FROM stats_mysql_connection_pool WHERE hostgroup=890` | `192.168.1.30` |
| `SELECT hostname FROM runtime_mysql_servers` | `ERROR 1045: no such table` |
| `UPDATE global_variables ...` | `ERROR 1045: attempt to write a readonly database` |
| stare `stats:stats` | `ERROR 1045: Access denied` |

Grep po `$PROXYSQL_ADMIN_PASSWORD` na wszystkich czterech węzłach klastra
(`/opt/galera-backup`, `/etc`): zero trafień. Backup i dryl odtworzenia
przeszły nowym poświadczeniem (`galera-green-r9-20260824-122356`, 4536
wierszy zweryfikowanych), bramka po budowie 13/13 PASS.

**Zgodność z dokumentacją ProxySQL.** Pomiar pokrywa się ze specyfikacją:
`admin-stats_credentials` to poświadczenia, które „are not allowed to update
internal data structures (...) neither to read configuration tables. They are
only allowed to read from the statistics and monitoring tables"
(global-variables/admin-variables); `stats_mysql_connection_pool` ma
kolumny `hostgroup`/`srv_host`/`srv_port`/`status`
(the-admin-schemas/stats/stats-mysql); `ONLINE` znaczy „fully operational"
(main-runtime/mysql-tables); zmiana wchodzi przez `LOAD ADMIN VARIABLES TO
RUNTIME` + `SAVE ADMIN VARIABLES TO DISK` (the-admin-schemas/admin-commands).

**Pusta live tabela po zimnym starcie — granica dowodu.** Oficjalna zmienna
`admin-stats_mysql_connection_pool` (domyślnie `60`) steruje odświeżaniem
**historycznych** statystyk w `proxysql_stats.db`, nie tabeli live
`stats_mysql_connection_pool`
(https://proxysql.com/documentation/global-variables/admin-variables/#admin-stats_mysql_connection_pool).
Dokumentacja nie określa, kiedy tabela live dostaje pierwsze wiersze po restarcie.

**[INFERENCE]** Na `grp2`, który nie trzyma VIP-a i nie widzi ruchu aplikacji,
tabela live miała komplet trzech wierszy hg 890, w tym wiersz o
`ConnOK=0, Queries=0`; obserwacja dowodzi działania bez ruchu, ale nie pokrywa
chwili zimnego startu. Restart wspólnego ProxySQL wyłącznie dla tego pomiaru
byłby nieuzasadnionym zakłóceniem. Jeśli tabela jest pusta, strażnik pozostaje
bezpieczny: odrzuca backup (`found 0`), zamiast ryzykować backup z writera.

---

## P1-4. Domyślna trwałość jest ustawiona w złą stronę — ZROBIONE

**Problem.** `roles/mariadb_install/templates/server.cnf.j2` renderuje
`innodb_flush_log_at_trx_commit` z `| default(0)`. Schemat nie wymaga tego klucza,
żaden walidator nie wiąże go z `cluster.environment`. Produkcyjny `cluster.yml`,
który pominie parametr, dostaje po cichu wariant mniej trwały.

Dokumentacja MariaDB: `innodb_flush_log_at_trx_commit=1` (z `sync_binlog=1`) jest
warunkiem trwałości ACID; `0` oznacza flush mniej więcej raz na sekundę i utratę
ostatniej sekundy transakcji przy crashu
(https://mariadb.com/docs/server/server-management/server-monitoring-logs/binary-log/group-commit-for-the-binary-log).

**Zmiana.** Domyślna wartość szablonu `1`. `laboratory` może jawnie ustawić `0`.
Walidator odrzuca `profile: production` z wartością inną niż `1`, chyba że
`cluster.yml` zawiera jawny rekord akceptacji ryzyka.

**Akceptacja.** Test jednostkowy renderujący szablon bez klucza → `1`. Walidator:
produkcyjny config z `0` bez akceptacji ryzyka → FAIL.

**Koszt:** mały.

**Wynik (`2996474`, `4aeae5f`).** Szablon domyślnie renderuje `1`. Wszystkie
sześć repozytoryjnych konfiguracji laboratoryjnych zachowuje jawne `0`; dwa
kontenerowe (`lab-cluster`, `lab2-cluster`) dostały brakujące explicit opt-out,
żeby converge nie zmienił ich zachowania. Schemat ogranicza wartości do `0/1/2`
i dodaje boolean `durability_risk_accepted`. Walidator odrzuca produkcyjne `0/2`
bez jawnej akceptacji, a pominięcie klucza pozostaje bezpieczne.

---

## P1-5. Rotacja globalnego poświadczenia monitora nie ma atomowej procedury — ZROBIONE

**Problem.** `mysql-monitor_username`/`password` są globalne dla instancji
ProxySQL (`platform_proxysql.yml:209-237`), a konto backendu tworzy każdy najemca
osobno (`f7_proxysql.yml:38-46`). Nie istnieje cel, który zmienia obie strony
razem. Każda kolejność zostawia okno, w którym ProxySQL shunuje zdrowe backendy.

**Zmiana.** Fleet-level workflow expand → switch → contract:
1. we wszystkich najemcach powstaje konto `*_v2` z nowym hasłem,
2. weryfikacja logowania do każdego backendu,
3. przełączenie globalnej pary w ProxySQL,
4. weryfikacja monitoringu wszystkich najemców,
5. usunięcie `*_v1`.

Wejście: iteracja po `clusters/*`, nie pojedynczy `CLUSTER=`.

**Akceptacja.** Test kolejności kroków na grafie zadań plus sonda: po każdym
kroku żaden backend nie jest `SHUNNED`/`OFFLINE`.

**Koszt:** średni.

**Wynik.** `make platform-monitor-rotate` wykonuje fleet-wide
`expand -> switch -> contract`. `expand` tworzy bezczynną tożsamość na każdym
najemcy i potwierdza logowanie na każdym backendzie. `switch` dodatkowo pyta
sam ProxySQL o wszystkie zarejestrowane backendy (bramka na niepełne `TENANTS`),
przełącza parę globalną, a następnie wymaga świeżych sukcesów bez
`connect_error` w `monitor.mysql_server_connect_log`. `contract` usuwa wyłącznie
tożsamość bezczynną — aktywna jest nietykalna.

**Defekty złapane live.**
1. Bramka „brak SHUNNED” była niemożliwa: przy `max_writers=1` zdrowe
   nie-writery są SHUNNED w writer hostgroup. Zastąpiona dowodem logowania
   monitora z oficjalnej tabeli `monitor.mysql_server_connect_log`.
2. `serial: 1` sprawiał, że każdy ProxySQL liczył cel osobno; para rozeszła się
   (`grp1 b->a`, `grp2 a->b`), a contract usunął konto używane przez grp2.
   Dokumentacja Ansible potwierdza: `run_once` z `serial` działa raz **na batch**.
   Teraz decyzja zapada raz (`run_once`, bez `serial`) i jest wspólna dla pary.
3. Zwykły `platform-proxysql` cofał nazwę monitora do hardkodowanej wartości.
   Interfejs sekretów obejmuje teraz parę `PROXYSQL_MONITOR_USER` +
   `PROXYSQL_MONITOR_PASSWORD`.

**Dowód green (2026-08-24).** Oba węzły przeszły zgodnie
`proxysql_monitor -> proxysql_monitor_b`, trzy backendy potwierdzone na każdym,
zero błędów logowania, stare konto usunięte. Ponowny `platform-proxysql`:
`changed=0`, tożsamość nie cofnięta.


---

## P2-6. `cluster-build` nie jest wznawialny — ZROBIONE

**Problem.** `cluster-build` zawsze zaczyna od `cluster-validate`, którego
`playbooks/f2_preflight.yml` wymaga hosta dziewiczego: brak pakietów MariaDB,
brak datadir, brak `mariadbd`. Awaria po F2 blokuje ponowne uruchomienie
komunikatem „host nie jest czysty".

**Zmiana.** Rozdzielenie preflightu na `fresh` i `converge`; `cluster-build`
wybiera wariant po stanie hosta, nie po intencji operatora:

```
brak MariaDB        -> instalacja
MariaDB obecna      -> weryfikacja przypiętych wersji
datadir pusty       -> inicjalizacja
datadir istnieje    -> weryfikacja tożsamości, nigdy wipe
Primary istnieje    -> brak bootstrapu
```

**Akceptacja.** Test: przerwanie po F2 i ponowny `cluster-build` kończy się
sukcesem bez ręcznej interwencji.

**Koszt:** średni.

**Wynik.** F2 wybiera tryb ze stanu hosta, nie z flagi operatora:

- całkowicie czysty (`pakiety=0`, brak datadir, proces STOPPED) -> `fresh`,
- każdy stan częściowy lub wdrożony -> `converge`.

`converge` jest read-only i fail-closed: wersja `MariaDB-server` musi być
dokładnie z lockfile, a istniejący datadir wymaga `/etc/my.cnf.d/server.cnf`
z `wsrep_cluster_name == galera.cluster_name`. Sam datadir nie jest dowodem
tożsamości; obcy klaster kończy się „Odmowa bez wipe”. Stan „pakiet jest,
datadir jeszcze nie” jest legalnym przerwaniem po instalacji i może być
dokończony.

Dokumentacja MariaDB potwierdza, że `wsrep_cluster_name` jest unikalną logiczną
nazwą wspólną dla wszystkich węzłów. Wdrożony green-r9 przeszedł
`cluster-validate` (`changed=0`); stary kod odrzucał dokładnie ten przypadek
jako „host nie jest czysty”.

Pełne kryterium akceptacji potwierdzone na żywym green-r9: drugi
`make cluster-build CLUSTER=green-r9 CONFIRM=yes` przeszedł od validate do
bramki końcowej. Pierwszy przebieg ujawnił drugi brak planu: preflight już
przechodził, ale bezpośredni bootstrap odmawiał żywemu Primary. Tylko graf
`cluster-build` przekazuje `bootstrap_skip_existing_primary=true`; playbook
wykrywa Primary i kończy krok `end_play`. Bezpośredni `cluster-bootstrap`
nadal fail-closed odmawia (anti split-brain). Dowód końcowy: backup
`galera-green-r9-20260824-164040`, restore 6042 wierszy, bramka 13/13 PASS.

---

## P2-7. Statyczny scheduler backupu po failoverze może zostać writerem — FUNKCJONALNIE ZROBIONE; BLOKER INTEGRALNOŚCI

**Problem.** `backup.scheduler.host` jest przypięty do konkretnego węzła, a guard
`assert_scheduler_is_not_writer` (fail-closed, `E_WRITER`) jest poprawny. Po
failoverze na host schedulera każdy kolejny backup pada trwale — bezpiecznie,
ale bez backupu.

**Zmiana.** Wybór donora w momencie startu: zdrowy non-writer wyliczony z
inwentarza i stanu klastra; statyczny host zostaje wyłącznie jako preferencja.

**Akceptacja.** Test: failover na host schedulera → następny zaplanowany backup
nadal PASS.

**Koszt:** średni.

**Wynik.** Runner wybiera donora przy starcie (`elect_backup_donor`), a cron stoi
na każdym węźle Galery. Węzeł niewybrany kończy się `rc=0` ze zdarzeniem
`skipped.not_elected` — to normalny wynik, nie awaria, więc dwa z trzech węzłów
nie produkują fałszywych porażek.

Zbiór kandydatów pochodzi z **backup hostgroup ProxySQL**, nie z osobnego
odpytywania klastra. Dokumentacja `mysql_galera_hostgroups`: do
`backup_writer_hostgroup` trafiają węzły `read_only=0` ponad `max_writers`
(czyli zdrowe, ale nie będące writerem), a niezdrowe idą do `offline_hostgroup`.
Dzięki temu elekcja nie potrzebuje ani uprawnień `SUPER`, ani nowego kanału —
wystarcza konto read-only wprowadzone w P1-3.

`backup.scheduler.host` zostaje **preferencją**: wygrywa, gdy jest zdrowym
nie-writerem. Straźnik `assert_scheduler_is_not_writer` zostaje jako druga,
niezależna warstwa.

**Defekt złapany dopiero na żywo.** Pierwsza wersja przeszła wszystkie testy
jednostkowe, a mimo to backup padał `E_WRITER` w dokładnie tym scenariuszu, dla
którego powstała elekcja: strażnik trzymał w zbiorze tożsamości
`scheduler_system_address`, więc wybrany donor porównywał z writerem adres
CUDZEGO hosta i trafiał. Tożsamość pochodzi teraz z `node_system_address` —
węzła, który faktycznie wykonuje pracę. Regresja ma własny test
(`GuardJudgesExecutorNotPreferenceTests`).

**Dowód (green, 2026-08-24).** Preferencja wskazana na `grg3`, który był
aktywnym writerem:

| Węzeł | Rola w przebiegu | Zdarzenie |
|---|---|---|
| `grg3` (.30) | preferowany, ale writer | `skipped.not_elected` |
| `grg1` (.28) | wybrany donor | `state.success` |
| `grg2` (.29) | kandydat | `skipped.not_elected` |

Artefakt `galera-green-r9-20260824-151333` zweryfikowany (aes-256-cbc, sha256 OK,
off-cluster w S3). Po przywróceniu preferencji bramka po budowie 13/13 PASS.

**BLOKER przed push.** Elekcja wymaga runnera i `secrets.env` na wszystkich
trzech węzłach Galery. Obecna polityka MinIO zawiera `s3:DeleteObject`
(`minio-policy.json.j2`), a runner realnie usuwa obiekty przy retencji
(`storage/s3.py`). W efekcie kompromitacja dowolnego węzła bazy może teraz
skasować historię off-cluster — poufność danych live tego nie łagodzi.
Mitigacja: osobne poświadczenie write/list/read na wszystkich donorach oraz
delete/prune tylko na jednym koordynatorze (albo udokumentowany Object Lock po
weryfikacji dokumentacji MinIO). Do czasu tej separacji P2-7 nie jest gotowe
do wypchnięcia mimo pełnej poprawności funkcjonalnej.

---

## P2-8. Nieograniczone okno startu przy SST — ZROBIONE

**Problem.** `playbooks/f5_join.yml` instaluje drop-in `TimeoutStartSec=infinity`,
a potem wykonuje blokujące `systemd: state=started`. Własny bounded wait
(`retries`/`delay`) jest **za** tym zadaniem, więc zawieszony SST nigdy do niego
nie dociera.

**Zmiana.** Start bez blokowania, jeden jawny właściciel deadline'u, a po jego
przekroczeniu diagnostyka: `systemctl status`, ogon journala, `wsrep_local_state`,
stan transferu SST.

**Akceptacja.** Test zawieszonego SST: playbook kończy się błędem w zadanym oknie
i zwraca komplet diagnostyki.

**Koszt:** mały.

**Wynik.** `f5_join.yml` startuje jednostkę z `no_block: true`, więc bounded wait
przestał być kodem nieosiągalnym i jest JEDYNYM właścicielem deadline'u
(domyślnie 360 × 5 s). Przekroczenie okna wpada w `rescue`, który zbiera
`systemctl status`, ogon journala oraz `wsrep_local_state` + stan katalogu SST,
i dopiero wtedy kończy się błędem z kompletem dowodów.

`TimeoutStartSec=infinity` **zostaje celowo**. MariaDB dokumentuje, że od systemd
236 działa `EXTEND_TIMEOUT_USEC=` i „manual override of TimeoutStartSec is often
unnecessary"; flota jest w tym reżimie (systemd 252, mariadbd 11.4.12 zawiera
15 odwołań do tego protokołu). Mimo to skończony timeout oznaczałby, że systemd
ubija mariadbd w połowie transferu danych, gdyby przedłużanie z jakiegokolwiek
powodu nie zadziałało. Deadline należy do playbooka, który potrafi zebrać
diagnostykę; systemd ma nie przerywać SST.

**Dowód (green, 2026-08-24).** Zatrzymany `grg2`, porty 4567/4444/4568
zablokowane na czas próby, okno skrócone do 3 prób:

```
Uruchom MariaDB ... (start bez blokowania)  -> changed
Czekaj na Synced                            -> FAILED - RETRYING (359 retries left)
Przekrocz okno dołączania                   -> blad po 15 s + diagnostyka
  [systemctl] Active: activating (start) since ... 17s ago
  [wsrep]     ERROR 2002 socket (111); /var/lib/mysql/.sst nie istnieje
```

Linia `FAILED - RETRYING` jest dowodem osiągalności bramki: pod blokującym
startem nie mogłaby wystąpić. Po zdjęciu blokady `make cluster-join` przywrócił
`size=3`, wszystkie węzły `Primary`/`Synced`; bramka po budowie 13/13 PASS.

---

## P2-9. Fail-open przy pustym zbiorze hostów i nieświeży plik stanu recovery — ZROBIONE

**Problem (składowa).** `ansible.cfg` nie ustawia `[inventory] unparsed_is_failed = True`,
więc „no hosts matched" to `rc = 0`. Jednocześnie `Makefile:513-522` nie kasuje
`RECOVER_STATE_FILE` przed przebiegiem, a bramka `test -s` mierzy istnienie pliku,
nie jego świeżość. Złożenie obu daje bootstrap węzła wybranego dla innego stanu
klastra.

**Zmiana.** `unparsed_is_failed = True`; `rm -f` pliku stanu przed playbookiem;
playbook zapisuje `{run_id, timestamp, node}`, a Makefile weryfikuje zgodność
`run_id`.

**Akceptacja.** Test: plik z poprzedniego przebiegu + `run_id` niezgodny → cel
odmawia bootstrapu.

**Koszt:** trywialny.

**Wynik (`48740b1`).** `unparsed_is_failed=True` zamienia brak inventory w rc=1.
Każdy recovery usuwa stary stan i generuje UUID; playbook zapisuje JSON
`{run_id, generated_at, node}`. Verifier wymaga zgodnego run_id, poprawnego
timestampu, bezpiecznej nazwy oraz członkostwa w grupie `galera`, po czym
atomowo powstaje osobny plik z jedyną nazwą konsumowaną przez bootstrap/join.
Stary plaintext, obcy run_id, zły host i częściowy JSON są odrzucane.

---

## P2-10. Brak zadeklarowanej ścieżki produkcyjnej

**Problem.** `profiles/` zawiera wyłącznie placeholder README. `platform-build`
bezwarunkowo wywołuje `platform-infra`, który asertuje
`platform.environment != 'production'`. Repozytorium ma więc inwarianty
produkcyjne, ale nie ma ścieżki produkcyjnej.

**Zmiana — decyzja jawna, jeden z dwóch wariantów:**

- **A.** Powstaje `profiles/production.yml` wymuszający: `versions.policy: locked`,
  TLS full, walidację certyfikatów PMM, TLS dla S3, `innodb_flush_log_at_trx_commit=1`,
  wyłączony chaos, brak maildeva, backup poza hostem, produkcyjne adresy alertów.
- **B.** README i ISA mówią wprost, że fabryka jest referencją lab/staging, a
  „production design requirements" to nie to samo co „production-supported deployment".

**Akceptacja.** Wariant A: walidator odrzuca produkcyjny config łamiący którykolwiek
warunek. Wariant B: brak zdania o produkcji bez kwalifikatora w README i ISA.

**Koszt:** A średni, B trywialny.

---

## P3. Higiena jednym przebiegiem

Pozycje tanie, bez ryzyka, do zrobienia razem:

Statusy zweryfikowane na kodzie 2026-08-24.

| Pozycja | Status | Dowód |
|---|---|---|
| `cluster_guard` na celach weryfikacyjnych | otwarte | 29 celów dotyka `CLUSTER` bez guardu, w tym destrukcyjne `lab-failover-hard-test`, `lab-split-brain-test` |
| Strażniki sekretów na początku bramki, nie po 12 sondach | otwarte | `PMM_ADMIN_PASSWORD` sprawdzany w 13. linii recepty `lab-post-build-gate`; `APP_DB_PASSWORD` bez guardu |
| `probe-zero-hardcode.py` skanuje też `Makefile` | otwarte | `SCAN_DIRS = ["playbooks", "roles"]` |
| Usunięcie zgniłych komentarzy (`n16g2/n16g3`, `proxysql-3.0.9`) | otwarte | `Makefile:378`, `versions/versions.lock.yml:42` |
| 6 śledzonych `.DS_Store` | **zrobione** (`277a587`) | `.gitignore:53` już je ignorował; zostały w indeksie z przeszłości |
| Rola bez `tasks/` to cichy no-op, nie błąd | otwarte | `roles: mariadb_install` -> `rc=0`, zero zadań |
| `galera-rebuild` w `.PHONY` | otwarte | jest tylko komentarz `Makefile:5` i definicja `:52` |
| `BUILD_SKIP`: sprzężenie seed→backup egzekwowane, nie komentowane | otwarte | sprzężenie nadal wyłącznie w komentarzu `Makefile:135-141` |
| Trzeci stan w tablicy ISC (`PASS-z-zastrzeżeniem`) | otwarte | `grep` po `ISA.md` nie znajduje takiego stanu |
| Aktualizacja ISA: Out of Scope kontra `infra-provision` | otwarte | `ISA.md:30` nadal wyklucza „tworzenie VM" |
| Piny wersji w krokach `pip install` w CI | otwarte | trzy niepinowane instalacje: `ci.yml:31,170,214` |
| Decyzja o `LICENSE` i o publiczności mapy sieci | otwarte | brak pliku `LICENSE`; `docs/infrastructure-state.md` |

---

## Czego ten plan celowo nie robi

- **Nie przepisuje playbooków na role.** Zysk mały, ryzyko duże. Z trzech
  „pustych szkieletów" `roles/preflight` i `roles/proxysql_install` w ogóle nie
  istnieją w repozytorium — git nie wersjonuje pustych katalogów, więc były
  wyłącznie lokalnym osadem po `ansible-galaxy init`. `roles/mariadb_install`
  zostaje: trzyma używany `templates/server.cnf.j2`.
- **Nie przenosi orkiestracji z Makefile do CLI w Pythonie.** Makefile zostaje
  jako interfejs operatora; do danych przenosimy wyłącznie graf zależności F0–F15.
- **Nie zmienia nazw `F0`–`F15`.** Numery są opisanym protokołem; aliasy podwoiłyby
  nazewnictwo.
- **Nie dodaje anti-affinity jako bramki.** Lab ma jeden węzeł PVE
  (`terraform/modules/pve_vm_set/variables.tf:39-42`, `node_name = "pve"`), więc
  sonda byłaby permanentnie czerwona. Zapisujemy to jako świadomie przyjęte ryzyko
  i włączamy bramkę dopiero przy drugim węźle hypervisora.

## Osobno: dług testowy, który to umożliwił

Ścieżka `not use_systemd` żyje w siedmiu playbookach (`bootstrap.yml:15`,
`site.yml:19`, `f5_join.yml:156`, `f12_rolling_restart.yml:80,144`,
`f13_remove_node.yml:241`, `cluster_recover.yml:102`). Testowana jest ścieżka,
której produkcja nie używa, i to w operacjach niszczących. Usunięcie tej gałęzi
jest warunkiem, żeby kolejne bramki cokolwiek dowodziły — planowane po P0.
