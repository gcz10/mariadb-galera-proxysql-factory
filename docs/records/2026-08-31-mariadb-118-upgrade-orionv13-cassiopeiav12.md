# Upgrade 11.4.12 → 11.8.9: orionv13-r10 i cassiopeiav12-r9 (2026-08-31)

## Powód

Decyzja operatora po rotacji z 2026-08-30 (oba świeże klastry stanęły na 11.4
jako bazie startowej): ujednolicenie serii z poprzednią linią produkcyjną na
**11.8.9**. Procedura: runbook `docs/runbooks/upgrade-mariadb-major.md`
(rolling per węzeł, drain/undrain ProxySQL, writer ostatni, `mariadb-upgrade`
per węzeł). Galera 26.4.27 bez zmian → wsrep API 26 zgodny w rolling.

## Świadoma regresja wsparcia

11.8 EOL 2028-06 < 11.4 EOL 2029-05 — strażnik ISC-55 wymaga
`-e f12_allow_eol_regression=true`. Akceptuje operator (wybór serii 11.8).

## Rollback

Tylko restore z backupów sprzed upgrade (ID w sekcji „Backup przed");
po `mariadb-upgrade` downgrade pakietów jest zabroniony (ISC-56).

## Backup przed

- orionv13-r10: `galera-orionv13-r10-20260831-004935` (58.7 MB)
- cassiopeiav12-r9: `galera-cassiopeiav12-r9-20260831-004937` (58.7 MB)

## Wynik (zmierzone po upgrade)

- **orionv13-r10**: rolling 3/3 węzłów 11.8.9 (galera-4 26.4.27-1.el10),
  powroty przez IST (Synced, Primary, size=3 po każdym węźle), mariadb-upgrade
  OK na każdym; CHECK TABLE wszystkich tabel użytkownika OK; backup po upgrade
  `galera-orionv13-r10-20260831-005444` (59.7 MB); restore drill PASS
  (o13r1 ok=22 failed=0); app-conformance PASS (TLS_AES_256_GCM_SHA384,
  read-your-writes, writer o13db3); pełna bramka post-build PASS.
- **cassiopeiav12-r9**: rolling 3/3 węzłów 11.8.9 (galera-4 26.4.27-1.el9),
  identyczna procedura; CHECK TABLE OK; backup po upgrade
  `galera-cassiopeiav12-r9-20260831-005827`; restore drill PASS (c12r1
  ok=22 failed=0); app-conformance PASS (writer c12db3); pełna bramka PASS.
- Tooling węzłów restore (o13r1/c12r1): MariaDB-backup/client 11.8.9.
