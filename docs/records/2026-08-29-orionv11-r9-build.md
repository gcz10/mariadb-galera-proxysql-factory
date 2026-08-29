# Build orionv11-r9 — Rocky 9 + MariaDB 11.4.12 (2026-08-29)

## Decyzja

Zdjęcie `orionv10-r10` (Rocky 10, MariaDB 11.8.9 po upgrade z 2026-08-29) i
postawienie na tych samych slotach klastra **Rocky 9 + MariaDB 11.4.12** na
czysto (czysty seed; backup 11.8 zostaje w S3 jako archiwum — restore
11.8→11.4 jest forward-incompatible i nie był wykonywany).

## Artefakty

- `clusters/orionv11-r9/{cluster.yml,inventory.yml}` — lockfile
  `versions.lock.yml` (EL9), `gcache_size: 512M`, hg `610-640`,
  `app_user_ov11`, endpoint `.172:6033` (wspólny VIP platformy xenonv10).
- `terraform/orionv11-r9/main.tf` — obraz `Rocky-9.8-GenericCloud.qcow2`,
  VMID 10004-10007, IP `.164-.167` (te same sloty), hosty `o11db1-3`/`o11r1`,
  **bez** snippetu `r10-cloud-init` (Rocky 9 nie potrzebuje; provider default).
- PKI: `pki/o11/` (`pki/generate.sh o11-galera o11db1,o11db2,o11db3,.164,.165,.166`;
  git-ignored). Frontend nadal CA platformy (`pki/xenonv10/ca.pem`).
- Archiwizacja: `docs/records/archives/{clusters,terraform}-orionv10-r10/`.

## Sekwencja (wykonana)

1. Backup przed: `galera-orionv10-r10-20260829-153230` (S3, archiwum).
2. `cluster-deregister CLUSTER=orionv10-r10 CONFIRM=yes` — ProxySQL/PMM/
   Grafana/MinIO-clean (bucket `orionv10-galera-backups` zachowany).
3. `infra-teardown CLUSTER=orionv10-r10 CONFIRM=yes` — 4 destroyed,
   0 sierot ZFS.
4. `infra-provision CLUSTER=orionv11-r9` — 196 s.
5. `cluster-trust-hosts` — 8/8 kluczy.
6. `cluster-build CLUSTER=orionv11-r9 CONFIRM=yes` — ~21,5 min, pełna
   bramka po budowie PASS.

## Dowody (zmierzone 2026-08-29)

- Idempotencja: `changed=0` (f2_install, site, firewall, wszystkie hosty).
- Galera: 3/3 `Primary/Synced/Ready`, SST=mariabackup, zero tabel bez PK.
- ProxySQL: 1 writer (o11db3), 3 backendy ONLINE, runtime==disk.
- Endpoint: VIP `.172` na x10p1, TLS wystawca `CN=xenonv10 CA`.
- App-conformance: TLS_AES_256_GCM_SHA384 frontend+backend,
  read-your-writes, ROLLBACK/COMMIT, jeden writer.
- Backup: `galera-orionv11-r9-20260829-180826`, AES-256-CBC, sha256 OK,
  metadata `11.4.12`, `seqno=0`. Restore drill: PASS.
- gcache: write-rate `83500 B/s` → wymagane 144M, wdrożone 512M.
- PMM 3.9.1: 3 namespaced nodes, 3 node-exporters 1.12.1, QAN, reguły ISC-47.

## Stan floty po

| Klaster | OS | MariaDB | Platforma |
|---|---|---|---|
| `orionv11-r9` | Rocky 9.8 | 11.4.12 | xenonv10 (wspólna) |
| `cassiopeiav10-r10` | Rocky 10.2 | 11.4.12 | xenonv10 (wspólna) |

`orionv10-r10` (11.8.9) — zdjęty; ADR upgrade w
`docs/records/2026-08-29-mariadb-118-upgrade-orion.md` (historia).

## Defekt z chaos-testów: /run/mariadb po twardym reboocie (2026-08-29)

Test `lab-failover-hard-test` (sysrq — twarda utrata maszyny writera) odsłonił
latentny defekt całej floty: `/run` jest tmpfs, a `/run/mariadb` tworzył
wyłącznie playbook converge — po twardym reboocie katalog znikał i **mysqld
nie podnosił się** (`Can't create/write pidfile`, Errcode: 2). Klaster
kontynuował na 2 węzłach (failover aplikacyjny PASS), ale węzeł nie wracał.

**Fix:** `site.yml` + `f5_join.yml` wdrażają `/etc/tmpfiles.d/mariadb.conf`
(`d /run/mariadb 0755 mysql mysql -`) — bootowy `systemd-tmpfiles-setup`
odtwarza katalog przed startem mariadb. Wdrożone converge'em na orionv11
i cassiopeia. Dowód: sysrq-reboot `o11db3` po fixie → mariadb wstał sam,
`Synced` w pierwszej próbie.

## Testy awaryjności i wydajności (orionv11-r9, 2026-08-29)

| Test | Wynik |
|---|---|
| Failover soft (SIGKILL writera) | PASS — gap 6,5 s (RTO 120 s), 0 utraconych transakcji |
| Failover hard (sysrq, utrata maszyny) | PASS — 0 utraconych transakcji (470 obecnych) |
| Utrata kworum (P2, SIGKILL 2 węzłów) | PASS — kontrakt `degraded`, artefakt quorum-evidence |
| Split-brain (partycja sieci, ISC-30) | PASS — majority zapisywalny, minority odmawia zapisów, heal do 3 |
| Backup pod obciążeniem (ISC-39) | PASS — flow control 0 ns, max stall 0,09 s / 2977 commitów |
| Wydajność z x10app | direct do 20,6k q/s; przez VIP ProxySQL 10-13,6k q/s (narzut hopu 19-27%) |
| gcache sizing | write-rate 83 500 B/s → wymagane 144M, wdrożone 512M |
