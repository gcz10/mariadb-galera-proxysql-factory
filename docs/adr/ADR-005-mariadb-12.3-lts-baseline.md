# ADR-005: MariaDB 12.3 LTS jako wersja bazowa

**Data:** 2026-10-20
**Status:** Accepted. Zastępuje [ADR-004](ADR-004-mariadb-11.4-lts-selection.md).
**Decydent:** Operator (decyzja 2026-10-20)

## Kontekst

ADR-004 (2026-07-22) wybrał 11.4.12 i odrzucił 12.3 z powodu „mniejszej
dojrzałości jako bieżący baseline". Od tego czasu:

- 12.3.3 jest najnowszym wydaniem serii (2026-08-24), a wsparcie 12.3 trwa
  dłużej niż 11.4 (tabela niżej). Teza o krótszym oknie 12.3 z
  `versions/compatibility-report.md` była błędna już w chwili zapisu.
- Najemca Orion działa na 12.3.3 od 2026-09-10: upgrade 11.4.13 → 11.8.9 →
  12.3.3 na `orionv16-r10`, potem świeża budowa `orionv17-r10` (Rocky 10).
  Na obu generacjach przeszły testy awarii (failover writera, twarda utrata
  maszyny, split-brain, backup pod obciążeniem), bramka po budowie, drille
  odtworzenia i TLS `full` z `require_secure_transport` (ISA.md, wpisy
  2026-09-10, 2026-09-12, 2026-09-15, 2026-09-20/21, 2026-09-27).
- Host aplikacyjny `x17app` (Rocky 9) ma `MariaDB-client-12.3.3-1.el9`.

## Kandydaci (endoflife.date/api/mariadb.json, odczyt 2026-10-20)

| Seria | Typ | Najnowsza | EOL |
|---|---|---|---|
| **12.3** | LTS | 12.3.3 (2026-08-24) | 2029-06-12 |
| 11.4 | LTS | 11.4.13 (2026-08-24) | 2029-05-29 |
| 11.8 | LTS | 11.8.9 (2026-08-24) | 2028-06-04 |
| 10.11 | LTS | 10.11.19 (2026-08-24) | 2028-02-16 |
| 13.0 | rolling | 13.0.2 (2026-09-14) | 2026-12-31 |

## Decyzja

**MariaDB 12.3 LTS, pin 12.3.3.** Nowy najemca deklaruje:

- Rocky 9: `versions.lock_file: versions/versions-el9-123.lock.yml`
- Rocky 10: `versions.lock_file: versions/versions-el10-123.lock.yml`

Seria bazowa jest zapisana w kodzie jako `mariadb_baseline_series` w
`playbooks/vars/mariadb_upgrade_paths.yml` — to domyślny cel planu
`make cluster-upgrade-plan`.

Najemca na 11.4 przechodzi na 12.3 przez 11.8, po jednym LTS na raz.
Dokumentacja MariaDB dla upgrade'u Galery bez przestoju: „You must upgrade
one major/LTS version at a time" (General Upgrade Information). 11.8 jest
wyłącznie przystankiem: jej wcześniejszy EOL nie blokuje ścieżki, której
celem jest 12.3.

## Uzasadnienie

- Najdłuższe wsparcie spośród dostępnych LTS.
- Zmierzona na flocie, nie tylko opisana: pełny zestaw testów awarii,
  odtworzenia i TLS na dwóch generacjach Oriona.
- Ten sam provider `galera-4` 26.4.27 (wsrep API 26) w 11.4, 11.8 i 12.3,
  więc mieszany klaster na czas rolling upgrade'u jest zgodny protokołem.
- Serie 12.0–12.2 były rolling i już wygasły; 13.0 jest rolling z EOL
  2026-12-31, więc nie nadaje się na bazę.

## Konsekwencje

- **Osobny pakiet Galery.** Od 12.3.2 `MariaDB-server` nie zależy od Galery
  (MDEV-38744, MDEV-30953); `MariaDB-server-galera` trzeba zainstalować
  jawnie, inaczej jednostka systemd nie obsługuje bootstrapu ani SST.
  Lockfile'y 12.3 deklarują `server_galera_package`; `f2_install.yml` i
  `cluster_upgrade_node.yml` instalują go warunkowo.
- **11.x → 12.x to wymiana pakietu, nie `dnf upgrade`.** Scriptlet pakietu
  odmawia przejścia, a `server.cnf` należy do pakietu; playbook zapisuje i
  przywraca konfigurację (ISA.md, 2026-09-10).
- **Izolacja snapshotu.** 11.8 zmienia domyślne `innodb_snapshot_isolation`;
  zmierzone 2026-10-20: `OFF` na 11.4.13 (Cassiopeia), `ON` na 12.3.3
  (Orion), w obu bez wpisu w konfiguracji. Aplikacja pracująca w REPEATABLE
  READ może dostać `ERROR 1020 (Record has changed since last read)` przy
  współbieżnych `UPDATE`/`DELETE` — od pierwszego węzła 11.8.
- **Zmiany niezgodne do sprawdzenia przed upgrade'em:** usunięte w 12.0
  `big_tables`, `storage_engine`, `large_page_size`; w 11.8
  `wsrep_load_data_splitting`; nowe słowa zastrzeżone `CONVERSION` i
  `TO_DATE`; `MYSQLD_OPTS` dla systemd przestarzałe.
- **Handshake ProxySQL.** `proxysql.server_version` w `platform.yml`
  deklaruje najstarszego najemcę; po przejściu ostatniego najemcy 11.4
  wartość zmienia się na `12.3.3-MariaDB`.
- **Hosty odtwarzania** muszą mieć `MariaDB-backup` serii klastra;
  `f10_restore.yml` wymienia pakiet przy zmianie rodziny.
- **Lockfile'y 11.4** (`versions/versions.lock.yml`,
  `versions/versions-el10.lock.yml`) zostają jako pin źródłowy dla
  najemców przed upgrade'em i jako wersja wspierana do 2029-05-29; nie są
  wyborem dla nowych deklaracji.

## Granica dowodu w chwili decyzji

Serwer 12.3 na Rocky 9 nie był jeszcze wdrożony. Repodata
`dlm.mariadb.com/repo/mariadb-server/12.3.3/yum/rhel/9/x86_64` odczytane
2026-10-20 zawiera `MariaDB-server`, `MariaDB-server-galera`,
`MariaDB-backup` i `MariaDB-client` 12.3.3-1.el9 oraz `galera-4`
26.4.27-1.el9.

**Zamknięte tego samego dnia:** `cassiopeiav18-r9` (Rocky 9) przeszła
rolling 11.4.13 → 11.8.9 → 12.3.3 bez utraty transakcji i z odtworzeniem
kopii 12.3.3 na hoście restore; pełny zapis w ISA.md (Verification,
2026-10-20).

## Odrzucone warianty

- **11.4:** wspierana, ale krócej niż 12.3; zostaje tylko jako stan przed
  upgrade'em.
- **11.8:** EOL 2028-06-04; przystanek na ścieżce 11.4 → 12.3, nie cel.
- **10.11:** EOL 2028-02-16.
- **13.0:** seria rolling, EOL 2026-12-31.

## Źródła

- https://endoflife.date/api/mariadb.json
- https://mariadb.com/docs/server/server-management/install-and-upgrade-mariadb/upgrading/mariadb-community-server-upgrade-paths/general-upgrade-information
- https://mariadb.com/docs/galera-cluster/galera-management/upgrading-galera-cluster/upgrading-from-mariadb-10.11-to-mariadb-11.4-with-galera-cluster-2 (strona „Upgrading from MariaDB 11.8 to MariaDB 12.3 with Galera Cluster")
- https://mariadb.com/docs/server/server-management/install-and-upgrade-mariadb/upgrading/mariadb-community-server-upgrade-paths/upgrading-from-mariadb-11-8-to-mariadb-12-3
- https://mariadb.com/docs/server/server-management/install-and-upgrade-mariadb/upgrading/mariadb-community-server-upgrade-paths/upgrading-from-mariadb-11-4-to-mariadb-11-8
- https://mariadb.com/docs/release-notes/community-server/12.3/12.3.3
- https://jira.mariadb.org/browse/MDEV-38744, https://jira.mariadb.org/browse/MDEV-30953
