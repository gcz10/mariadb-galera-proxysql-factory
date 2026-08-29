# Upgrade cassiopeiav10-r10: MariaDB 11.4.12 → 11.8.9 (2026-08-29)

## Decyzja (ADR)

- **Powód:** jak dla orionv11 (`2026-08-29-mariadb-118-upgrade-orionv11.md`) —
  aplikacja wymaga serii 11.8. Świadoma regresja EOL (2029-05 → 2028-06),
  strażnik ISC-55 obejmowany `-e f12_allow_eol_regression=true`.
- **Lockfile:** istniejący `versions/versions-el10-118.lock.yml` (wcześniej
  sierota po zdjęciu orionv10; zwalidowany). **Ten sam plik pełni teraz rolę
  obowiązkowego elementu ścieżki upgrade** — pakiet 11.8 wyłącza aktywny wpis
  tmpfiles `d /run/mariadb`, więc `site.yml`/`f5_join.yml` wdrażają własny
  (fix z chaos-testu sysrq tego samego dnia).
- **gcache:** 512M → **2G** (okno IST, warunek #2 planu).
- Zakres: TYLKO `cassiopeiav10-r10`. `orionv11-r9` zostaje na 11.8 (oba klastry
  w 11.8 na różnych OS — drugi wymiar miksów, wspierany per-cluster lock_file).

## Wykonalność (dnf probe na c10db1, 2026-08-29)

MariaDB-server/client/backup **11.8.9-1.el10** + **galera-4 26.4.27-1.el10**
(ta sama galera co klaster → wsrep API 26 → rolling zgodny).

## Wykonanie

Runbook `docs/runbooks/upgrade-mariadb-major.md` + sekwencja z orionv11:
backup przed (`c10db1`, scheduler) → rolling canary non-writer → writer
ostatni (drain `OFFLINE_SOFT`, `innodb_fast_shutdown=0`, repo `/11.4.12/`→
`/11.8.9/`, `gcache.size=2G`, dnf exact, start → IST →
`mariadb-upgrade --skip-write-binlog` → brama `Synced/Primary/3` → undrain)
→ tooling `c10r1` (mariabackup/client 11.8.9) → weryfikacje.

## Dowody (zmierzone 2026-08-29)

- Backup przed: `galera-cassiopeiav10-r10-20260829-192959` (65 MB, S3).
- Rolling: `c10db1` (canary, 41s) → `c10db2` (39s) → writer `c10db3` (41s) —
  każdy przez IST (bez SST), `mariadb-upgrade --skip-write-binlog` OK,
  brama `Synced/Primary/size=3` przed undrain.
- Stan końcowy: 3/3 `11.8.9-MariaDB` Primary/Synced; ProxySQL: `.170`
  single-writer ONLINE, `.168/.169` backup (`active=1, max_writers=1`).
- `CHECK TABLE` wszystkich tabel użytkownika: bez błędów.
- Backup po: `galera-cassiopeiav10-r10-20260829-193605` (66 MB).
- Restore drill: PASS (`c10r1` ok=22, failed=0; tooling 11.8.9).
- Drift ISC-21: PASS (runtime==disk, uuid spójny `74ea9f14…`).
- `lab-app-verify`: PASS (TLS_AES_256_GCM_SHA384, read-your-writes,
  writer c10db3).

## Chaos-suite na 11.8 (po upgrade)

| Test | Wynik |
|---|---|
| Failover soft (SIGKILL writera) | PASS — gap 6,2 s, 0 utraconych |
| Failover hard (sysrq, utrata maszyny) | PASS — 0 utraconych (491 transakcji); **c10db3 wstał sam po reboocie** (tmpfiles fix) |
| Utrata kworum (P2) | PASS — kontrakt `degraded`, artefakt `…-aad26661….json` |
| Split-brain (ISC-30) | PASS — majority zapisywalny, minority odmawia, heal do 3 |
| Wydajność | direct do 19,4k q/s; VIP 10,7k q/s (‑12% vs plaintext) |
