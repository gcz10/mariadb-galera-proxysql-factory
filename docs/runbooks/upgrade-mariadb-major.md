# Runbook: ręczny major upgrade MariaDB/Galera (na przykładzie 11.4.12 → 11.8.9, orionv10-r10, 2026-08-29)

Procedura wypracowana i wykonana na żywym klastrze. Repo ma **generator planu
(read-only, ISC-53) i rolling patch w obrębie serii (`cluster-patch`), ale NIE ma
playbooka egzekwującego major upgrade** — major wykonuje się ręcznie wg planu z
`make cluster-upgrade-plan`, krok po kroku, z bramami zdrowia po każdym węźle.
Ten dokument jest tym krokiem po kroku.

## 0. Decyzja i ostrzeżenia (przeczytaj przed startem)

- **Rollback NIE istnieje jako downgrade pakietów** (ISC-56): datadir po
  `mariadb-upgrade` jest forward-incompatible. Jedyny rollback = odtworzenie
  datadiru z backupu mariadb-backup. Dlatego backup przed jest kontraktem, nie
  sugestią.
- **Cel krótszy EOL niż obecna seria wymaga świadomej decyzji**: strażnik
  „regresji wsparcia" w `f12_upgrade_plan.yml` odmówi (np. 11.8 EOL 2028-06 <
  11.4 EOL 2029-05). Obejście: `-e f12_allow_eol_regression=true` **+ uzasadnienie
  w ADR** (kontrakt strażnika; przykład: `docs/records/2026-08-29-mariadb-118-upgrade-orion.md`).
- **Mieszanie wersji**: między klastrami — wspierane (per-cluster `lock_file`);
  w obrębie jednego klastra — tylko przejściowo podczas tego rolling upgrade.
  Wszystkie węzły klastra kończą na jednej wersji.
- **Brak DDL** w klastrze przez cały rolling. Rolling = klastr chwilami ma węzły
  w dwóch wersjach — to normalne i wspierane przy zgodnym wsrep API.
- **Writer restartuje się ostatni** — ProxySQL `mysql_galera_hostgroups`
  automatycznie promuje backup-writera przy drainless failover.

## 1. Weryfikacja wykonalności celu (zanim cokolwiek zmienisz)

```bash
# Najnowsza łatka serii docelowej:
curl -s "https://downloads.mariadb.org/rest-api/mariadb/11.8" | jq -r '.releases | keys[]' | sort -V | tail -3

# Czy repo serii istnieje dla EL10 i co dnf faktycznie rozwiąże (na jednym z węzłów):
ansible <db1> -i clusters/<klaster>/inventory.yml -b -m shell -a "printf '[probe]\nbaseurl=https://dlm.mariadb.com/repo/mariadb-server/11.8/yum/rhel/10/x86_64\ngpgcheck=0\n' > /etc/yum.repos.d/probe.repo && dnf -q --disablerepo='*' --enablerepo='probe' --showduplicates list available 'MariaDB-server' 'MariaDB-client' 'MariaDB-backup' 'galera-4'; rm -f /etc/yum.repos.d/probe.repo"
```

**Warunek twardy:** `galera-4` w repo docelowym = to samo wydanie co w klastrze
(wsrep API 26). Inaczej rolling między wersjami nie zadziała — najpierw wyrównaj
galera, potem mariadb.

## 2. Zmiany w repo (przed dotknięciem maszyn)

1. **Lockfile**: skopiuj platformowy (np. `versions-el10.lock.yml`) do
   `versions/<nazwa>-<seria>.lock.yml` i zmień **wyłącznie** blok `mariadb`
   (`version`, `series`, `eol`, `rpm_release`, `repo_setup_args`,
   `verified_baseurl`). Reszta pinów platformowych bez zmian.
2. **`clusters/<klaster>/cluster.yml`**: `versions.lock_file` → nowy lockfile;
   `mariadb_tuning.gcache_size` → **`2G`** (warunek #2 planu: okno IST zamiast
   SST; mimo 512M IST zwykle działa — 2G to zalecenie dokumentacji).
3. **ADR/record**: `docs/records/<data>-mariadb-<seria>-upgrade-<klaster>.md` —
   powód, świadome regresje, procedura, rollback.
4. Walidatory: `validate-cluster-schema.py` + `validate-lockfile.py`
   (ściślej: `python3 tests/validation/validate-cluster-schema.py clusters/<klaster>/cluster.yml clusters/schema/cluster.schema.json`).
5. Plan: `make cluster-upgrade-plan CLUSTER=<klaster> ANSIBLE_OPTS="-e f12_target=<seria> [-e f12_allow_eol_regression=true]"`
   → `docs/plans/major-upgrade-plan.md`. Recap musi pokazać `changed=0` na
   węzłach (plan jest read-only — ISC-53).

## 3. Backup przed (kontrakt ISC-32)

Backup leci z pipeline'u na węźle (sekrety są na nodach, nie w env sesji):

```bash
ansible <db1> -i clusters/<klaster>/inventory.yml -b -m shell -a "/opt/galera-backup/galera-backup backup <klaster> 2>&1 | tail -2"
```

Oczekiwane: `… completed successfully (galera-<klaster>-<ts>, N bytes)`. **Zapisz
ID backupu** — to jedyna ścieżka rollback.

## 4. Kolejność węzłów

Writer odczytaj z runtime ProxySQL (tożsamość z `/etc/proxysql/admin-check.cnf`
wdrożona przez warstwę wspólną — nie używaj sekretów z env):

```bash
ansible x10p1 -i platform/<platforma>/inventory.yml -b -m shell -a "mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf -h127.0.0.1 -P6032 -uadmin -N -B -e \"SELECT hostname FROM runtime_mysql_servers WHERE hostgroup_id=<writer_hg> AND status='ONLINE'\""
```

Kolejność: canary = pierwszy non-writer, potem pozostali non-writers, **writer
ostatni**.

## 5. Procedura per węzeł (powtórz dla każdego)

> **Automatyzacja per węzeł:** Całą poniższą sekwencję (5a drain w ProxySQL, 5b flush/stop/repo/dnf/start/IST/mup, 5c undrain i weryfikację zdrowia klastra)
> wykonuje dedykowane polecenie fabryki:
> ```bash
> make cluster-upgrade-node CLUSTER=<klaster> target_node=<węzeł> old_mariadb_version=<wersja_przed>
> ```
> Poniższy opis przedstawia kroki składowe i służy do weryfikacji manualnej lub diagnostyki.

### 5a. Drain w ProxySQL

```bash
ansible x10p1 -i platform/<platforma>/inventory.yml -b -m shell -a "mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf -h127.0.0.1 -P6032 -uadmin -N -B -e \"UPDATE mysql_servers SET status='OFFLINE_SOFT' WHERE hostname='<ip_węzła>'; LOAD MYSQL SERVERS TO RUNTIME; SELECT hostgroup_id,hostname,status FROM runtime_mysql_servers WHERE hostname='<ip_węzła>'\""
```

### 5b. Upgrade na węźle (atomiczny skrypt — przerwanie w połowie zostawia węzeł zatrzymany, co jest bezpieczne dla klastra)

```bash
ansible <węzeł> -i clusters/<klaster>/inventory.yml -b -m shell -a "set -e; \
mysql -e 'SET GLOBAL innodb_fast_shutdown=0'; systemctl stop mariadb; \
sed -i 's#/repo/mariadb-server/<stara>/#/repo/mariadb-server/<nowa>/#' /etc/yum.repos.d/mariadb.repo; \
sed -i 's/gcache.size=512M/gcache.size=2G/' /etc/my.cnf.d/server.cnf; \
dnf -q makecache --disablerepo='*' --enablerepo='mariadb-main' >/dev/null; \
dnf -q install -y MariaDB-server-<wer>-1.el10 MariaDB-client-<wer>-1.el10 MariaDB-backup-<wer>-1.el10 galera-4-<wer_galery>-1.el10 >/dev/null; \
rpm -q MariaDB-server galera-4; \
systemctl start mariadb; \
s=''; for i in \$(seq 1 60); do s=\$(mysql -N -B -e \"SHOW GLOBAL STATUS LIKE 'wsrep_local_state'\" 2>/dev/null | awk '{print \$2}'); [ \"\$s\" = \"4\" ] && break; sleep 5; done; \
[ \"\$s\" = \"4\" ] || { echo \"IST/SST FAILED state=\$s\"; exit 1; }; echo 'wsrep: Synced'; \
mariadb-upgrade --skip-write-binlog >/tmp/mup.log 2>&1 || { echo UPGRADE-FAILED; tail -5 /tmp/mup.log; exit 1; }; tail -2 /tmp/mup.log; \
mysql -N -B -e \"SELECT @@version; SHOW GLOBAL STATUS LIKE 'wsrep_cluster_status'; SHOW GLOBAL STATUS LIKE 'wsrep_cluster_size'\""
```

Szczegóły, które miał znaczenie (doświadczone):

- `innodb_fast_shutdown=0` **przed** stop — czysty flush na wypadek rollbacku.
- `sed` repo: repo_setup pisze **wersjonowany katalog** (`/11.4.12/`), więc swap
  to jedna podmiana ścieżki — odporna na dryf aliasu serii.
- `mariadb-upgrade --skip-write-binlog` po starcie: lokalna aktualizacja tabel
  systemowych BEZ replikowania DDL do węzłów w starszej wersji. W 11.8 server
  robi auto-upgrade przy starcie — jawnie odpalony jest idempotentny i daje
  czytelny log (`/tmp/mup.log`).
- Powrót przez **IST** (sekundy). Jeśli `gcache` donora przepada → SST (też OK,
  wolniej). Pętla ma 5 min na powrót; po tym STOP — nie kontynuuj rollingu (ISC-55).
- Oczekiwany czas na węzeł: **~40 s** (lab, mały datadir).

### 5c. Undrain

```bash
ansible x10p1 -i platform/<platforma>/inventory.yml -b -m shell -a "mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf -h127.0.0.1 -P6032 -uadmin -N -B -e \"UPDATE mysql_servers SET status='ONLINE' WHERE hostname='<ip_węzła>'; LOAD MYSQL SERVERS TO RUNTIME\""
```

Monitor galera sam wyrówna placement (writer/backup) w kilka sekund — **nie
wymuszaj niczego ręcznie**. Weryfikacja po ustaniu ruchu (sleep 8):

```sql
SELECT hostgroup_id,hostname,status FROM runtime_mysql_servers WHERE hostgroup_id IN (<writer_hg>,<backup_hg>) ORDER BY hostgroup_id,hostname;
SELECT writer_hostgroup,active,max_writers FROM runtime_mysql_galera_hostgroups WHERE writer_hostgroup=<writer_hg>;
```

Uwaga na kolumny: `runtime_mysql_galera_hostgroups` ma `active`/`max_writers`,
NIE ma `active_writer` (writer = wiersz ONLINE w writer_hg przy
`max_writers=1`). **Wydzielaj kwerendy** — błąd w drugiej kwerendzie zawali
cały wywołanie ansible (rc≠0), mimo że pierwsza (UPDATE) zrobiła już swoje.

### 5d. Węzeł restore (`<klaster>r1`) — po całym rollingu

`mariabackup` **starszy niż server nie odtworzy nowego backupu**. Podbij
tooling na węźle restore (ten sam swap repo, tylko pakiety klienta/backupu):

```bash
ansible <klaster>r1 -i clusters/<klaster>/inventory.yml -b -m shell -a "sed -i 's#/repo/mariadb-server/<stara>/#/repo/mariadb-server/<nowa>/#' /etc/yum.repos.d/mariadb.repo; dnf -q makecache --disablerepo='*' --enablerepo='mariadb-main' >/dev/null; dnf -q install -y MariaDB-backup-<wer>-1.el10 MariaDB-client-<wer>-1.el10; rpm -q MariaDB-backup"
```

## 6. Weryfikacja po upgrade

```bash
# Wersja + wsrep na WSZYSTKICH węzłach:
ansible galera -i clusters/<klaster>/inventory.yml -b -m shell -a "mysql -N -B -e \"SELECT @@version; SHOW GLOBAL STATUS LIKE 'wsrep_local_state'\""

# Integralność tabel użytkownika (lista oddzielona PRZECINKAMI — nowa linia daje syntax error):
T=$(mysql -N -B -e "SELECT CONCAT(table_schema,'.',table_name) FROM information_schema.tables WHERE table_schema IN (<twoje_schematy>)" | tr '\n' ',' | sed 's/,$//'); mysql -e "CHECK TABLE $T"

# Backup po upgrade (dowód, że pipeline działa na nowej wersji):
ansible <db1> … "/opt/galera-backup/galera-backup backup <klaster>"

# Restore drill (ISC-36 — backup odtwarzany na czysty węzeł restore):
make cluster-restore-drill CLUSTER=<klaster> CONFIRM=yes

# Topologia ProxySQL po wyrównaniu — identyczna ze sprzed upgrade
# (writer ONLINE w writer_hg, pozostali w backup_hg; active=1, max_writers=1).
```

Sonda aplikacyjna (TLS + read-your-writes przez VIP) jest env-gated:
`make lab-app-verify CLUSTER=<klaster>` z powłoki operatora (`APP_DB_PASSWORD`).
Do czasu jej przejścia upgrade uznawaj za „technicznie zielony, aplikacyjnie
niepotwierdzony".

## 7. Rollback (gdy coś pójdzie nie tak)

1. **STOP rolling** przy pierwszej utracie zdrowia (ISC-55). Klastr z quorum na
   starej wersji + 1–2 węzły 11.8 wróci do pracy na 11.4 po przywróceniu
   pakietów **tylko jeśli `mariadb-upgrade` jeszcze nie ruszył datadiru** —
   dlatego `innodb_fast_shutdown=0` i natychmiastowa ocena.
2. Po `mariadb-upgrade` (zapisanym datadirze): **tylko restore z backupu**
   (ID z kroku 3) na czysty datadir — `make cluster-restore-drill` pokazuje
   procedurę; docelowo na węźle restore, przy pełnej awarii — bootstrap z
   `cluster-recover`.
3. Nigdy `dnf downgrade` MariaDB z niepustym datadir.

## 8. Czego NIE robić (zebrane pułapki)

- Nie mieszaj wersji w jednym klastrze na stałe (tylko przejściowo w rolling).
- Nie dotykaj statusów w ProxySQL ręcznie poza drain/undrain — monitor galera
  sam zarządza placement (max_writers=1).
- Nie odpalaj `cluster-patch` do major upgrade — to narzędzie patchowania w
  ramach skonfigurowanego repo (canary + bramy), bez swapu repo.
- Nie rób DDL podczas rolling; nie restartuj dwóch węzłów naraz.
- Nie kopiuj sekretów z node'ów do env/transkryptu — tożsamość
  `admin-check.cnf` i pipeline backupu są już na maszynach.

## 9. Checklist skrócony

```
[ ] repo serii docelowej istnieje dla EL10 (dnf probe), galera-4 zgodna
[ ] lockfile nowy + cluster.yml przełączone + walidatory PASS
[ ] ADR/record z uzasadnieniem (+ f12_allow_eol_regression jeśli EOL krótszy)
[ ] plan wygenerowany (changed=0 na hostach)
[ ] backup przed — ID zapisany
[ ] rolling: canary → non-writers → writer; brama Synced po każdym
[ ] o10r1: MariaDB-backup/client podbite do wersji docelowej
[ ] VERSION() = cel na 3/3; CHECK TABLE OK; topologia ProxySQL jak przed
[ ] backup po + restore drill PASS
[ ] lab-app-verify (APP_DB_PASSWORD) — zalecane
[ ] commit: lockfile + cluster.yml + ADR + plan
```
