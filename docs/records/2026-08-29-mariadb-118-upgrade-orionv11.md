# Upgrade orionv11-r9: MariaDB 11.4.12 → 11.8.9 (2026-08-29)

## Decyzja (ADR)

- **Powód:** aplikacja najemcy orion wymaga serii **11.8** (pierwotny ADR:
  `docs/records/2026-08-29-mariadb-118-upgrade-orion.md`). Po przebudowie
  klastra na Rocky 9 (see `2026-08-29-orionv11-r9-build.md`) upgrade powtarzamy
  na nowym klastrze — tym razem wg runbooka
  `docs/runbooks/upgrade-mariadb-major.md`.
- **Świadoma regresja EOL:** 11.8 = 2028-06 vs 11.4 = 2029-05 — strażnik ISC-55
  obejmowany jawnie `-e f12_allow_eol_regression=true`.
- **Lockfile:** `versions/versions-el9-118.lock.yml` (struktura 1:1 z
  `versions.lock.yml`, zmienia się wyłącznie blok `mariadb`).
- **gcache:** 512M → **2G** (okno IST, warunek #2 planu ISC-54) — edycja
  cluster.yml + per-węzeł w oknie stopu.
- Zakres: TYLKO `orionv11-r9`. `cassiopeiav10-r10` zostaje na 11.4 (kontrola).

## Wykonalność (zweryfikowana 2026-08-29)

- dnf probe EL9 (`dlm.mariadb.com/repo/mariadb-server/11.8/yum/rhel/9/x86_64`):
  MariaDB-server/client/backup **11.8.9-1.el9** + **galera-4 26.4.27-1.el9**
  (ta sama galera co klaster → wsrep API 26 → rolling zgodny).

## Wykonanie

Rolling wg runbooka: canary non-writer → writer ostatni; drain
(`OFFLINE_SOFT`) → `innodb_fast_shutdown=0` → stop → repo `/11.4.12/`→`/11.8.9/`
→ `gcache.size=2G` → dnf exact `11.8.9` + `galera-4 26.4.27` → start → IST →
`mariadb-upgrade --skip-write-binlog` → brama `Synced/Primary/size=3` →
undrain. Tooling `o11r1` podbity **przed** pierwszym backupem po upgrade
(mariabackup 11.4 nie odtworzy formatu 11.8 — lekcja z orionv10).

## Rollback

Brak downgrade datadir (ISC-56) — wyłącznie restore z backupu. Backup przed:
`galera-orionv11-r9-20260829-<ts>` (ID po wykonaniu).

## Wykonanie (2026-08-29, zmierzone)

- Backup przed: `galera-orionv11-r9-20260829-182227` (56 MB, S3).
- Rolling 3/3: `o11db1` (canary, 45s) → `o11db2` (38s) → `o11db3`
  (writer, 39s). Każdy węzeł: drain `OFFLINE_SOFT` →
  `innodb_fast_shutdown=0` → stop → repo `/11.8.9/` → `gcache.size=2G` →
  dnf exact `11.8.9-1.el9` + `galera-4 26.4.27-1.el9` → start → **IST
  (bez SST)** → `mariadb-upgrade --skip-write-binlog` OK → brama
  `Synced/Primary/size=3` → undrain.
- Stan końcowy: 3/3 `11.8.9-MariaDB`, `Primary`, `Synced`; ProxySQL
  po wyrównaniu: `.166` single-writer ONLINE, `.164/.165` backup ONLINE
  (`active=1, max_writers=1`) — jak przed upgrade.
- `CHECK TABLE` (isa_test.restore_probe, isa_test.app_conformance,
  gcache_meas.w): **OK** na 3/3 węzłach.
- Backup po: `galera-orionv11-r9-20260829-182752` (57 MB).
- Restore drill: PASS (`o11r1` ok=22 failed=0; tooling mariabackup/client
  11.8.9 podbity PRZED backupem po upgrade — lekcja z orionv10).
- Drift ISC-21: PASS — ProxySQL runtime==disk (2 węzłów), Galera uuid
  spójny (`c6e93d8a…`); zero dryfu po upgrade.
- `lab-app-verify`: PASS — TLS_AES_256_GCM_SHA384 frontend+backend,
  read-your-writes, ROLLBACK/COMMIT, jeden writer (o11db3), CA klastra
  i CA endpointu zweryfikowane.

## Stan floty po

| Klaster | OS | MariaDB |
|---|---|---|
| `orionv11-r9` | Rocky 9.8 | **11.8.9** |
| `cassiopeiav10-r10` | Rocky 10.2 | 11.4.12 (kontrola) |
