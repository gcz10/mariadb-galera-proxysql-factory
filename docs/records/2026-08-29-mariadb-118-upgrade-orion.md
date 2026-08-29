# Upgrade orionv10-r10: MariaDB 11.4.12 → 11.8.9 (2026-08-29)

## Decyzja (ADR)

- **Powód:** aplikacja najemcy orion wymaga dokładnie serii **11.8** — to jest
  uzasadnienie dla świadomej regresji okna wsparcia (EOL 11.4 = 2029-05 →
  EOL 11.8 = 2028-06). Strażnik „regresji wsparcia" w `f12_upgrade_plan.yml`
  (ISC-55/56) jest obejmowany jawnie: `-e f12_allow_eol_regression=true`.
- **Zakres:** TYLKO `orionv10-r10`. `cassiopeiav10-r10` zostaje na
  `versions-el10.lock.yml` (11.4) jako kontrola — miks serii MIĘDZY klastrami
  jest wspierany kontraktem (`versions.lock_file` per klaster); w obrębie
  jednego klastra wersje mieszają się tylko przejściowo (rolling, wsrep API 26).
- **Lockfile:** nowy `versions/versions-el10-118.lock.yml` — struktura 1:1 z
  el10, zmienia się wyłącznie blok `mariadb` (11.8.9, eol 2028-06).
- **gcache:** 512M → **2G** (warunek #2 planu ISC-54: wystarczające okno IST na
  czas upgrade, by węzły wracały przez IST, nie SST).

## Weryfikacja wykonalności (2026-08-29)

- Repo `dlm.mariadb.com/repo/mariadb-server/11.8/yum/rhel/10/x86_64` — dnf probe
  na `o10db1` (`--enablerepo=mariadb-118probe`): **MariaDB-server/client/backup
  11.8.9-1.el10** oraz **galera-4 26.4.27-1.el10** — to samo wydanie galery co
  klaster 11.4 (wsrep API 26) → warunek rolling upgrade spełniony wprost.
- 11.8.9 = najnowsza łatka serii (Q3 2026 maintenance, mariadb.org 2026-08-24).

## Procedura (z `docs/plans/major-upgrade-plan.md`, ISC-54)

Rolling, canary non-writer pierwszy, writer ostatni; brama zdrowia
(`wsrep_local_state=4` + `Primary` + pełny size) po każdym węźle; STOP przy
utracie zdrowia (ISC-55):

1. Pełny backup `mariadb-backup` (kontrakt ISC-32; **rollback = restore**, nie
   downgrade — ISC-56: datadir wyższej wersji jest forward-incompatible).
2. Drain węzła w ProxySQL (`OFFLINE_SOFT`), `innodb_fast_shutdown=0`, stop.
3. `baseurl` w `/etc/yum.repos.d/mariadb.repo`: `/11.4.12/` → `/11.8.9/`
   (wersjonowany katalog — odporny na dryf aliasu serii).
4. `dnf install` dokładnych wersji: server/client/backup 11.8.9-1.el10 +
   galera-4 26.4.27-1.el10; `gcache.size=2G` w `server.cnf` (spójnie z
   cluster.yml — pilnowane przez F13 drift).
5. Start → IST; `mariadb-upgrade --skip-write-binlog` (lokalnie, bez replikacji
   DDL do nie-upgradowanych węzłów).
6. Brama zdrowia → undrain w ProxySQL → następny węzeł.

## Weryfikacja po

- `VERSION()` = 11.8.9 na 3/3 węzłach; `mariadb-upgrade` OK; `CHECK TABLE`;
  `wsrep_cluster_size=3`, `Primary`, `Synced`.
- Post-gate: `lab-galera-verify`, `lab-proxysql-verify`, `lab-endpoint-verify`,
  `cluster-drift`, sonda aplikacyjna z `x10app`, backup + restore po upgrade.

## Sekrety operacyjne

Operacje ProxySQL przez zdeponowaną tożsamość `/etc/proxysql/admin-check.cnf`
(wzorzec `check_proxysql.sh`), nie przez env sesji.

## Wykonanie (2026-08-29, zmierzone)

- Rolling 3/3: `o10db1` (canary, 46s) → `o10db2` (41s) → `o10db3` (writer,
  37s). Każdy węzeł: drain `OFFLINE_SOFT` → `innodb_fast_shutdown=0` → stop →
  repo `/11.8.9/` → `gcache.size=2G` → dnf exact `11.8.9` + `galera-4
  26.4.27` → start → **IST (bez SST)** → `mariadb-upgrade
  --skip-write-binlog` OK → brama `Synced/Primary/size=3` → undrain.
- Stan końcowy: 3/3 `11.8.9-MariaDB`, `Primary`, `Synced`; ProxySQL runtime
  po wyrównaniu identyczny ze sprzed upgrade (`.166` single-writer ONLINE,
  `.164/.165` backup ONLINE; `active=1, max_writers=1`).
- `CHECK TABLE` na wszystkich tabelach użytkownika (`isa_test`,
  `gcache_meas`): **OK**.
- Backup przed: `galera-orionv10-r10-20260829-152437` (57 MB). Backup po:
  `galera-orionv10-r10-20260829-153230` (58 MB) — pipeline działa na 11.8.
- Restore drill po upgrade (`cluster-restore-drill CONFIRM=yes`): backup
  11.8.9 odtworzony na czysty `o10r1` (datadir z `isa_test`/`gcache_meas`),
  failed=0 (ISC-34/36/37). Tooling `o10r1` podbity do
  MariaDB-backup/client 11.8.9 — warunek odtwarzalności nowego formatu.
- Otwarte (env-gated): `lab-app-verify` wymaga `APP_DB_PASSWORD` (poza
  sesją); zalecane odpalenie z powłoki operatora.

Runbook uogólniający tę procedurę dla przyszłych upgrade'ów:
`docs/runbooks/upgrade-mariadb-major.md`.
