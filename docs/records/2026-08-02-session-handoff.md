# Zapis sesji — 2026-08-02: odbudowa floty i migracja monitoringu

Dokument przekazania. Opisuje **co zrobiono, co działa, i co jest niedokończone**
— z dokładnością pozwalającą podjąć pracę bez tej rozmowy.

Ostatni commit sesji: `f3db5a1`. Working tree czysty, wszystko wypchniete.

## Przebieg

1. **Teardown starej floty** — 18 VM skasowanych (bramka: zamknięta lista VMID
   + asercja przynależności do puli `claude-isa`). Zwolniło 325 GB rezerwacji
   `data1` (98.23% → 62.99%). Rekord sprzed: `docs/records/2026-08-02-pre-teardown.md`.
2. **Nowa flota `finalclaude`** postawiona od zera z kodu — warstwa wspólna
   + dwa klastry.
3. **Wielodostęp ProxySQL** — jedna para HA obsługuje oba klastry.
4. **Migracja monitoringu na `pmm-client`** — częściowa, celowo (porównanie).
5. **QAN** — naprawiony (nigdy nie działał), potem przełączony na slowlog na r9.

## Stan floty

**11 VM, wszystkie w puli `claude-isa`.**

| Moduł Terraform | Hosty | VMID | IP |
|---|---|---|---|
| `terraform/shared/` | `fcinfra` (PMM+MinIO+maildev), `fcp1`, `fcp2` (ProxySQL HA) | 9400-9402 | .130-.132, VIP **.133** |
| `terraform/finalclaude-r10/` | `f10g1-3` (galera), `f10r1` (restore) | 9410-9413 | .140-.143 |
| `terraform/finalclaude-r9/` | `f9g1-3` (galera), `f9r1` (restore) | 9420-9423 | .150-.153 |

### Kluczowe różnice między klastrami (celowe — to jest porównanie)

| | `finalclaude-r10` | `finalclaude-r9` |
|---|---|---|
| platforma | Rocky 10 | Rocky 9 |
| własność warstwy wspólnej | **owner** | consumer |
| hostgroupy ProxySQL | 10/20/30/40 | **110**/120/130/140 |
| użytkownik aplikacyjny | `app_user` | `app_user_fc9` |
| model monitoringu Galery | **agentless** (PMM zdalnie) | **pmm-client** (lokalny agent) |
| monitoring ProxySQL | pmm-client (owner instaluje) | — (należy do ownera) |
| źródło QAN | `perfschema` | **`slowlog`**, próg 100 ms |

Rozdział ruchu idzie **po użytkowniku, nie po porcie** — oba klastry dzielą
`VIP:6033`, a o trafieniu decyduje konto. Dowiedzione: `app_user` → `fc10_galera`,
`app_user_fc9` → `fc9_galera`.

## Pięć defektów, które ujawniła dopiero budowa od zera

Wszystkie miały **ten sam kształt**: coś było zarejestrowane i „RUNNING", ale nie
produkowało danych — a sonda sprawdzała istnienie zamiast zachowania.

| defekt | objaw | commit |
|---|---|---|
| `svcaccs: null` na świeżym MinIO | pierwszy backup padał na każdej nowej instalacji | `7e1429e` |
| zaszyty `rnode1` w `probe-restore` | „cannot parse restore state" wskazujące w złe miejsce | `7e1429e` |
| 6 vs 8 reguł alertowych | poprawny klaster oblewał weryfikację | `7e1429e` |
| alert `no-writer` filtrowany po `cluster` | palił się fałszywie na konsumencie wspólnej pary | `b3f57e4` |
| **`performance_schema=OFF`** | QAN pusty **na całej flocie, od zawsze** | `f820c61` |

Do tego dwie ciche utraty przy samej migracji:
- **metryki backupu** — runner pisał do `/var/lib/node_exporter/textfile_collector`,
  a po zatrzymaniu tarballowego eksportera nikt tego nie czytał. Naprawione
  symlinkiem **katalogu** (nie plików — runner podmienia je atomowym rename).
- **etykiety `cluster`** — węzły z `pmm-agent setup` ich nie miały, więc znikały
  z dashboardów. API nie pozwala dodać ich później (501); muszą iść przy
  rejestracji przez `PMM_AGENT_SETUP_CUSTOM_LABELS`.

## Pomiary (nie szacunki)

| co | wartość |
|---|---|
| `performance_schema` | **105 MB/węzeł** (nie 200-400, jak najpierw podałem) |
| slow log przy `long_query_time=0` | **276 B/zapytanie** → ~1 GB/h **na węzeł** przy 1000 q/s |
| slow log przy progu 0.1 s | **0 B** dla tego samego benchmarku |
| koszt progu | QAN widzi 2 kształty zapytań zamiast 31 (okno 6 min) |
| dashboard ProxySQL | 0/40 → **37/40** metryk po `pmm-client` |

**Tryb `push` jest obowiązkowy.** Domyślka `pmm-admin` to `auto`, które tutaj
wybiera `pull`, a pull wymaga otwarcia 42000-51999 przychodząco — czego minimalna
polityka firewalld (ISC-5) nie dopuszcza. Objaw: eksporter `RUNNING`, `up=0`.

## Nowe elementy konfiguracji

| pole | znaczenie |
|---|---|
| `proxysql.hostgroup_base` | baza ID hostgroup; rozłączna na wspólnej parze |
| `proxysql.app_user` | użytkownik aplikacyjny; rozłączny na wspólnej parze |
| `proxysql.role` | `owner` \| `consumer` — kto zarządza węzłami wspólnymi |
| `monitoring.agent_groups` | grupy hostów z lokalnym `pmm-client` |
| `monitoring.qan_source` | `perfschema` \| `slowlog` |
| `mariadb_tuning.performance_schema` | domyślnie `ON` |
| `mariadb_tuning.slow_query_log` / `long_query_time` | źródło QAN typu slowlog |

Nowa sonda statyczna: `tests/validation/probe-proxysql-tenancy.py` — pilnuje, by
klastry na wspólnym endpoincie miały rozłączne hostgroupy i użytkowników oraz
**dokładnie jednego ownera**. Wpięta w Makefile i CI.

## Kontrakt dwutrybowy sondy — domknięty

`make lab-monitoring-verify` przechodzi dla obu klastrów, a `make cluster-monitoring`
jest idempotentny i nie psuje tego stanu (sprawdzone powtórzonymi przebiegami).
Rotacja poświadczeń przećwiczona **end-to-end na obu ścieżkach**.

Dojście tam ujawniło pięć defektów tej samej klasy co reszta sesji — coś
raportowało poprawność, nie robiąc swojej roboty:

| defekt | skutek |
|---|---|
| `pkill -x mysqld_exporter` w sprzątaniu standalone | ubijał eksporter prowadzony przez `pmm-agent` **przy każdym przebiegu** |
| uzgadnianie etykiet przez `PUT` | PMM nadpisuje cały obiekt — ginął tryb push, potem hasło |
| `pmm-admin status` bez limitu czasu | zaklinowane API agenta wieszało cały playbook bez komunikatu |
| pętla oczekiwania na metryki przerywana `break` | jedna realna usterka produkowała ~15 widmowych „brak metryki" |
| templatowany klucz YAML w ciele POST agenta QAN | zadanie **nigdy niczego nie utworzyło** — PMM dostawał `invalid agent type <nil>` |

Ten ostatni jest najbardziej pouczający: zadanie wyglądało na sprawne, bo jego
`when` trafiał na agentów utworzonych **ręcznie przez API** i pomijał się. Wyszło
dopiero, gdy kasowanie przy rotacji zmusiło je do faktycznej pracy.

Wnioski warte zapamiętania:
- **`PUT` na `/v1/inventory/agents` nadpisuje cały obiekt.** Dryf na ścieżce
  agentowej naprawiamy przez delete + recreate jedną ścieżką POST. Ścieżka
  agentless może zostać przy `PUT`, bo jej ciało zawsze niesie user+hasło —
  to była jedyna różnica, nie sam czasownik HTTP.
- API **przyjmuje** `push_metrics`, a **zwraca** `push_metrics_enabled`.
- **Templatowany klucz YAML w ciele `uri` nie rozwiązuje się.** Gdy typ obiektu
  jest zmienny, całe ciało musi być jednym wyrażeniem Jinja. Ta pułapka wystąpiła
  w tym pliku **dwa razy**.
- Sufiks rozdzielczości (`hr`/`mr`/`lr`) różni się per metryka **i** per tryb, więc
  każdy filtr po `job` gubi jedną stronę — agregujemy po `node_name`.
- `node_exporter` w trybie agentowym to **agent**, nie usługa; ProxySQL ma
  natywny eksporter zamiast restapi na 6070; MySQL jest osiągane przez pętlę zwrotną.
- **`credentials_revision` obu klastrów wynosi teraz `2`** — to baza po testach
  rotacji. Liczba jest dowolna, znaczenie ma wyłącznie zgodność konfiguracji z etykietą.

## Reguły przyjęte w tej sesji

- **Pula `claude-isa` to asercja, nigdy źródło listy do skasowania.** „To jest
  nasze" i „to wolno skasować" to nie to samo zdanie.
- **Nie ruszamy `9010-9012`, `9040-9042`, `9050`, `9060`** — poprzednicy sprzed
  tej automatyzacji, poza pulą.
- **Weryfikuj zachowanie, nie rejestrację.** Pięć defektów tej sesji to jeden
  wzorzec: agent `RUNNING` bez danych, reguła istniejąca ale paląca się, metryka
  pisana ale nieczytana.
- **Mierz, nie szacuj.** Siedem twierdzen podanych w tej sesji jako fakt okazalo
  sie blednych przy sprawdzeniu: lokalizacja bledu z review, zalecenie push mode,
  liczba VM, koszt RAM perfschemy, "wyscig przy kasowaniu", wina ControlMasterow
  i hipoteza o `changed_when`. Za kazdym razem rozstrzygal pomiar, nie rozumowanie.
- **Test musi odwzorowywac to, co robi kod.** Reczny POST z ZASZYTYM kluczem
  przechodzil i "dowodzil", ze cialo jest poprawne — playbook wysylal klucz
  templatowany i dostawal 400. Sprawdzalem cos innego niz to, co dziala.
- **Sciezka, ktorej nikt nie uruchomil, nie dziala.** Delete+recreate istnieje
  wylacznie dla rotacji; do czasu jej przecwiczenia byl to kod nieprzetestowany,
  ktory przy pierwszym uzyciu skasowal wszystkie agenty QAN i ich nie odtworzyl.
