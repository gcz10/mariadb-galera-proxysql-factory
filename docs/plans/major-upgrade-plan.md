# Plan major upgrade: MariaDB 11.4.13 → 12.3 LTS

_Wygenerowany przez `f12_upgrade_plan.yml` (ISC-53/54/56). Read-only — ten plik
jest planem, nie wykonaniem. Źródło: oficjalna dokumentacja MariaDB/Galera._

## Ścieżka (ISC-54 — oficjalna dokumentacja)
- **Obecna wersja:** MariaDB 11.4.13 (Galera 4, wsrep API 26)
- **Cel:** MariaDB 12.3 LTS (EOL 2029-06-12)
- **Etapy:** 11.4 → 11.8 → 12.3. Rolling upgrade Galery idzie o jeden LTS
  na raz: "You must upgrade one major/LTS version at a time. (...) skipping versions
  during a rolling upgrade is not supported". Przystanek nie jest celem, więc jego
  wcześniejszy EOL nie blokuje planu.
- **Metoda:** rolling in-place na każdym etapie, bez dump/restore. Pominięcie etapu
  jest wspierane wyłącznie przy zatrzymaniu CAŁEGO klastra — tego ta procedura nie robi.
- **Źródła:**
  - https://mariadb.com/docs/server/server-management/install-and-upgrade-mariadb/upgrading/mariadb-community-server-upgrade-paths/general-upgrade-information
  - https://mariadb.com/docs/galera-cluster/galera-management/upgrading-galera-cluster/
  - https://mariadb.com/kb/en/downgrading-between-major-mariadb-versions/

## Warunki wstępne
1. **Pełny backup** mariadb-backup PRZED upgrade (ISC-32 — off-cluster, szyfrowany).
2. **gcache.size** wystarczające dla IST (obecnie: 4G), aby węzły
   powracały przez IST, nie pełne SST.
3. **Brak DDL** i nowych funkcji serii docelowej, dopóki ostatni etap nie obejmie
   wszystkich węzłów.
4. `galera-4` tego samego wydania na każdym etapie (wsrep API 26).

## Etap 1: 11.4 → 11.8
1. `versions.lock_file` w `clusters/<klaster>/cluster.yml` → `versions/versions-el9-118.lock.yml`.
2. Węzeł po węźle, non-writery najpierw, writer ostatni:
   `make cluster-upgrade-node CLUSTER=<klaster> target_node=<węzeł> old_mariadb_version=<wersja na węźle>`.
   Playbook odmawia przed drainem, gdy wersja na węźle różni się od podanej, gdy krok
   nie jest jednym LTS albo gdy w klastrze jest już trzecia seria.
- Zmiany przy wejściu na 11.8: Domyślne innodb_snapshot_isolation=ON (zmierzone: OFF na 11.4.13, ON na 12.3.3) — aplikacja w REPEATABLE READ może dostać ERROR 1020 przy współbieżnych UPDATE/DELETE. Usunięte wsrep_load_data_splitting.
- Etap kończy się, gdy WSZYSTKIE węzły raportują 11.8; następny etap dopiero wtedy.
- Host odtwarzania: `make cluster-restore-drill` wymienia `MariaDB-backup` na serię etapu.

## Etap 2: 11.8 → 12.3
1. `versions.lock_file` w `clusters/<klaster>/cluster.yml` → `versions/versions-el9-123.lock.yml`.
2. Węzeł po węźle, non-writery najpierw, writer ostatni:
   `make cluster-upgrade-node CLUSTER=<klaster> target_node=<węzeł> old_mariadb_version=<wersja na węźle>`.
   Playbook odmawia przed drainem, gdy wersja na węźle różni się od podanej, gdy krok
   nie jest jednym LTS albo gdy w klastrze jest już trzecia seria.
3. Zmiana rodziny 11.x → 12.x: pakiet serwera jest usuwany i instalowany
   od nowa (scriptlet odmawia `dnf upgrade`); playbook zachowuje `server.cnf`.
- Zmiany przy wejściu na 12.3: MariaDB-server nie zależy już od Galery: MariaDB-server-galera instaluje się jawnie (lockfile server_galera_package). Usunięte big_tables, storage_engine, large_page_size; nowe słowa zastrzeżone CONVERSION i TO_DATE; MYSQLD_OPTS dla systemd przestarzałe.
- Etap kończy się, gdy WSZYSTKIE węzły raportują 12.3; następny etap dopiero wtedy.
- Host odtwarzania: `make cluster-restore-drill` wymienia `MariaDB-backup` na serię etapu.

## Na każdym węźle (wykonuje `cluster_upgrade_node.yml`)
1. Drain węzła na każdym ProxySQL (`OFFLINE_SOFT`, `ConnUsed=0`).
2. `innodb_fast_shutdown=0`, `systemctl stop mariadb`.
3. Repo na wersję z lockfile'a etapu, instalacja przypiętych pakietów.
4. `systemctl start mariadb` — węzeł powraca przez IST (jeśli gcache wystarczy).
5. **`mariadb-upgrade --skip-write-binlog`** — aktualizuje tabele systemowe LOKALNIE,
   bez replikacji DDL do nie-uaktualnionych węzłów (`--skip-write-binlog` kluczowe).
6. Brama zdrowia: `wsrep_local_state=4`, `Primary`, `wsrep_ready=ON`, pełny size.
7. Undrain na każdym ProxySQL, potem kolejny węzeł.

## ISC-55: stop przy utracie zdrowia
Po każdym węźle brama zdrowia (wsrep_local_state=4 + Primary + size). Jeśli węzeł
nie odzyska zdrowia → STOP, nie kontynuuj rolling (klaster może utracić quorum).

## ISC-56: ANTI — rollback NIE downgraduje datadir
MariaDB nie wspiera downgrade datadir między major wersjami:
> "Data files modified by a higher major version or mariadb-upgrade
>  are forward-incompatible." (mariadb.com/kb/en/downgrading-between-major-mariadb-versions)
**Rollback = odtworzenie datadir z backupu (mariadb-backup), NIE downgrade pakietów.**
Przed upgrade ustaw `innodb_fast_shutdown=0` (czysty flush) dla awaryjnego rollbacku.

## Weryfikacja po ostatnim etapie
- Wszystkie węzły: `SELECT VERSION();` == 12.3
- `wsrep_cluster_status=Primary`, `wsrep_cluster_size=3`
- `mariadb-upgrade --skip-write-binlog` raportuje OK na każdym węźle
- `CHECK TABLE` na tabelach użytkownika (integralność)
- Backup + restore drill po upgrade (ISC-36)
- `proxysql.server_version` w `platform.yml` = najstarszy najemca po upgrade
