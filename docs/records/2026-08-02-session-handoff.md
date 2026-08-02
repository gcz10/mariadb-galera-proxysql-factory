# Zapis sesji — 2026-08-02: odbudowa floty i migracja monitoringu

Dokument przekazania. Opisuje **co zrobiono, co działa, i co jest niedokończone**
— z dokładnością pozwalającą podjąć pracę bez tej rozmowy.

Ostatni commit sesji: `52f8196`. Working tree czysty.

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

## NIEDOKOŃCZONE — `probe-pmm-native.py`

`make lab-monitoring-verify` **oblewa dla obu klastrów**. Dla `r10` to częściowo
regresja z tej sesji.

Sonda zna wyłącznie kształt agentless. Kontrakt dwutrybowy jest zrobiony w ~40%:
gotowe są zbiór węzłów (wspólny dla trybów), `AGENTLESS_HOSTS` jako podzbiór dla
usług `external`, nazwy usług MySQL zależne od trybu i typ agenta QAN z
`monitoring.qan_source`.

Zostały **dwie grupy**, obie tej samej natury — dla hostów `pmm-client` sonda
musi asertować **agentów pod lokalnym `pmm_agent_id`**, a nie usługi i agentów
spod agenta serwera:

1. **Węzły i eksportery**
   - `missing generic node: fc10-galera-fcp1` — węzeł istnieje z etykietami;
     sonda filtruje po atrybucie, który `pmm-agent` ustawia inaczej
   - `missing native node-exporter service: <host>-node-exporter` —
     **`pmm-agent` nie tworzy usługi o tej nazwie**; `node_exporter` jest u niego
     *agentem*, nie usługą
2. **Atrybuty eksporterów**
   - `targets wrong address or port` — lokalny agent celuje w `127.0.0.1`,
     sonda oczekuje adresu węzła
   - `wrong credential revision` — etykietę `credentials_revision` ustawia tylko
     ścieżka API (`f11_pmm_client`)

Dodatkowo: `f11_pmm_agent.yml` nie jest w pełni idempotentny (`changed=1` przy
powtórce), choć `make cluster-monitoring` kończy się `rc=0` na obu klastrach.

## Reguły przyjęte w tej sesji

- **Pula `claude-isa` to asercja, nigdy źródło listy do skasowania.** „To jest
  nasze" i „to wolno skasować" to nie to samo zdanie.
- **Nie ruszamy `9010-9012`, `9040-9042`, `9050`, `9060`** — poprzednicy sprzed
  tej automatyzacji, poza pulą.
- **Weryfikuj zachowanie, nie rejestrację.** Pięć defektów tej sesji to jeden
  wzorzec: agent `RUNNING` bez danych, reguła istniejąca ale paląca się, metryka
  pisana ale nieczytana.
- **Mierz, nie szacuj.** Cztery twierdzenia podane w tej sesji jako fakt okazały
  się błędne przy sprawdzeniu (lokalizacja błędu z review, zalecenie push mode,
  liczba VM, koszt RAM perfschemy).
