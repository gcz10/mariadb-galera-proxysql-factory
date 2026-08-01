# Rekord przed teardownem — 2026-08-02

**Zamrożone:** 2026-08-02 ~00:10 UTC, bezpośrednio przed skasowaniem maszyn.
**Powód:** rewalidacja kodu od zera (`docs/plans/from-scratch-revalidation.md`).

Artefakt **datowany i niezmienny**. Nie aktualizuj go — jeżeli stan się zmieni,
napisz nowy rekord. Żywy obraz infrastruktury trzymamy w
`docs/infrastructure-state.md`.

## Bramka zakresu

Skasowano dokładnie 18 maszyn wymienionych niżej. Zakres wzięto z zamkniętej
listy VMID, a przynależność do puli Proxmox `claude-isa` posłużyła jako
asercja — nie jako źródło listy.

Wynik sprawdzenia przed kasowaniem:

```
POOL=9123 9124 9125 9126 9130 9131 9132 9150 9151 9152 9153 9170 9171 9172 9173 9193 9194 9195
--- moje spoza puli (musi byc puste) ---
--- w puli, ale nie na mojej liscie ---
```

Pula `claude-isa` odpowiadała **dokładnie** naszym 18 maszynom: żadnej naszej
poza pulą, żadnej obcej w puli.

**Nietknięte:** `9010-9012`, `9040-9042`, `9050`, `9060` (`galera-01..03`,
`galera10-01..03`, `proxysql-01`, `monitoring-01`) — poprzednicy sprzed tej
automatyzacji, spoza puli `claude-isa`. Oraz cała reszta hosta: RKE2 (`9000`,
`9201-9235`), GitLab (`9301`), `qoder-*` (`9501-9999`).

## Maszyny w chwili zamrożenia

| VMID | Nazwa | Stan | vCPU | RAM (MB) | Dysk | IP |
|---|---|---|---:|---:|---|---|
| 9123 | r10b-pnode1 | running | 1 | 2560 | `local-zfs:vm-9123-disk-0` | 192.168.1.44 |
| 9124 | r10b-pnode2 | running | 1 | 2560 | `local-zfs:vm-9124-disk-0` | 192.168.1.45 |
| 9125 | r10b-rnode1 | running | 1 | 2560 | `local-zfs:vm-9125-disk-0` | 192.168.1.46 |
| 9126 | r10b-infra | running | 4 | 8192 | `local-zfs:vm-9126-disk-0` | 192.168.1.47 |
| 9130 | r10b-gnode4 | stopped | 2 | 4096 | `local-zfs:vm-9130-disk-0` | 192.168.1.51 |
| 9131 | r10b-gnode5 | stopped | 2 | 4096 | `local-zfs:vm-9131-disk-0` | 192.168.1.52 |
| 9132 | r10b-gnode6 | stopped | 2 | 4096 | `local-zfs:vm-9132-disk-0` | 192.168.1.53 |
| 9150 | r9g-g9node1 | stopped | 2 | 2560 | `data1:vm-9150-disk-0` | 192.168.1.17 |
| 9151 | r9g-g9node2 | stopped | 2 | 2560 | `data1:vm-9151-disk-0` | 192.168.1.18 |
| 9152 | r9g-g9node3 | stopped | 2 | 2560 | `data1:vm-9152-disk-0` | 192.168.1.19 |
| 9153 | r9g-r9node1 | running | 1 | 2560 | `data1:vm-9153-disk-0` | 192.168.1.39 |
| 9170 | r9t-g9tnode1 | stopped | 2 | 2560 | `data1:vm-9170-disk-0` | 192.168.1.54 |
| 9171 | r9t-g9tnode2 | stopped | 2 | 2560 | `data1:vm-9171-disk-0` | 192.168.1.55 |
| 9172 | r9t-g9tnode3 | stopped | 2 | 2560 | `data1:vm-9172-disk-0` | 192.168.1.56 |
| 9173 | r9t-r9tnode1 | running | 1 | 2560 | `data1:vm-9173-disk-0` | 192.168.1.57 |
| 9193 | r10c-gnode7 | running | 2 | 4096 | `local-zfs:vm-9193-disk-0` | 192.168.1.71 |
| 9194 | r10c-gnode8 | running | 2 | 4096 | `local-zfs:vm-9194-disk-0` | 192.168.1.72 |
| 9195 | r10c-gnode9 | running | 2 | 4096 | `local-zfs:vm-9195-disk-0` | 192.168.1.73 |

Wszystkie dyski 40 GB poza `9126` (80 GB). Brama `192.168.1.1`, bridge `vmbr0`.

## Stan usług

**Galera `claude-r10c`** — `gnode7/8/9` (`.71-.73`), Rocky 10.2,
MariaDB 11.4.12, `wsrep_cluster_name: r10c_galera`. Wszystkie trzy
`Primary`/`Synced`, `wsrep_ready=ON`, `wsrep_last_committed=21`, writer na
`gnode9`.

**Warstwa dostępowa** — `proxysql` i `keepalived` aktywne na `pnode1` i `pnode2`;
VIP `192.168.1.50` trzymał `pnode1`. Na `rnode1` MariaDB celowo `inactive`
(drill restore zostawia zatrzymany serwer).

**PMM 3.8.1** (`192.168.1.47`) — 6 węzłów w Inventory (`pmm-server` + 3 galera
+ 2 proxysql), 28/28 celów scrape'u `up`, `mysql_up=1` na trzech węzłach,
8 reguł alertowych `isa-r10c-galera-*`.

**Backup** — ostatni sukces `galera-claude-r10c-20260801-212253`,
`last_failure: null`; drill restore zaliczony ~29 min przed zamrożeniem.

**MinIO** (`192.168.1.47:9000`) — buckety: `r10c-galera-backups` (aktywny),
`r10b-galera-backups`, `r10t-galera-backups`, `r9g-galera-backups`,
`r10n-galera-backups` (po skasowanym klastrze), `decoy-bucket-test` (artefakt
testu ownership z 2026-07-29).

## Pojemność przed teardownem

| Pool | Rozmiar | Zaalokowane fizycznie | Zarezerwowane (widok Proxmoksa) |
|---|---|---|---|
| `data1` | 952 GB | 162 GB (16%) | 906 GB / **98.23%** |
| `rpool` (`local-zfs`) | 472 GB | 78.5 GB (16%) | 15.60% |

Rezerwację `data1` trzymały w 36% nasze wyłączone klastry: 8 zvoli
`claude-r9g` i `claude-r9t` po 40.6 GiB = 325 GiB rezerwacji przy ~15 GiB
realnych danych (zvole grube, `refreservation` = `volsize`). Teardown miał tę
rezerwację zwolnić — dowód w `docs/infrastructure-state.md` po odbudowie.

## Czego ten rekord celowo nie zawiera

Stanu i kodu Terraform starych klastrów. Nie jest to wartość warta zachowania:
moduły i tak dostają nowy kształt, a rusztowanie nie jest przedmiotem testu.
