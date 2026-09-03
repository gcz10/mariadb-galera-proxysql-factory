# Rotacja 2026-09-03: Nowa generacja floty (xenonv12, orionv15-r10 i cassiopeiav14-r9)

## Cel

Na polecenie operatora: postawić od zera całkowicie nowe środowisko na nowo przydzielonych hostach i adresach IP:
1. Nowa warstwa wspólna **`xenonv12`**: nowa para ProxySQL + VIP, nowy host aplikacyjny oraz dedykowany host infra **`x12mon`** (PMM Server 3.9 + MinIO S3 + Maildev) zintegrowany w Terraformie.
2. Dwa nowe klastry Galera **MariaDB 11.4 LTS**:
   - `orionv15-r10`: **Rocky Linux 10.2** z użyciem **Terraforma** (`versions-el10.lock.yml`).
   - `cassiopeiav14-r9`: **Rocky Linux 9.8** z użyciem **`tools/pve-create-vm.sh`** (`versions.lock.yml`, `terraform_managed: false`).

---

## Węzły, adresacja i VMID

### 1. Platforma wspólna: `xenonv12` (Rocky Linux 9.8, Terraform)
- **`x12mon`**: VMID **10035**, IP `192.168.1.70` (PMM 3.9 + MinIO S3 + Maildev)
- **`x12p1`**: VMID **10036**, IP `192.168.1.71` (ProxySQL node 1, MASTER VRRP)
- **`x12p2`**: VMID **10037**, IP `192.168.1.72` (ProxySQL node 2, BACKUP VRRP)
- **`x12app`**: VMID **10038**, IP `192.168.1.73` (Host aplikacyjny klienta)
- **VIP Keepalived**: **`192.168.1.74:6033`** (bezpieczny, unikalny oktet zgodny z `reserved-addresses.yml`)

### 2. Klaster Galera 1: `orionv15-r10` (Rocky Linux 10.2, Terraform)
- **`o15db1`**: VMID **10040**, IP `192.168.1.80` (Galera node 1)
- **`o15db2`**: VMID **10041**, IP `192.168.1.81` (Galera node 2)
- **`o15db3`**: VMID **10042**, IP `192.168.1.82` (Galera node 3, aktywny writer)
- **`o15r1`**: VMID **10043**, IP `192.168.1.83` (Restore host)
- Hostgroup ProxySQL: **`780`**, App user: `app_user_ov15`
- Backup S3: bucket `orionv15-galera-backups` na `x12mon:9000`

### 3. Klaster Galera 2: `cassiopeiav14-r9` (Rocky Linux 9.8, `pve-create-vm.sh`)
- **`c14db1`**: VMID **10045**, IP `192.168.1.90` (Galera node 1)
- **`c14db2`**: VMID **10046**, IP `192.168.1.91` (Galera node 2)
- **`c14db3`**: VMID **10047**, IP `192.168.1.92` (Galera node 3, aktywny writer)
- **`c14r1`**: VMID **10048**, IP `192.168.1.93` (Restore host)
- Hostgroup ProxySQL: **`820`**, App user: `app_user_cv14`
- Backup S3: bucket `cassiopeiav14-galera-backups` na `x12mon:9000`

---

## Status maszyn poprzedniej generacji

W celu zwolnienia pamięci RAM hiperwizora (dla 12 nowych maszyn) zatrzymano przez API Proxmoxa (`qm stop`):
- `orionv14-r10`: VMID **10025 – 10028** (status: stopped, dyski i dane zachowane)
- `cassiopeiav13-r9`: VMID **10029 – 10032** (status: stopped, dyski i dane zachowane)
- Poprzednia platforma `xenonv11` (`10001–10003`) oraz `x10mon` (`10000`) pozostały nienaruszone.

---

## Dowody akceptacji zmierzone po budowie

1. **Routing ProxySQL na `xenonv12` (`192.168.1.74:6033`)**:
   - Hostgroup 780 (`orionv15`): `o15db3` ONLINE (writer), `o15db1`/`o15db2` ONLINE (readers).
   - Hostgroup 820 (`cassiopeiav14`): `c14db3` ONLINE (writer), `c14db1`/`c14db2` ONLINE (readers).
2. **Kontrakt aplikacyjny (`probe-app-conformance.py` z `x12app`)**:
   - `orionv15-r10`: **PASS** (`TLS_AES_256_GCM_SHA384` przez VIP, read-your-writes, rollback/commit).
   - `cassiopeiav14-r9`: **PASS** (`TLS_AES_256_GCM_SHA384` przez VIP, read-your-writes, rollback/commit).
3. **Monitoring PMM 3.9 na nowym `x12mon` (`probe-pmm-native.py`)**:
   - `orionv15-r10`: **PASS** (3 węzły, 3 node_exportery, 3 serwisy MySQL, QAN, reguły ISC-47 zielone).
   - `cassiopeiav14-r9`: **PASS** (3 węzły, 3 node_exportery, 3 serwisy MySQL, QAN, reguły ISC-47 zielone).
4. **Szyfrowany backup i odtworzenie (Restore drill na nowym MinIO `x12mon`)**:
   - `orionv15-r10`: **PASS** (backup do bucketa `orionv15-galera-backups`, restore i weryfikacja na `o15r1`).
   - `cassiopeiav14-r9`: **PASS** (backup do bucketa `cassiopeiav14-galera-backups`, restore i weryfikacja na `c14r1`).
5. **Weryfikacja niezmienników statycznych**:
   - `probe-address-collision`: **PASS** (44 adresy w 11 klastrach bez żadnej kolizji).
   - `probe-proxysql-tenancy`: **PASS** (pełna rozłączność hostgroupów i użytkowników).
   - Wszystkie 9 bramek `make verify-*`: **PASS**.
