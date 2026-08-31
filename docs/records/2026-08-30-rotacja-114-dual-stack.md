# Rotacja 2026-08-30: dwa nowe klastry MariaDB 11.4 (Rocky 10 + Rocky 9)

## Cel

Na polecenie operatora: skasować oba żywe klastry 11.8 (orionv12-r9,
cassiopeiav11-r9), zachować monitoring i MinIO (x10mon), postawić dwa nowe
klastry na serii **11.4**: jeden z Terraformem na **Rocky 10**, drugi na
ręcznych VM-ach **bez Terraformu** na **Rocky 9**.

## Artefakty i wersje

- `clusters/orionv13-r10/` — Rocky 10.2, MariaDB 11.4.12, galera-4 26.4.27;
  root `terraform/orionv13-r10/` (VMID 10016-10019), hostgroup_base 690;
  lockfile `versions-el10.lock.yml`; backup `orionv13-galera-backups`.
- `clusters/cassiopeiav12-r9/` — Rocky 9.8, MariaDB 11.4.12, galera-4 26.4.27;
  VM-y utworzone REST API hypervisora wg `machines-from-elsewhere.md`
  (VMID 10020-10023), hostgroup_base 730; lockfile `versions.lock.yml`;
  backup `cassiopeiav12-galera-backups`. `cluster.yml` nosi nowe pole
  `terraform_managed: false` — jawne wyjście z kontraktu inventory↔TF
  (sonda `probe-inventory-tf-consistency` pomija wymóg roota; schema + 3 testy).
- Wspólna para ProxySQL xenonv11 (VIP .172) i x10mon (MinIO/PMM/Maildev):
  nietknięte. Obaj nowi najemcy korzystają z tego endpointu.

## Provenance adresów i VMID

- `.30-.33` (orionv13-r10): wcześniej użycie przez **zarchiwizowanego
  najemcę kobalt-r9** (`docs/records/archives/clusters-kobalt-r9/`:
  kp2=.30, kapp=.32, kg1=.33).
- `.40-.42` (cassiopeiav12-r9): wcześniej **sigma-r9** (sg1/sg2/sg3,
  `archives/clusters-sigma-r9/inventory.yml:36-46`).
- `.43` (cassiopeiav12-r9, restore): wcześniej **orion-r9** (og1,
  `archives/clusters-orion-r9/inventory.yml:22-24`). Żaden z tych adresów
  nie był wcześniej w rejestrze rezerwacji.
- Legalność reusu obu bloków opiera się na dwóch warunkach z nagłówka
  `clusters/reserved-addresses.yml`: WIADOMO, czym były (archiwa: kobalt-r9,
  sigma-r9, orion-r9) i WIADOMO, że ich nie ma (lista `/qemu` hypervisora
  nie wykazuje żadnej VM z tych rootów, a aktywny skan puli 10-100 z
  2026-08-30 dał żywe wyłącznie .20/.21/.22/.38/.100).
- `.100` — wykryty żywy host spoza floty; dopisany do rejestru rezerwacji.
- VMID 10016-10023: świeży zakres, nigdy wcześniej nieużywany.

## Sekwencja wykonania

1. Finalne backupy kasowanych klastrów: `galera-orionv12-r9-20260830-203141`
   i `galera-cassiopeiav11-r9-20260830-203141` (artefakty w zachowanych
   bucketach; buckety NIE zostały usunięte).
2. `cluster-deregister` obu najemców + `cluster-deregister-verify`:
   zero sierot w PMM, Grafanie, ProxySQL i kontach MinIO.
3. `infra-teardown` obu rootów: 8 VM (10004-10011) usuniętych, zero sierot ZFS.
4. Budowa orionv13-r10: terraform apply → trust-hosts 8/8 → cluster-build.
   Build przerwał się na restore-drill (bucket pusty przed pierwszym backupem);
   po ręcznym backupie dokończono restore-drill → app-host → bramka.
5. Budowa cassiopeiav12-r9: runbook machines-from-elsewhere (create → wait →
   resize 40G → start, sekwencyjnie) → trust-hosts 8/8 → pełny cluster-build
   (pierwsze uruchomienie padło na chwilowy timeout pobrania node_exportera;
   idempotentne ponowienie przeszło w całości).

## Dowody bramek (zmierzone po budowie)

- **orionv13-r10**: PASS — backup v2 AES-GCM zweryfikowany (sha256 OK),
  restore drill, app→VIP TLS_AES_256_GCM_SHA384 + read-your-writes +
  jeden writer (o13db3), hardening, rolling/patch/drift, gcache 512M pokrywa
  write_rate 74222 B/s (wymagane 128M), PMM 3.9.1: 3 węzły, 3 eksportery,
  10 reguł ISC-47 + trasa email.
- **cassiopeiav12-r9**: PASS — backup v2 GCM (metadata 11.4.12, seqno=0),
  restore drill na izolowanym hoście, rolling restart 3/3 Synced/Primary,
  patch canary + 6 bram zdrowia, drift, gcache 512M, PMM + ISC-47 + email.

## Znane wady i lekcje

- **Latentny defekt F11**: `f11_proxysql_metrics.yml` rejestruje węzły ProxySQL
  z `distro: rocky-{{ platform.rocky_linux_major }}` (major NAJEMCY, nie hosta).
  Dla najemcy o innym majorze niż wspólna para etykieta skłamałaby. Dziś nie
  strzela (para zarejestrowana przez warstwę jako `xenonv11-x11p*`, distro
  `linux`), ale przed pierwszym takim najemcą trzeba brać major z faktów gościa.
- **Lekcja procesowa**: runbook `machines-from-elsewhere.md` zawiera kompletną,
  zweryfikowaną procedurę REST (data-urlencode, cpu=host, aio=io_uring,
  pool=claude-isa, czekanie na task create). Pięć nieudanych prób przed sięgnię
 ciem po niego — następny raz: runbook najpierw.
- Sonda `probe-backup` (ISC-33/35) oraz `fetch_latest` w storage miały
  przytwierdzone `format_version == 1` — poprawione na przyjmowanie 1|2
  (lockfile el9-118: nagłówek zaktualizowany — historyczny, bez żywego
  użytkownika).
