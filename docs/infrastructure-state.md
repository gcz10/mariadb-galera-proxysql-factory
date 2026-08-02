# Stan infrastruktury

**Snapshot:** 2026-08-02 01:40 UTC
**Zebrany z:** `terraform`, `ansible`, ProxySQL admin, PMM, MinIO, `qm list`.

> Ten plik jest **datowanym zdjęciem**, nie źródłem prawdy. Źródłem prawdy dla
> zamiaru są `clusters/<name>/` i `terraform/<name>/`; dla rzeczywistości —
> hypervisor.

## Flota `finalclaude` — działa

Stara flota (18 VM, prefiks `claude-*`) została skasowana 2026-08-02; stan
sprzed w `docs/records/2026-08-02-pre-teardown.md`. Obecna powstała w całości
z kodu, etapami — `docs/plans/from-scratch-revalidation.md`.

**11 VM, wszystkie w puli `claude-isa`.**

### Warstwa wspólna — `terraform/shared/`

Nie należy do żadnego klastra. Jedna para ProxySQL w HA obsługuje **całą flotę**,
obecną i przyszłą.

| Host | VMID | IP | Rola | vCPU | RAM |
|---|---:|---|---|---:|---:|
| `fcinfra` | 9400 | 192.168.1.130 | PMM 3.8.1 + MinIO + maildev | 4 | 5120 MB |
| `fcp1` | 9401 | 192.168.1.131 | ProxySQL — **trzyma VIP** | 1 | 3072 MB |
| `fcp2` | 9402 | 192.168.1.132 | ProxySQL — BACKUP | 1 | 3072 MB |
| — | — | **192.168.1.133:6033** | VIP Keepalived — wspólny endpoint | — | — |

### `finalclaude-r10` — Rocky 10, owner warstwy wspólnej

MariaDB 11.4.12 (`el10`), `wsrep_cluster_name: fc10_galera`, `tls=disabled`.
Hostgroupy ProxySQL **10/20/30/40**, użytkownik `app_user`.

| Host | VMID | IP | Rola | RAM |
|---|---:|---|---|---:|
| `f10g1` | 9410 | .140 | galera + scheduler backupu | 3072 MB |
| `f10g2` | 9411 | .141 | galera | 3072 MB |
| `f10g3` | 9412 | .142 | galera | 3072 MB |
| `f10r1` | 9413 | .143 | restore (własny) | 2560 MB |

### `finalclaude-r9` — Rocky 9, konsument warstwy wspólnej

MariaDB 11.4.12 (`el9`), `wsrep_cluster_name: fc9_galera`, `tls=disabled`.
Hostgroupy ProxySQL **110/120/130/140**, użytkownik `app_user_fc9`.

| Host | VMID | IP | Rola | RAM |
|---|---:|---|---|---:|
| `f9g1` | 9420 | .150 | galera + scheduler backupu | 3072 MB |
| `f9g2` | 9421 | .151 | galera | 3072 MB |
| `f9g3` | 9422 | .152 | galera | 3072 MB |
| `f9r1` | 9423 | .153 | restore (własny) | 2560 MB |

## Jak działa jedna para ProxySQL dla dwóch klastrów

ProxySQL trzyma `mysql_servers`, `mysql_galera_hostgroups` i `mysql_users`
w tabelach **globalnych**, więc klastry rozdziela wyłącznie rozłączność
identyfikatorów:

```
runtime_mysql_servers (ONLINE):
  hostgroup 10  -> 1   writer  fc10        hostgroup 110 -> 1   writer  fc9
  hostgroup 20  -> 2   backup  fc10        hostgroup 120 -> 2   backup  fc9

mysql_users:  app_user -> hg 10       app_user_fc9 -> hg 110
```

Rozdział idzie **po użytkowniku, nie po porcie** — oba klastry współdzielą
`VIP:6033`, a o trafieniu decyduje konto, którym loguje się aplikacja.
Routing po porcie wymagałby zarządzania `mysql-interfaces` i restartu ProxySQL.

Dokładnie jeden klaster na parze ma `proxysql.role: owner` — instaluje pakiety
na węzłach wspólnych, zarządza VIP-em i rejestruje je w PMM. Konsument robi
tylko `f7`. Pilnuje tego `tests/validation/probe-proxysql-tenancy.py`.

## Dowody z żywej instalacji

| Sprawdzenie | Wynik |
|---|---|
| Galera fc10 / fc9 | `Primary`, `size=3`, `wsrep_ready=ON` — oba |
| Zapis przez VIP | `app_user` → `fc10_galera`, `app_user_fc9` → `fc9_galera` |
| VIP | wyłącznie na `fcp1` |
| Backup | `galera-finalclaude-r10-*` i `galera-finalclaude-r9-*`, zaszyfrowane, sha256 OK |
| Drill restore | oba `success`, 1 baza / 1 tabela / 1 wiersz |
| PMM | owner: 5 węzłów + 2 eksportery ProxySQL; konsument: 3 węzły, 0 |
| Reguły alertowe | 8 na klaster, namespace `isa-fc10-*` / `isa-fc9-*` |

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

for c in finalclaude-r10 finalclaude-r9; do
  make cluster-health CLUSTER=$c
  make lab-monitoring-verify CLUSTER=$c
done

ansible fcp1 -i clusters/finalclaude-r10/inventory.yml -m shell -a \
  'mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf -h127.0.0.1 -P6032 -uadmin -N -B \
   -e "SELECT hostgroup_id, COUNT(*) FROM runtime_mysql_servers WHERE status=\"ONLINE\" GROUP BY 1"'
```
