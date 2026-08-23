# Stan infrastruktury

**Snapshot bazowy:** 2026-08-02 01:40 UTC — topologia, VMID, adresy.
**Warstwy nalozone pozniej:** 2026-08-05 (TLS full na r9), 2026-08-15 (runda 3
TLS, limity systemd, dekompozycja runnera, rotacja poswiadczen) — zaznaczone
w miejscach, ktorych dotycza.
**Zebrany z:** `terraform`, `ansible`, ProxySQL admin, PMM, MinIO, `qm list`.

> Ten plik jest **datowanym zdjęciem**, nie źródłem prawdy. Źródłem prawdy dla
> zamiaru są `clusters/<name>/` i `terraform/<name>/`; dla rzeczywistości —
> hypervisor.

## Flota `finalclaude` — działa

Stara flota (18 VM, prefiks `claude-*`) została skasowana 2026-08-02; stan
sprzed w `docs/records/2026-08-02-pre-teardown.md`. Obecna powstała w całości
z kodu, etapami — `docs/plans/from-scratch-revalidation.md`.

**12 VM, wszystkie w puli `claude-isa`. PMM Server został zaktualizowany do 3.9.1; backup danych przed zmianą obrazu: `pmm-data-backup-20260823-133353` (2.5G).**

### Warstwa wspólna — `terraform/shared/`

Nie należy do żadnego klastra. Jedna para ProxySQL w HA obsługuje **całą flotę**,
obecną i przyszłą.

| Host | VMID | IP | Rola | vCPU | RAM |
|---|---:|---|---|---:|---:|
| `fcinfra` | 9400 | 192.168.1.130 | PMM 3.9.1 + MinIO + maildev | 4 | 5120 MB |
| `fcp1` | 9401 | 192.168.1.131 | ProxySQL — **trzyma VIP** | 1 | 3072 MB |
| `fcp2` | 9402 | 192.168.1.132 | ProxySQL — BACKUP | 1 | 3072 MB |
| — | — | **192.168.1.139:6033** | VIP Keepalived — wspólny endpoint | — | — |
| `fcapp` | 9403 | 192.168.1.134 | host aplikacyjny (klient, sondy z perspektywy aplikacji) | 2 | 3072 MB |

Cyklem życia tych hostów zarządza **wyłącznie** `make platform-*`
(`platform/shared/`). Żaden klaster nie jest ich właścicielem — patrz niżej.

### PMM upgrade — 2026-08-23

PMM Server został zaktualizowany z 3.9.0 do **3.9.1** przez `make platform-infra`,
po backupie trwałego volume `isa-pmm-data` do `pmm-data-backup-20260823-133353`
(2.5G; katalogi Grafana i VictoriaMetrics zweryfikowane). Klienci zostały
zaktualizowane najpierw na `fcp1/fcp2` (EL10), następnie na `n17g1-3` (EL9),
zgodnie z zasadą PMM Server >= PMM Client. `f10` pozostaje agentless.
Kolejne zmiany przypiętego obrazu są fail-closed: `platform-infra` rozpoznaje
różnicę obrazu i tworzy świeży volume backupu z wersją oraz timestampem dla
każdej próby. Źródłowy `isa-pmm-data` jest montowany read-only; niekompletna
kopia usuwa wyłącznie volume bieżącej próby i uruchamia stary PMM. Dopiero
marker oraz kontrola katalogów danych pozwalają na Compose recreate.


Nie zastosowano hurtowo porad z `VolkanSah/optimize-MySQL-MariaDB`: są ogólnymi
przykładami dla dedykowanych serwerów, a n17 ma 3G RAM na węzeł, Galerę, SST,
backup i QAN. Obowiązująca konfiguracja (`768M` buffer pool, `256M` redo,
`gcache=256M`, `max_connections=100`) jest ograniczona pomiarem i bramkami.
Zmiany strojenia powinny być pojedynczym parametrem, po baseline i benchmarku;
nie włączamy `innodb_dedicated_server`, query cache ani `innodb_flush_log_at_trx_commit=1`
bez osobnej decyzji wymagań trwałości.

### `finalclaude-r10` — Rocky 10, najemca warstwy wspólnej

MariaDB 11.4.12 (`el10`), `wsrep_cluster_name: fc10_galera`, `tls=disabled`.
Hostgroupy ProxySQL **10/20/30/40**, użytkownik `app_user`.

| Host | VMID | IP | Rola | RAM |
|---|---:|---|---|---:|
| `f10g1` | 9410 | .140 | galera + scheduler backupu | 3072 MB |
| `f10g2` | 9411 | .141 | galera | 3072 MB |
| `f10g3` | 9412 | .142 | galera | 3072 MB |
| `f10r1` | 9413 | .143 | restore (własny) | 2560 MB |

### `newclaude17-r9` — Rocky 9, najemca warstwy wspólnej

MariaDB 11.4.12 (`el9`), `wsrep_cluster_name: n17_galera`, **`tls.mode: full`**
(replikacja Galera i SST szyfrowane, certy per węzeł ze wspólnego CA klastra).
Hostgroupy ProxySQL **850/860/870/880**, użytkownik `app_user_n17`.

| Host | VMID | IP | Rola | RAM |
|---|---:|---|---|---:|
| `n17g1` | 9550 | .144 | galera + scheduler backupu | 3072 MB |
| `n17g2` | 9551 | .145 | galera | 3072 MB |
| `n17g3` | 9552 | .146 | galera | 3072 MB |
| `n17r1` | 9553 | .147 | restore (własny) | 2560 MB |


Poprzednicy (`finalclaude-r9`, `newclaude8-r9` … `newclaude15-r9`) zostali
zniszczeni po zamknięciu swoich cykli — historia w `docs/records/`.

## Jak działa jedna para ProxySQL dla dwóch klastrów

ProxySQL trzyma `mysql_servers`, `mysql_galera_hostgroups` i `mysql_users`
w tabelach **globalnych**, więc klastry rozdziela wyłącznie rozłączność
identyfikatorów:

```
mysql_galera_hostgroups (odczyt z zywego fcp1, 2026-08-21):
  writer / backup / reader / offline
     10  /   20   /   30   /   40    -> finalclaude-r10
    850  /  860   /  870   /  880    -> newclaude17-r9

mysql_users:  app_user -> hg 10       app_user_n17 -> hg 850 (n17)
```

Rozdział idzie **po użytkowniku, nie po porcie** — oba klastry współdzielą
`VIP:6033`, a o trafieniu decyduje konto, którym loguje się aplikacja.
Routing po porcie wymagałby zarządzania `mysql-interfaces` i restartu ProxySQL.

Warstwa wspólna **nie należy do żadnego klastra**. Opisuje ją
`platform/shared/` (`platform.yml` + `inventory.yml`), a jej cyklem życia
zarządzają wyłącznie cele `make platform-*`. Klastry są **najemcami**:
`make cluster-proxysql` rejestruje ich hostgroupy i użytkownika, i tylko tyle.

Do 2026-08-21 właścicielem warstwy był klaster Galera (`finalclaude-r10` miał
`proxysql.role: owner`), więc skasowanie tego klastra osierociłoby parę
ProxySQL, VIP, PMM i MinIO — pozostali najemcy jechaliby dalej na
infrastrukturze, której nikt nie może zaktualizować ani odtworzyć. Pole
`proxysql.role` już nie istnieje; jego powrotu pilnuje
`tests/validation/probe-proxysql-tenancy.py`, a niezależności warstwy od
węzłów bazy — `tests/validation/validate-platform.py` (offline) oraz
`tests/lab/probe-platform.py` (na żywym hoście).

## Dowody z żywej instalacji — 2026-08-21/22 (faza n16; n16 zderejestrowany i
## zniszczony 2026-08-22, zastąpiony przez newclaude17-r9 na .144-.147)

| Sprawdzenie | Wynik (n16, do czasu teardown) |
|---|---|
| Galera fc10 / n16 | `Primary`, `size=3`, `wsrep_ready=ON` — oba |
| Zapis przez VIP | `app_user` → `fc10_galera`, `app_user_n16` → `n16_galera` |
| VIP `.139` | wyłącznie na `fcp1` |
| Backup | `galera-newclaude16-r9-*` off-cluster w S3, `aes-256-cbc`, sha256 OK; ostatni przed teardown: `20260822-223752` |
| Drill restore | `success` na izolowanym `n16r1`, 1 wiersz zweryfikowany |
| PMM | warstwa: `shared-fcp1/2` + 2 eksportery ProxySQL; każdy najemca: 3 węzły |
| Reguły alertowe | najemca: `isa-<klaster>-*`; warstwa: `isa-shared-*`, rozłączne |
| Pomiar P2 | `degraded`: klient przez VIP `ERROR 2027`, backend `ERROR 1047`; rekord w `docs/records/` |
| Derejestracja n16 | PASS zero sierot (PMM/Grafana/ProxySQL/MinIO), 4 VM zniszczone, ZFS czysty |

### Dolozone 2026-08-15

| Sprawdzenie | Wynik |
|---|---|
| Limity ZYWYCH procesow | `/proc/<pid>/limits`: `nofile=1048576` dla `mariadbd` i `proxysql` na 8/8 hostach (bylo 32768 / 102400) |
| `TimeoutStartSec` | `infinity` na 6/6 wezlach Galera — pelny SST duzej bazy nie zostanie zabity po 15 min |
| TLS na r9 | `socket.ssl = YES`, brak `socket.dynamic` na 3/3; SST szyfrowany, potwierdzony wymuszonym pelnym transferem |
| SELinux / firewalld | `Enforcing` i `running` na 14/14 hostow; polityka firewalld bez dryfu (`--check` = `changed=0`) |
| Idempotencja | drugi `site.yml`: `changed=0` na 6/6 wezlow Galera |
| Runner backupu | 12 modulow, entrypoint 21 linii; backup + drill restore przeszly po dekompozycji |
| `wsrep_desync` | backup odsynchronizowuje wezel i przywraca `Synced` — `galera.desync` → `galera.resync` w dzienniku zdarzen |

## Limit zasobów

Operator: **max 5 GB RAM na VM**, podnoszone tylko na dowód.

- `fcinfra` 5 GB — PMM zajmował 1.4 GB z 3 GB przy zerze usług.
- ProxySQL 3 GB — preflight wymaga `ansible_memtotal_mb >= 2048`, a przydział
  2048 MB daje 1769 MB widzianych przez OS. W próg trzeba uderzyć z zapasem.
- Galera 3 GB, `innodb_buffer_pool_size: 768M` — zapas na `mariabackup` w SST.

## Poza tą automatyzacją — nie dotykać

| Grupa | VMID | Uwaga |
|---|---|---|
| Poprzednicy ISA | `9010-9012`, `9040-9042`, `9050`, `9060` | poza pulą `claude-isa`, sprzed tej automatyzacji |
| RKE2 lab | `9000`, `9201-9235` | — |
| GitLab | `9301` | — |
| `qoder-*` | `9601-9620`, `9999` | przenumerowane z `95xx` 2026-08-02 przez kogoś innego |

## Reguła stała

Każdy zasób tworzony przez tę automatyzację należy do puli `claude-isa`.
Przynależność do puli to **asercja** („to jest nasze"), nigdy źródło listy do
skasowania.

## Jak odtworzyć ten raport

```bash
qm list | sort -k1 -n
pvesh get /pools/claude-isa --output-format json

make platform-verify

for c in finalclaude-r10 newclaude17-r9; do
  make cluster-health CLUSTER=$c
  make lab-monitoring-verify CLUSTER=$c
done

ansible fcp1 -i platform/shared/inventory.yml -m shell -a \
  'mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf -h127.0.0.1 -P6032 -uadmin -N -B \
   -e "SELECT hostgroup_id, COUNT(*) FROM runtime_mysql_servers WHERE status=\"ONLINE\" GROUP BY 1"'
```
