# Plan: uszczelnienie granic własności i bezpieczników

**Status:** W TOKU — utworzony 2026-08-23 na bazie czterech zewnętrznych recenzji.
**Zrobione:** P0-1 (`85ff115`), P0-2 (`2d7df6d`), P1-3, P1-4 (`2996474`, `4aeae5f`), P2-9 (`48740b1`), P3 (higiena, komplet).
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

## P1-5. Rotacja globalnego poświadczenia monitora nie ma atomowej procedury

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

---

## P2-6. `cluster-build` nie jest wznawialny

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

---

## P2-7. Statyczny scheduler backupu po failoverze może zostać writerem

**Problem.** `backup.scheduler.host` jest przypięty do konkretnego węzła, a guard
`assert_scheduler_is_not_writer` (fail-closed, `E_WRITER`) jest poprawny. Po
failoverze na host schedulera każdy kolejny backup pada trwale — bezpiecznie,
ale bez backupu.

**Zmiana.** Wybór donora w momencie startu: zdrowy non-writer wyliczony z
inwentarza i stanu klastra; statyczny host zostaje wyłącznie jako preferencja.

**Akceptacja.** Test: failover na host schedulera → następny zaplanowany backup
nadal PASS.

**Koszt:** średni.

---

## P2-8. Nieograniczone okno startu przy SST

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
