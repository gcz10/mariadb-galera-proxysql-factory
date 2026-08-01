# Stan infrastruktury

**Snapshot:** 2026-08-01 23:50 UTC
**Zebrany z:** `qm list`, `pvesm status`, `zpool list`, `zfs get` na hoście PVE,
`terraform.tfstate` per moduł, PMM API, MinIO, `clusters/*/`.

> Ten plik jest **datowanym zdjęciem**, nie źródłem prawdy. Źródłem prawdy dla
> zamiaru są `clusters/<name>/` i `terraform/<name>/`; dla rzeczywistości —
> hypervisor. Rozjazd między nimi jest wypisany w sekcji „Rozbieżności".
> Sekcja „Jak odtworzyć ten raport" na końcu podaje dokładne komendy.

## Hypervisor

| | |
|---|---|
| Host | `pve` — 192.168.1.181 (Proxmox VE 9.2.2) |
| CPU / RAM | 16 rdzeni, 93 GB (68 GB w użyciu), load 3.7 |
| Uptime | 4 dni |
| `rpool` (= `local-zfs`) | 472 GB, 78.5 GB zaalokowane — **16%** |
| `local` (dir) | 385 GB, 1.6% zajęte |
| `data1` | 952 GB, 162 GB zaalokowane — **16%** fizycznie, ale **98.2% zarezerwowane** |

### `data1`: wyczerpana rezerwacja, nie miejsce

`pvesm status` pokazuje `data1` jako 98.23% zajęte (16.4 GB wolnego), a
`zpool list` — 162 GB z 952 GB, 790 GB wolnego. Oba są prawdziwe i mówią
o czym innym. Zvole utworzono jako **grube** (thick), więc ZFS rezerwuje pełny
`volsize` niezależnie od tego, ile faktycznie zapisano:

```
data1/vm-9150-disk-0  volsize         42949672960   (40 GiB)
data1/vm-9150-disk-0  refreservation  43623645184   (40.6 GiB — zarezerwowane)
data1/vm-9150-disk-0  referenced       2072633344   (1.9 GiB — faktycznie zapisane)
```

Praktyczny skutek: **Proxmox odmówi utworzenia nowego wolumenu na `data1`**,
bo nie ma z czego zarezerwować. Nie jest to natomiast zbliżająca się awaria
z braku miejsca — fizycznie pool jest w 16%.

Rezerwację `data1` trzymają w 36% nasze wyłączone klastry: 8 dysków `claude-r9g`
i `claude-r9t` po 40.6 GiB = 325 GiB rezerwacji przy ~15 GiB realnych danych.
Reszta to RKE2 lab, GitLab i `qoder-*`.

Zwolnienie rezerwacji **nie wymaga kasowania niczego**: `zfs set
refreservation=none <zvol>` zamienia wolumen na cienki i natychmiast oddaje
rezerwację. Kosztem jest to, że cienki wolumen może trafić na `ENOSPC` przy
zapisie, gdy pool faktycznie się zapełni — przy 790 GB wolnego ryzyko odległe.
Kasowanie dysków jest opcją mocniejszą i nieodwracalną.

### Maszyny

Na hoście stoją **43 VM**, z czego **18 należy do tego repozytorium**
(`9123-9126`, `9130-9132`, `9150-9153`, `9170-9173`, `9193-9195`).

Poza nimi stoi **8 zatrzymanych poprzedników ISA** sprzed tej automatyzacji —
`galera-01..03` (`9010-9012`), `galera10-01..03` (`9040-9042`),
`proxysql-01` (`9050`), `monitoring-01` (`9060`). Nie ma ich w żadnym
`inventory.yml` ani module Terraform; leżą na `rpool` i zajmują ~30 GB.
Repo ich nie odtworzy i nie skasuje — to ręczna decyzja.

Pozostałe (RKE2 lab `9000`, `9201-9235`, GitLab `9301`, stack `qoder-*`
`9501-9999`) nie są przez to repo zarządzane — nie ruszaj ich playbookami
ani modułami Terraform stąd.

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

- 6 generic nodes w Inventory: `pmm-server` + 3 galera + 2 proxysql
- 28/28 celów scrape'u `up`, zero down (`count(up==1)` == `sum(up)`); liczba
  obejmuje eksportery samego `pmm-server`. Zawężone do klastra:
  `count(up{cluster="r10c-galera"}==1)`
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
2. **`data1`: rezerwacja wyczerpana w 98%, pool fizycznie w 16%.** Blokuje
   tworzenie nowych wolumenów na tym poolu. Opcja pierwsza, nieniszcząca:
   `zfs set refreservation=none` na zvolach wyłączonych klastrów — oddaje
   325 GiB rezerwacji, zostawia dane. Opcja druga, nieodwracalna: skasować te
   dyski, tracąc możliwość wskrzeszenia `claude-r9g` i `claude-r9t`. Patrz
   sekcja „`data1`: wyczerpana rezerwacja, nie miejsce".
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
pvesm status                     # widok Proxmoksa: rezerwacja
zpool list -o name,size,alloc,free,cap    # widok ZFS: fizyczna alokacja

# Dlaczego oba się różnią — grube zvole rezerwują pełny volsize
zfs get -p volsize,refreservation,referenced data1/vm-9150-disk-0

# Rezerwacja per VM, posortowana
zfs list -H -p -t volume -o name,used | awk '{split($1,a,"/"); n=a[length(a)];
  if (match(n,/vm-[0-9]+/)) {id=substr(n,RSTART+3,RLENGTH-3); u[a[1]"|"id]+=$2}}
  END{for (k in u) printf "%s %.1f GiB\n", k, u[k]/1073741824}' | sort

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
