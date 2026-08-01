# Stan infrastruktury

**Snapshot:** 2026-08-02 00:20 UTC
**Zebrany z:** `qm list`, `pvesm status`, `zpool list`, `zfs list` na hoście PVE.

> Ten plik jest **datowanym zdjęciem**, nie źródłem prawdy. Źródłem prawdy dla
> zamiaru są `clusters/<name>/` i `terraform/<name>/`; dla rzeczywistości —
> hypervisor. Sekcja „Jak odtworzyć ten raport" podaje dokładne komendy.

## Ten repozytorium nie ma teraz żadnej infrastruktury

Wszystkie 18 maszyn należących do tej automatyzacji zostało skasowanych
2026-08-02 w ramach rewalidacji kodu od zera
(`docs/plans/from-scratch-revalidation.md`). Stan sprzed skasowania — maszyny,
usługi, pojemność — jest zamrożony w `docs/records/2026-08-02-pre-teardown.md`.

Pula Proxmox `claude-isa` ma **0 członków**. Nie działa żaden klaster Galera,
ProxySQL, VIP, PMM ani MinIO.

## Co zostało na hoście

| Grupa | VMID | Status |
|---|---|---|
| Poprzednicy ISA | `9010-9012`, `9040-9042`, `9050`, `9060` | zatrzymane, **nietknięte** — powstały przed tą automatyzacją, poza pulą `claude-isa` |
| RKE2 lab | `9000`, `9201-9235` | nie nasze |
| GitLab | `9301` | nie nasze |
| `qoder-*` | `9501-9999` | nie nasze; nazwy `qoder-galera-*`, `qoder-proxysql-01`, `qoder-pmm-01` mylnie przypominają nasze |

## Pojemność po teardownie

| Pool | Użyte | Wolne | Widok Proxmoksa |
|---|---|---|---|
| `data1` | 581 GB | 341 GB | **62.99%** (było 98.23%) |
| `rpool` (`local-zfs`) | 55.5 GB | 402 GB | **10.49%** (było 15.60%) |

Rezerwacja `data1` spadła z 906 GB do 581 GB — **325 GB zwolnione**, co do
gigabajta tyle, ile przewidywała diagnoza: 8 grubych zvoli `claude-r9g`
i `claude-r9t` po 40.6 GiB. Blokada tworzenia nowych wolumenów na tym poolu
zniknęła.

## Co przetrwało i jest potrzebne do odbudowy

Zweryfikowane 2026-08-02, leży w `local`, teardown tego nie dotykał:

- `local:import/Rocky-10.2-GenericCloud.qcow2` (519 MiB)
- `local:import/Rocky-9.8-GenericCloud.qcow2` (616 MiB)
- `local:snippets/r10-cloud-init.yaml` — wymagany przez moduły EL10
- pula `claude-isa` (pusta, ale istnieje)
- lokalnie w repo: `tests/lab/.env` z sekretami — **bez tego odbudowa jest
  niemożliwa**

## Stan repo wobec rzeczywistości

Definicje `clusters/*/` i moduły `terraform/*/` **zostały nietknięte** — kod jest
przedmiotem nadchodzącego testu, nie jego ofiarą. Pliki
`terraform/*/terraform.tfstate` opisują teraz maszyny, których nie ma; to
oczekiwane i nieistotne, bo rusztowanie Terraform i tak dostaje nowy kształt
w Fazie 3 planu.

Reguła stała: **każdy zasób tworzony przez tę automatyzację należy do puli
`claude-isa`.** Przynależność do puli służy jako asercja („to jest nasze"),
nigdy jako źródło listy do skasowania.

## Jak odtworzyć ten raport

```bash
# Maszyny i pula (SSH jako root@192.168.1.181)
qm list | sort -k1 -n
pvesh get /pools/claude-isa --output-format json

# Pojemność: widok Proxmoksa (rezerwacja) vs ZFS (fizyczna alokacja)
pvesm status
zpool list -o name,size,alloc,free,cap
zfs list -o name,used,avail data1 rpool

# Warunki wstępne odbudowy
ls -la /var/lib/vz/import/ /var/lib/vz/snippets/r10-cloud-init.yaml
```
