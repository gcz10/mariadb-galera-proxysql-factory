# Rotacja 2026-09-02: klastry MariaDB 11.4 (orionv14-r10 i cassiopeiav13-r9)

## Cel

Na polecenie operatora: wyłączyć maszyny poprzedniej generacji (VMID 10016–10023),
pozostawić ich kod w repozytorium (`clusters/orionv13-r10`, `clusters/cassiopeiav12-r9`),
zachować platformę `xenonv11` oraz `x10mon` (MinIO, PMM), i postawić od zera nową
parę klastrów z bazą **MariaDB 11.4**:
1. `orionv14-r10`: **Rocky Linux 10.2** z użyciem **Terraforma** (`versions-el10.lock.yml`).
2. `cassiopeiav13-r9`: **Rocky Linux 9.8** bez Terraforma, z użyciem `tools/pve-create-vm.sh`
   (`versions.lock.yml`, flaga `terraform_managed: false`).

## Węzły, adresacja i VMID

- **`orionv14-r10`** (Rocky 10.2, Terraform):
  - `o14db1`: VMID **10025**, IP `192.168.1.50` (Galera node 1)
  - `o14db2`: VMID **10026**, IP `192.168.1.51` (Galera node 2)
  - `o14db3`: VMID **10027**, IP `192.168.1.52` (Galera node 3, writer)
  - `o14r1`: VMID **10028**, IP `192.168.1.53` (restore)
  - `hostgroup_base: 770`, user: `app_user_ov14`
  - Backup S3: bucket `orionv14-galera-backups` na `x10mon` (`.160:9000`)
- **`cassiopeiav13-r9`** (Rocky 9.8, bez Terraforma — `pve-create-vm.sh`):
  - `c13db1`: VMID **10029**, IP `192.168.1.60` (Galera node 1)
  - `c13db2`: VMID **10030**, IP `192.168.1.61` (Galera node 2)
  - `c13db3`: VMID **10031**, IP `192.168.1.62` (Galera node 3, writer)
  - `c13r1`: VMID **10032**, IP `192.168.1.63` (restore)
  - `hostgroup_base: 810`, user: `app_user_cv13`
  - Backup S3: bucket `cassiopeiav13-galera-backups` na `x10mon` (`.160:9000`)
- **Stare maszyny**: VMID 10016–10023 zatrzymane (`qm stop` przez API PVE).
- **Adresacja IP**: Obie pule (`.50–.53` oraz `.60–.63`) potwierdzone aktywnym skanem ICMP
  przed przydziałem. Zero kolizji w `probe-address-collision.py`.
- **ProxySQL**: Oba klastry wpięte do wspólnej pary `xenonv11` (VIP `192.168.1.172:6033`).
  Hostgroupy w pełni rozłączne (770 i 810 vs 690 i 730).

## Dowody akceptacji zmierzone po budowie

Oba klastry przeszły pełną bramkę `lab-post-build-gate` w pierwszym podejściu bez błędów:
- **`orionv14-r10`**:
  - `probe-app-conformance`: PASS (TLS_AES_256_GCM_SHA384, read-your-writes, rollback/commit, writer `o14db3`)
  - `probe-backup`: PASS (artefakt `galera-orionv14-r10-20260902-194050` w S3, szyfrowanie `aes-256-gcm-pbkdf2-sha256`, sha256 OK, MariaDB `11.4.12`, `seqno=0`)
  - `probe-restore`: PASS (odtworzenie na izolowany host `o14r1`, 1 wiersz zweryfikowany)
  - `probe-rolling-restart`: PASS (3/3 węzły Synced/Primary)
  - `probe-gcache`: PASS (`512M` pokrywa write_rate 74222 B/s na 30-minutowe okno IST)
  - `probe-pmm-native`: PASS (PMM 3.9.1, 3 węzły, 3 eksportery, 10 reguł ISC-47)
- **`cassiopeiav13-r9`**:
  - `probe-app-conformance`: PASS (identyczny komplet dowodów aplikacyjnych, writer `c13db3`)
  - `probe-backup`: PASS (artefakt `galera-cassiopeiav13-r9-20260902-195919` w S3, GCM v2, MariaDB `11.4.12`)
  - `probe-restore`: PASS (odtworzenie na izolowany host `c13r1`)
  - `probe-rolling-restart`: PASS (3/3 węzły Synced/Primary)
  - `probe-gcache`: PASS (`512M`)
  - `probe-pmm-native`: PASS (PMM 3.9.1, 10 reguł ISC-47)
