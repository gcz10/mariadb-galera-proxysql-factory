# Stan infrastruktury

**Snapshot:** 2026-08-01 23:32 UTC
**Zebrany z:** `qm list` + `pvesm status` na hoście PVE, `terraform.tfstate` per moduł, PMM API, `clusters/*/`.

> Ten plik jest **datowanym zdjęciem**, nie źródłem prawdy. Źródłem prawdy dla
> zamiaru są `clusters/<name>/` i `terraform/<name>/`; dla rzeczywistości —
> hypervisor. Rozjazd między nimi jest wypisany w sekcji „Rozbieżności".
> Sekcja „Jak odtworzyć ten raport" na końcu podaje dokładne komendy.

## Hypervisor

| | |
|---|---|
| Host | `pve` — 192.168.1.181 (Proxmox VE 9.2.2) |
| CPU / RAM | 16 rdzeni, 93 GB (66 GB w użyciu), load 6.8 |
| Uptime | 4 dni |
| `local-zfs` | 449 GB, **15.6%** zajęte |
| `local` (dir) | 385 GB, 1.6% zajęte |
| `data1` | 922 GB, **98.2% zajęte — 16 GB wolnego** |

`data1` to zagrożenie pojemnościowe: tam leżą dyski klastrów `claude-r9g`
i `claude-r9t`, które są wyłączone, ale zajmują miejsce.

Na hoście stoją **34 VM**, z czego **18 należy do tego repozytorium**. Pozostałe
(RKE2 lab `9201-9235`, GitLab `9301`, stack `qoder-*` `9501-9999`) nie są przez
nie zarządzane — nie ruszaj ich playbookami ani modułami Terraform stąd.

## Klastry ISA

### `claude-r10c` — jedyny aktywny klaster bazodanowy

Rocky Linux 10.2, MariaDB 11.4.12, galera-4 26.4.27, TLS wyłączone.
`wsrep_cluster_name: r10c_galera`, `Primary/Synced`, `size=3`.

| Host | VMID | IP | Rola |
|---|---:|---|---|
| `gnode7` | 9193 | 192.168.1.71 | galera, scheduler backupu |
| `gnode8` | 9194 | 192.168.1.72 | galera |
| `gnode9` | 9195 | 192.168.1.73 | galera, aktywny writer |

### Warstwa dostępowa i wspierająca

Formalnie należy do modułu `terraform/claude-r10b/`, ale **obsługuje
`claude-r10c`**. Oba klastry deklarują ją w swoich inventory; właścicielem
konfiguracji jest ten, dla którego ostatnio uruchomiono `f7_proxysql`/`f8_keepalived`.

| Host | VMID | IP | Stan |
|---|---:|---|---|
| `pnode1` | 9123 | 192.168.1.44 | running — `proxysql` + `keepalived` active |
| `pnode2` | 9124 | 192.168.1.45 | running — `proxysql` + `keepalived` active |
| `rnode1` | 9125 | 192.168.1.46 | running — MariaDB **inactive**, co jest poprawne: drill restore czyści datadir i zostawia serwer zatrzymany |
| `infranode` | 9126 | 192.168.1.47 | running — kontenery `pmm-server`, `minio`, `maildev` |

Endpoint aplikacyjny: **VIP `192.168.1.50:6033`** (Keepalived) → ProxySQL → writer.

### Klastry wyłączone (dyski zachowane)

W kodzie mają `started = false`, więc `terraform apply` ich nie wystartuje.
Przywrócenie = usunięcie tej flagi i `apply`.

| Klaster | Wyłączone | Nadal działa |
|---|---|---|
| `claude-r10b` | `gnode4-6` (9130-9132, .51-.53) | warstwa dostępowa wyżej |
| `claude-r9g` | `g9node1-3` (9150-9152, .17-.19) | `r9node1` (9153) |
| `claude-r9t` | `g9tnode1-3` (9170-9172, .54-.56) | `r9tnode1` (9173) |

### Definicje bez infrastruktury

`claude-pve` (EL9, .10-.20), `claude-r10` (EL10, .31-.40), `claude-r10t`
(EL10 TLS full, .54-.60) — kod i pusty stan Terraform, zero VM.
`example-cluster` to szablon, `lab-cluster` i `lab2-cluster` to laboratorium
dockerowe (172.28.0.x / 172.29.0.x), bez modułów Terraform.

## Monitoring

PMM 3.8.1 na `192.168.1.47`, jedyny zarejestrowany klaster to `r10c-galera`:

- 5 generic nodes (3 galera + 2 proxysql), 16/16 eksporterów `up`
- `mysql_up=1` na trzech węzłach, `wsrep_cluster_size=3`
- 8 reguł alertowych `isa-r10c-galera-*` w folderze `isa-alerts-r10c-galera`
- metryki backupu i świeżości drilla żywe (`galera_backup_last_run_success=1`)

Uwaga interpretacyjna: `up` mierzy żywotność **procesu eksportera**, nie bazy —
PMM-managed `mysqld_exporter` raportuje `up=1` także dla zgaszonego węzła.
Sygnałem bazy jest `mysql_up` i metryki `wsrep_*`. Każdy agent ma trzy serie
(`_hr`/`_mr`/`_lr`) — to rozdzielczości scrape'u, nie duplikaty.

## Backup

MinIO na `192.168.1.47:9000`, bucket per klaster, scoped service account per
bucket (nie root), marker właściciela `galera-backup-owner.json`.

| Bucket | Właściciel | Uwaga |
|---|---|---|
| `r10c-galera-backups` | `claude-r10c` | aktywny, backup + drill zaliczone |
| `r10b-galera-backups` | `claude-r10b` | ostatni backup 2026-08-01 02:00 (cron sprzed wyłączenia węzłów) |
| `r10t-galera-backups`, `r9g-galera-backups` | klastry wyłączone | — |
| `r10n-galera-backups` | **klaster nie istnieje** | do usunięcia po decyzji |
| `decoy-bucket-test` | — | artefakt testu ownership z 2026-07-29 |

Cron backupu: `/etc/cron.d/galera-backup-claude-r10c` na `gnode7`, `0 2 * * *`,
uruchamiany jako dziecko `systemd-cat` (kod wyjścia dociera do crona).

## Rozbieżności wymagające decyzji

1. **`claude-r9t`: kod ≠ rzeczywistość.** `terraform/claude-r9t/main.tf`
   deklaruje VMID `9180-9185` i IP `.61-.66`, a w Proxmoksie żyją `9170-9173`
   na `.54-.57`. `terraform apply` odtworzyłby wszystkie cztery VM pod nowymi
   numerami. Rozjazd powstał przy rozdzielaniu adresacji `claude-r10t`
   i `claude-r9t`. Wyjścia: przenumerować żywe maszyny albo cofnąć kod — oba
   destrukcyjne, wymagają decyzji operatora.
2. **`data1` na 98%.** Zwolnienie miejsca oznacza usunięcie dysków wyłączonych
   klastrów, czyli utratę możliwości ich wskrzeszenia.
3. **Trzy moduły z pustym stanem** (`claude-pve`, `claude-r10`, `claude-r10t`) —
   do zachowania jako szablony albo do usunięcia.
4. **Bucket `r10n-galera-backups` i `decoy-bucket-test`** — pozostałości,
   nie kasowane bez zgody, bo to dane.
5. **Współdzielony ProxySQL.** `clusters/claude-r10b/inventory.yml` nadal
   deklaruje `gnode4-6`. Uruchomienie `make cluster-proxysql CLUSTER=claude-r10b`
   przepnie żywy VIP na wyłączone węzły.

## Jak odtworzyć ten raport

```bash
# VM i storage na hypervisorze (SSH jako root@192.168.1.181)
qm list | sort -k1 -n
pvesm status

# Zamiar w repo: co deklaruje każdy klaster
for c in clusters/*/cluster.yml; do
  python3 -c "import yaml,sys;d=yaml.safe_load(open('$c'));print(d['cluster']['name'], d['platform']['rocky_linux_major'] if 'platform' in d else '-', d['tls']['mode'])"
done

# Co Terraform uważa, że posiada
for m in terraform/*/; do
  printf '%s: ' "$m"; terraform -chdir="$m" state list 2>/dev/null | wc -l
done

# Zdrowie żywego klastra
make cluster-health CLUSTER=claude-r10c

# PMM: zarejestrowane obiekty i reguły
curl -sk -u admin:$PMM_ADMIN_PASSWORD https://192.168.1.47/v1/inventory/nodes
curl -sk -u admin:$PMM_ADMIN_PASSWORD https://192.168.1.47/graph/api/v1/provisioning/alert-rules
```
