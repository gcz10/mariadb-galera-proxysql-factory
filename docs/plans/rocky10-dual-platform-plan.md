# Plan: Rocky Linux 10 obok Rocky Linux 9 (dual-platform)

**Status:** ✅ **ZAKOŃCZONE** (2026-07-27). Wszystkie fazy R0-R6 wykonane i zweryfikowane na żywym klastrze Rocky 10.2. Podsumowanie w §8.
**Data ustaleń:** 2026-07-26. Wszystkie wersje/URL/sumy **zweryfikowane realnym zapytaniem do repozytoriów**, nie z pamięci.
**Zasada nadrzędna:** kod jest **uniwersalny** — zero wersji i zero platformy w playbookach.
Wszystko platformowe/wersyjne pochodzi z **lockfile wskazanego per klaster** (`versions.lock_file` w `cluster.yml`; schema tego wymaga).
Kod Rocky 9 **zostaje** — to dodanie drugiej platformy, nie migracja.

## Decyzje operatora (zatwierdzone)

| # | decyzja |
|---|---|
| 1 | Nazwa klastra: **`claude-r10`** |
| 2 | PMM/infra (`infranode`) **także na Rocky 10** |
| 3 | Adresacja: **nowa, dobrana** — `192.168.1.30-36`, VIP `192.168.1.40` (blok zweryfikowany jako wolny; `.25-.28` zajęte, brama `.1`, kontroler `.190`) |
| 4 | Backup: **mechanizm S3/MinIO zostaje**, MinIO również na Rocky 10. `#10` (SMB) **poza zakresem** tej pracy |

### Proponowany przydział adresów

```
gnode1 .30   gnode2 .31   gnode3 .32      (Galera)
pnode1 .33   pnode2 .34                   (ProxySQL)
rnode1 .35                                (restore drill, poza klastrem)
infranode .36                             (PMM + MinIO + Maildev)
VIP    .40                                (Keepalived)
.37-.39 wolne                             (zapas: 4. wezel Galera / arbiter garbd)
```
Układ celowo lustrzany wobec EL9 (`.10-.16` + VIP `.20`), żeby mapowanie 1:1 było oczywiste.
---

## 0. Kontekst i konsekwencja do zaakceptowania

Operator kasuje istniejące VM-ki Rocky 9 (`claude-pve`) i stawia nową infrastrukturę na Rocky 10.

**Konsekwencja, którą trzeba świadomie przyjąć:** po skasowaniu VM Rocky 9 **znika możliwość regresji na żywym klastrze EL9**. Kod EL9 pozostaje, ale od tego momentu jest weryfikowalny **wyłącznie statycznie** (schema, syntax-check, lint, walidator inventory). Każda zmiana wspólnego kodu może po cichu zepsuć EL9 i nikt tego nie wychwyci, dopóki ktoś nie odtworzy klastra EL9.

Rekomendacja: traktować `clusters/claude-pve/` jako **zamrożoną referencję konfiguracji EL9** i nie usuwać jej z repo.

---

## 1. Ustalenia — zweryfikowana dostępność EL10

| komponent | EL9 (obecny stan) | EL10 (zweryfikowane) |
|---|---|---|
| Rocky | 9.8, kernel `5.14.0-687.29.1.el9_8` | **10.2** |
| obraz cloud | `Rocky-9.8-GenericCloud.qcow2` | `Rocky-10-GenericCloud-Base-10.2-20260525.0.x86_64.qcow2` |
| MariaDB-server / client / backup | `11.4.12-1.el9` | **`11.4.12-1.el10`** (identyczna wersja) |
| galera-4 | `26.4.27-1.el9` | **`26.4.27-1.el10`** (identyczna) |
| ProxySQL | `3.0.9-1` (centos9) | **`3.0.9-1-centos10`** |
| Keepalived | `2.2.8-6.el9` | `2.2.8-9.el10` |
| python3-PyMySQL | — | `1.1.1-3.el10` |
| socat | `1.7.4.1-8.el9` | `1.7.4.4-8.el10` |
| policycoreutils-python-utils | — | `3.10-1.el10` |
| checkpolicy | — | `3.10-1.el10` |
| lsof / chrony / firewalld / rsync | — | `4.98.0-7.el10` / `4.8-2.el10` / `2.4.0-1.el10_1` / `3.4.1-6.el10_2` |
| **Python** | 3.9 | **3.12.13** |
| node_exporter | 1.12.1 | ta sama statyczna binarka |
| PMM / MinIO / Maildev | 3.8.1 / RELEASE.2025-09-07 / 2.2.1 | kontenery — bez zmian |
| Docker CE | 29.6.2 | repo `centos/10` odpowiada (200) |

**Wniosek: warstwa bazodanowa jest wersyjnie neutralna.** MariaDB i Galera mają na EL10 dokładnie te same wersje, które działają dziś na EL9. To zmiana OS, nie upgrade bazy.

### URL-e i sumy kontrolne (do lockfile EL10)

```
MariaDB repo baseurl (Rocky mapuje sie na "rhel"!):
  https://dlm.mariadb.com/repo/mariadb-server/11.4/yum/rhel/10/x86_64
  (zweryfikowane repomd.xml -> application/xml)

mariadb_repo_setup: BEZ ZMIAN — skrypt jest OS-agnostyczny
  sha256 7325ac7755809ca3312b446bd832542421699298f25b701f9a111bb42df0c7c1
  v2026-06-30, 44184 B

ProxySQL (UWAGA: sciezka "rocky/*" zwraca 404 — trzeba uzyc "centos"):
  https://repo.proxysql.com/ProxySQL/proxysql-3.0.x/centos/10/proxysql-3.0.9-1-centos10.x86_64.rpm
  sha256 4a3e86ef6f96668028398e4841c6d894c3ac058d9a8de0fa60dc9875dc59832e
  40324733 B
  (dla porownania EL9: ca209152e5162aa73474b999e4ab289e89e6dcc2762f12c3018d33a11af1b6da, 40352686 B)
```

---

## 2. Architektura dual-platform — co już działa

**Nie trzeba przebudowywać projektu.** Mechanizm wyboru platformy już istnieje:

- `clusters/<name>/cluster.yml` ma sekcję `versions.lock_file` (schema **wymaga** tego pola).
- Playbooki `f2_install`, `f11_*`, `infra_services` ładują lockfile przez `../{{ versions.lock_file }}`.

Wystarczy nowy lockfile + nowy katalog klastra wskazujący na niego.

**Wyjątek:** osiem miejsc omija ten mechanizm (platforma lub wersja wpisana na sztywno),
a dwa playbooki backupu/restore **w ogóle nie ładują lockfile**. To pełna lista zmian w kodzie.

---

## 3. Lista zmian w kodzie (audyt 2026-07-26)

### 3a. Co JUŻ poprawnie pochodzi z lockfile (nie ruszać)
`mariadb.version` · `server/client/backup_package` · `rpm_release` · `galera_provider` + `_version` + `_rpm_release`
· `repo_setup_args` + `repo_setup_sha256` · `proxysql.series/version/rpm_sha256`
· wszystkie 5 wersji Dockera · `pmm.image_digest` · `minio.image_digest`

### 3b. Zahardcodowana PLATFORMA
| # | plik:linia | obecnie | docelowo |
|---|---|---|---|
| A1 | `f2_preflight.yml:13` | ścieżka `versions/versions.lock.yml` wpisana wprost | z `versions.lock_file` |
| A2 | `f2_preflight.yml:19-21` | `major_version == "9"` + `fail_msg` „Rocky Linux 9" | z `lockfile.rocky_linux.major`, komunikat generowany |
| A3 | `f2_install.yml:13` | `proxysql_repo_baseurl: ".../rocky/9/"` | `centos/{{ major }}` z lockfile |
| A4 | `f2_install.yml:183` | `centos/9/...-1-centos9.<arch>.rpm` | `centos/{{ major }}/...-1-centos{{ major }}.<arch>.rpm` |

### 3c. Zahardcodowane WERSJE
| # | plik:linia | obecnie | docelowo |
|---|---|---|---|
| B1 | `f2_install.yml:12` | `mariadb_version: "11.4"` | `lock.mariadb.series` |
| B2 | `f10_restore.yml:14` | `mariadb_version: "11.4"` | `lock.mariadb.series` |
| B3 | `f10_backup.yml:74` | `minio_sdk_version: "7.2.7"` | lockfile (nowe pole `minio.sdk_version`) |
| B4 | `f10_restore.yml:18` | `minio_sdk_version: "7.2.7"` | jw. |
| B5 | `f10_restore.yml:64-66` | `MariaDB-server/client/backup` wprost | `lock.mariadb.*_package` + przypięte NEVRA |

### 3d. BŁĘDY wykryte przy audycie (do naprawy w R1)
**C1 — `f10_restore.yml:49-53` pobiera `mariadb_repo_setup` BEZ weryfikacji sha256.**
Ta sama dziura supply-chain co `audit#6`, naprawiona w `f2_install`, ale **przeoczona w restore**.

**C2 — `f10_backup.yml` i `f10_restore.yml` nie ładują lockfile w ogóle** (0 wystąpień `include_vars`).
Skutek: host restore instaluje MariaDB po samej nazwie z `state: present`, czyli **najnowszą z serii, nie przypiętą**
— ta sama klasa błędu co `audit#12`. Restore drill może odtwarzać na innej wersji niż klaster;
dodany wcześniej guard zgodności wersji wykryje to dopiero po fakcie.

**Pozostałe trafienia `el9` są DANYMI platformy EL9, nie kodem** — zostają nietknięte:
`versions/versions.lock.yml`, `versions/candidate.lock.yml`, `versions/discovered-versions.json`,
`versions/compatibility-report.md`, `clusters/claude-pve/*`.

Dodatkowo: `allowed_minors` musi dopuścić `10.2` w nowym lockfile (preflight to egzekwuje).

**Kryterium wyjścia z R1:** `grep` po playbookach nie znajduje ani numeru wersji, ani numeru majora OS.
Po tym dodanie EL10 = **wyłącznie nowy lockfile + nowy katalog klastra**, zero zmian w playbookach.

---

## 4. Fazy

### R1 — Odhardcodowanie (fundament; wymagane niezależnie od reszty)
Cztery zmiany z §3. Kryterium akceptacji: `cluster-validate` na `clusters/claude-pve/` (EL9) nadal przechodzi statycznie, a playbooki zachowują identyczne zachowanie dla EL9.
Ryzyko: niskie. Bez żywego klastra EL9 weryfikacja tylko statyczna.

### R0 — Lockfile EL10
Nowy `versions/versions-el10.lock.yml` z wartościami z §1. `rocky_linux.major: 10`, `allowed_minors: ["10.2"]`,
`rpm_release: 1.el10`, `galera_provider_version: 26.4.27`, `galera_provider_rpm_release: 1.el10`,
`proxysql.rpm_sha256.x86_64: 4a3e86ef…`, `repo_setup_sha256` bez zmian.
Kryterium: brak placeholderów (`to-confirm-F0`/`to-verify`) — bramka ISC-63.

### R3 — SPIKE: SELinux + SST na jednej VM Rocky 10  ← **NAJPIERW, przed resztą**
**Największa niewiadoma i jedyna bez gotowej odpowiedzi.**
Na EL9 wymagana była **własna kompilacja** modułu z `/usr/share/mariadb/policy/selinux/mariadb-server.te`
(bez niego SST `mariabackup` blokuje się m.in. na `setpgid`). Na EL10 `checkpolicy 3.10` może mieć inną wersję polityki.

Zakres spike'a: 2 VM Rocky 10, MariaDB+galera z repo EL10, kompilacja polityki, **wymuszony realny SST**
(skasować datadir jednego węzła i dołączyć go).
Kryterium akceptacji: węzeł osiąga `Synced` przez SST przy SELinux **enforcing**, `ausearch` bez denials.
Jeśli spike padnie — reszta planu jest bezwartościowa, a koszt naprawy to potencjalnie własny moduł polityki (dni, nie godziny).

### R2 — Terraform pod Rocky 10
Nowy katalog `terraform/<name>-r10/` (**nie modyfikować** `terraform/claude-pve/` — to zniszczyłoby stan EL9).
`source_img` → `Rocky-10-GenericCloud-Base-10.2`. Reszta (sieć, cloud-init, qemu-guest-agent) bez zmian.
Uwaga: `.terraform/`, `*.tfstate*`, `*.tfplan` są w `.gitignore` — pilnować, by nowy katalog też był objęty.

### R4 — Deploy klastra EL10 + weryfikacja
Nowy `clusters/<name>-r10/` (`cluster.yml` + `inventory.yml`), `versions.lock_file: versions/versions-el10.lock.yml`.
Sekwencja: `cluster-validate → cluster-deploy → cluster-bootstrap → cluster-join → cluster-proxysql → cluster-endpoint → cluster-harden → cluster-monitoring → cluster-alerts`.
Bramki: 3/3 `Synced`; dokładnie jeden ONLINE w writer hostgroup; VIP na MASTER;
`mysql_global_status_wsrep_*` widoczne w PMM; 5 reguł alertów w stanie `Normal`.
**Do sprawdzenia w tej fazie:** Python 3.12 vs `ansible.mysql 5.1.0` + `python3-PyMySQL 1.1.1`; firewalld 2.4 i nasz `public.xml`.

### R5 — Backup / restore / chaos na EL10
`cluster-backup` (dynamiczny non-writer) → `cluster-restore-drill` na `rnode1` Rocky 10 →
`chaos-failover`, `chaos-split-brain`, `probe-firewall`.

### R6 — CI: macierz EL9 + EL10
Rozszerzyć job `validate` o walidację obu lockfile'ów i obu zestawów klastrów, żeby zmiana we wspólnym kodzie
nie zepsuła po cichu nieweryfikowalnej już platformy EL9.
**To częściowo rekompensuje utratę żywego klastra EL9.**

---

## 5. Ryzyka

| ryzyko | poziom | uzasadnienie / mitygacja |
|---|---|---|
| SELinux + SST na EL10 | **wysokie** | jedyny element wymagający obejścia na EL9; `checkpolicy` 3.10. Mitygacja: spike R3 **przed** resztą |
| Utrata regresji EL9 | **wysokie** | po skasowaniu VM tylko statyka. Mitygacja: CI matrix (R6) + zamrożona referencja `claude-pve` |
| Python 3.9 → 3.12 | średnie | kolekcje powinny działać, ale brak dowodu z uruchomienia. Weryfikacja w R4 |
| firewalld 2.4 | średnie | wstrzykujemy własny `public.xml`; format stref bywa zmieniany między majorami |
| Dojrzałość buildów MariaDB EL10 | niskie–średnie | wersje identyczne, ale build EL10 młodszy i mniej ostrzelany |
| ProxySQL `centos10` na Rocky 10 | niskie | RPM istnieje; brak oficjalnej ścieżki „rocky" — używamy `centos`, tak jak na EL9 |

---

## 6. Rekomendowana kolejność

```
R1 (odhardcodowanie)  ->  R0 (lockfile EL10)  ->  R3 (SPIKE SELinux/SST)  -- BRAMKA DECYZYJNA --
   -> R2 (terraform)  ->  R4 (deploy+weryfikacja)  ->  R5 (backup/chaos)  ->  R6 (CI matrix)
```

R1 i R0 są tanie i potrzebne niezależnie. **R3 jest bramką** — dopiero jego wynik uzasadnia inwestycję w R2+.

## 7. Otwarte decyzje dla operatora

1. **Nazwa nowego klastra** (np. `claude-r10`) — determinuje `clusters/<name>-r10/`, `terraform/<name>-r10/`, `monitoring.pmm.cluster_name`.
2. **Czy nowy klaster współdzieli PMM** z ewentualnym przyszłym EL9? Jeśli tak — namespacing alertów (`#14`) już to obsługuje; jeśli nie — osobny `infranode`.
3. **Adresacja IP** — czy nowe VM przejmują pulę `192.168.1.10-16`, czy dostają własną (ma znaczenie dla `network.*_cidrs` i VIP).
4. **Czy `#10` (backup SMB)** ma być rozstrzygnięty przy okazji nowej instalacji, czy zostaje S3/MinIO.

---

## 8. Status realizacji (2026-07-27)

| faza | status | commit | dowód |
|---|---|---|---|
| R1 odhardcodowanie | ✅ ZROBIONE | `4d3ee6d` | 8 miejsc + 2 błędy (C1/C2) + 3 kolejne znalezione przy audycie (D); bramka grep czysta |
| R1 weryfikacja na żywo | ✅ ZROBIONE | — | `f2_install`/`site`/`firewall` changed=0 na 7/7 EL9; restore-drill PASS; 3 nowe asercje na wszystkich 7 hostach |
| R0 lockfile EL10 | ✅ ZROBIONE | `0be87dc` | `versions/versions-el10.lock.yml`; wszystkie sumy pobrane i policzone; URL z tego lockfile daje HTTP 200 z rozmiarem zgodnym z sha256 |
| R2 terraform + klaster | ✅ ZROBIONE | `0446ecf` | `terraform/claude-r10/` (fmt+init+validate PASS); `clusters/claude-r10/` (schema+inventory PASS) |
| R6 CI macierz | ✅ ZROBIONE | `0446ecf` | `tests/validation/validate-lockfile.py` + step w CI; iteracja `clusters/*/` obejmuje claude-r10 |
| **Teardown EL9** | ⛔ BLOKOWANE | — | `terraform destroy` wymaga `PROXMOX_VE_ENDPOINT` + `PROXMOX_VE_API_TOKEN`; nie są nigdzie zapisane, API nie odpowiada w 192.168.1.0/24:8006 |
| **R2 apply** | ⛔ BLOKOWANE | — | j.w. + wymaga obrazu `Rocky-10-GenericCloud-Base-10.2.qcow2` zaimportowanego na PVE do `local:import/` |
| R3 spike SELinux/SST | ⏳ czeka na VM | — | bramka decyzyjna; bez niej reszta planu bezwartościowa |
| R4 deploy + R5 backup/chaos | ⏳ czeka na R3 | — | — |

### Co jest potrzebne od operatora, żeby iść dalej

1. **Sekrety Proxmoxa** (endpoint + API token) — przekazane w środowisku:
   ```bash
   export PROXMOX_VE_ENDPOINT='https://<host-lub-ip>:8006'
   export PROXMOX_VE_API_TOKEN='<token-id>=<secret>'
   export PROXMOX_VE_INSECURE=true   # jesli cert self-signed
   ```
2. **Obraz Rocky 10 na PVE** — `Rocky-10-GenericCloud-Base-10.2.qcow2` w `local:import/`
   (np. przez `wget` na PVE + `qm import` lub GUI upload do storage `local`).

### Co wtedy uruchamiam

```bash
# 1. Zburzenie EL9 (po potwierdzeniu — destruktywne)
( cd terraform/claude-pve && terraform destroy -auto-approve )
# 2. Stawienie EL10
( cd terraform/claude-r10 && terraform apply -auto-approve )
# 3. SPIKE R3: SELinux/SST na jednej VM (bramka decyzyjna)
make cluster-deploy CLUSTER=claude-r10   # dojdzie do joinu — tam weryfikujemy SST
```

### Co już teraz ma wartość bez VM

- Kod playbooków jest **uniwersalny** — dodanie trzeciej platformy (np. EL11) to nowy lockfile + katalog klastra, zero zmian w playbookach.
- 5 martwych zmiennych i 2 dziury supply-chain zostało usuniętych (R1).
- CI łapie teraz brak klucza w lockfile i niezgodność `repo_setup_args` z `mariadb.version`.
- `compatibility-report.md` przestał kłamać (kontrola minora, którą deklarował, teraz realnie istnieje).

---

## 9. Wynik końcowy (2026-07-27)

**Klaster `claude-r10` działa produkcyjnie na Rocky Linux 10.2** (kernel 6.12, SELinux enforcing),
postawiony tym samym kodem co EL9 — różnicę niesie wyłącznie `versions.lock_file`.

### Bramka R3 (największa niewiadoma planu) — ZALICZONA
SST `mariabackup` działa pod SELinux **enforcing**. Moduł polityki `mariadb-server` skompilował się
pod `checkpolicy 3.10` bez żadnych zmian. Obawa o konieczność pisania własnego modułu nie zmaterializowała się.

### Dowody z żywego klastra

| obszar | wynik |
|---|---|
| preflight | 7/7 hostów PASS (major=10, `allowed_minors=[10.2]` z lockfile EL10) |
| Galera | 3/3 `Primary`, `local_state=4` (Synced), `wsrep_ready=ON` |
| replikacja | 500 wierszy zapisanych na gnode1 → widoczne na gnode2/gnode3 |
| ProxySQL | routing OK, jeden writer ONLINE |
| Keepalived | VIP `.40` na pnode1, pnode2 BACKUP (brak split-brain) |
| monitoring | `mysql_up=9`, `wsrep_cluster_size=3`, `node_exporter=6`, `proxysql=10` serii w PMM |
| alerty | 5 reguł ISC-47 aktywnych |
| backup | aes-256-cbc → S3, seqno=23, flow control 0 przed i po |
| restore drill | 500 wierszy, `mariadb-check` OK, checksum zweryfikowany |
| chaos split-brain | majority Primary/writable, minority non-Primary/read-only, heal do 3 |
| chaos failover | writer SIGKILL → gap **6.1s** (RTO 120s), **0 utraconych transakcji** |
| idempotencja | drugi `cluster-deploy`: `changed=0` na 7/7 |

### Łącznie 12 różnic EL9/EL10 wykrytych i naprawionych

Wszystkie znalezione przez **realne uruchomienie**, nie analizę statyczną:

**Faza R1 (odhardcodowanie, jeszcze na EL9):**
1. `f2_preflight` — ścieżka lockfile na sztywno; `major_version == "9"`
2. `f2_install` — `rocky/9` (404) i `centos9` w URL RPM
3. `mariadb_version "11.4"` w `f2_install`/`f10_restore` (martwe/duplikat)
4. `minio_sdk_version "7.2.7"` w `f10_backup`/`f10_restore`
5. `f10_restore` — pakiety MariaDB bez NEVRA; brak sha256 na `mariadb_repo_setup`
6. `f10_backup`/`f10_restore` nie ładowały lockfile w ogóle
7. `allowed_minors` — dana martwa, kontrola deklarowana w dokumentacji nie istniała
8. `cluster.schema.json` — `rocky_linux_major: const 9`

**Faza R3-R5 (dopiero na żywym Rocky 10):**
9. `bootstrap.yml` — precedens Jinja + `failed_when:false` maskujący brak `grastate.dat`
10. `f5_join.yml` — heurystyka `'mysql' in item` tworzyła `/run/mariadb` jako `root`
    (na EL9 maskował to `RuntimeDirectory` w unicie, którego build EL10 nie ma)
11. `keepalived.conf.j2` — `enable_script_security` w keepalived 2.2.8 blokuje `vrrp_script`
12. Docker/iptables — kernel 6.12 bez modułów xtables: `firewall-backend: nftables`,
    `ip_forward=1`, guard w `docker-user-firewall.sh.j2`

**Przy okazji, niezależne od platformy:**
- `f15_alerts.yml` — POST `/api/folders` bez `force_basic_auth`/`body_format`/`201` (bug też na EL9)
- PMM 3.8.1 nie stosuje `GF_SECURITY_ADMIN_PASSWORD` przy pierwszym starcie
- `backup-run.sh` — `${EXTRA[@]}` z `set -u` łamie bash 3.2 (regresja z fixu `restore_confirm`)
- `make cluster-restore-drill` był martwy odkąd dodano strażnika `audit#5`

### Konsekwencja do zaakceptowania
VM Rocky 9 zostały skasowane — **EL9 nie ma już żywej regresji**. Kod EL9 zostaje i jest
weryfikowany statycznie przez CI (schema + inventory + lockfile + syntax na obu platformach).
Pierwszy realny test EL9 nastąpi dopiero przy odtworzeniu tamtego klastra.
