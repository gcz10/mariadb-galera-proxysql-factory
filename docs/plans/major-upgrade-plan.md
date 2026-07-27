# Plan major upgrade: MariaDB 11.4.12 → 12.3 LTS

_Wygenerowany przez `f12_upgrade_plan.yml` (ISC-53/54/56). Read-only — ten plik
jest planem, nie wykonaniem. Źródło: oficjalna dokumentacja MariaDB/Galera._

## Ścieżka (ISC-54 — oficjalna dokumentacja)
- **Obecna wersja:** MariaDB 11.4.12 (Galera 4, wsrep API 26)
- **Cel:** MariaDB 12.3 LTS (EOL 2029-06-30)
- **Metoda:** rolling in-place upgrade (Galera 4 wspiera 10.11/11.4/11.8, brak dump/restore)
- **Źródła:**
  - https://mariadb.com/kb/en/upgrading-galera-cluster/
  - https://mariadb.com/kb/en/upgrading-between-major-mariadb-versions/
  - https://galeracluster.com/library/documentation/upgrading.html

## Warunki wstępne
1. **Pełny backup** mariadb-backup PRZED upgrade (ISC-32 — off-cluster, szyfrowany).
2. **gcache.size** wystarczające dla IST (obecnie: 256M); zalecane >=2G na czas upgrade,
   aby węzły powracały przez IST, nie pełne SST.
3. **Brak DDL** w klastrze podczas rolling upgrade.
4. Wersja Galera 4 zgodna między węzłami (wsrep API 26).

## Procedura rolling (jeden węzeł naraz, non-writer pierwszy, writer ostatni)
1. Drain węzła w ProxySQL (`status='OFFLINE_SOFT'` w mysql_servers).
2. `systemctl stop mariadb`.
3. Aktualizuj repo → swap pakietów (MariaDB-server, MariaDB-client, galera-4).
   Uwaga: dla 12.3+ zainstaluj jawnie `mariadb-server-galera`.
4. Usuń zdeprecjonowane zmienne z server.cnf.
5. `systemctl start mariadb` — węzeł powraca przez IST (jeśli gcache wystarczy).
6. **`mariadb-upgrade --skip-write-binlog`** — aktualizuje tabele systemowe LOKALNIE,
   bez replikacji DDL do nie-uaktualnionych węzłów (`--skip-write-binlog` kluczowe).
7. Weryfikuj zdrowie: `wsrep_local_state=4`, `Primary`, `wsrep_ready=ON`, pełny size.
8. Re-enble traffic w ProxySQL, powtórz dla kolejnego węzła.

## ISC-55: stop przy utracie zdrowia
Po każdym węźle brama zdrowia (wsrep_local_state=4 + Primary + size). Jeśli węzeł
nie odzyska zdrowia → STOP, nie kontynuuj rolling (klaster może utracić quorum).

## ISC-56: ANTI — rollback NIE downgraduje datadir
MariaDB nie wspiera downgrade datadir między major wersjami:
> "Data files modified by a higher major version or mariadb-upgrade
>  are forward-incompatible." (mariadb.com/kb/en/downgrading-between-major-mariadb-versions)
**Rollback = odtworzenie datadir z backupu (mariadb-backup), NIE downgrade pakietów.**
Przed upgrade ustaw `innodb_fast_shutdown=0` (czysty flush) dla awaryjnego rollbacku.

## Weryfikacja po upgrade
- Wszystkie węzły: `SELECT VERSION();` == 12.3
- `wsrep_cluster_status=Primary`, `wsrep_cluster_size=3`
- `mariadb-upgrade --skip-write-binlog` raportuje OK na każdym węźle
- `CHECK TABLE` na tabelach użytkownika (integralność)
- Backup + restore drill po upgrade (ISC-36)
