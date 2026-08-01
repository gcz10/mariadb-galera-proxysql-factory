# Stan infrastruktury

**Snapshot:** 2026-08-02 01:10 UTC
**Zebrany z:** `terraform apply`, `ansible`, PMM/MinIO health, `free -m` na hostach.

> Ten plik jest **datowanym zdjęciem**, nie źródłem prawdy. Źródłem prawdy dla
> zamiaru są `clusters/<name>/` i `terraform/<name>/`; dla rzeczywistości —
> hypervisor.

## Flota `finalclaude` — w budowie

Stara flota (18 VM) została skasowana 2026-08-02; stan sprzed w
`docs/records/2026-08-02-pre-teardown.md`. Nowa powstaje od zera z kodu, etapami
— `docs/plans/from-scratch-revalidation.md`.

### Warstwa wspólna — `terraform/shared/` ✅ działa

**Nie należy do żadnego klastra.** Zawiera dokładnie to, co współdzielą wszystkie
klastry Galera, obecne i przyszłe. To celowa naprawa układu, w którym
`claude-r10b` był właścicielem infry używanej przez trzy klastry — i przez to
mógł przepiąć żywy VIP na własne, wyłączone węzły.

| Host | VMID | IP | Rola | vCPU | RAM |
|---|---:|---|---|---:|---:|
| `fcinfra` | 9400 | 192.168.1.130 | PMM + MinIO + maildev | 4 | 5120 MB |
| `fcp1` | 9401 | 192.168.1.131 | ProxySQL (HA) | 1 | 2048 MB |
| `fcp2` | 9402 | 192.168.1.132 | ProxySQL (HA) | 1 | 2048 MB |
| — | — | 192.168.1.133 | VIP Keepalived | — | — |

Stan: 3 kontenery na `fcinfra` (`pmm-server`, `minio`, `maildev`).
`https://192.168.1.130/v1/readyz` i `http://192.168.1.130:9000/minio/health/live`
zwracają **200** z węzłów klastra. Pamięć: 1358 MB użyte z 4654, **3296 wolne**.

ProxySQL: maszyny postawione, usługa jeszcze nie konfigurowana (wymaga
działającej Galery).

### `finalclaude-r10` — `terraform/finalclaude-r10/` 🔨 maszyny postawione

Rocky Linux 10, `tls=disabled`, `wsrep_cluster_name: fc10_galera`.

| Host | VMID | IP | Rola | vCPU | RAM |
|---|---:|---|---|---:|---:|
| `f10g1` | 9410 | 192.168.1.140 | galera + scheduler backupu | 2 | 3072 MB |
| `f10g2` | 9411 | 192.168.1.141 | galera | 2 | 3072 MB |
| `f10g3` | 9412 | 192.168.1.142 | galera | 2 | 3072 MB |
| `f10r1` | 9413 | 192.168.1.143 | restore (własny, nie współdzielony) | 1 | 2560 MB |

Stan: VM utworzone, SSH zweryfikowane (7/7 hostów w `known_hosts`). MariaDB
jeszcze nie instalowana.

Host restore jest **per klaster**, nie wspólny: drill czyści datadir, a
harmonogramy drilli są identyczne (`0 4 * * 0`) — wspólny host oznaczałby
regularną kolizję dwóch klastrów odtwarzających się jednocześnie.

## Limit zasobów

Operator: **max 5 GB RAM na VM**. Podnosimy tylko tam, gdzie jest dowód
potrzeby. `fcinfra` dostał 5 GB, bo przy zerze zarejestrowanych usług PMM
zajmował już 1.4 GB z 3 GB. Węzły Galera zostają na 3 GB z buffer poolem 768M
(zamiast 1G) — zapas na `mariabackup` podczas SST; podniesienie do 4 GB dopiero
gdy pomiar pokaże presję.

## Poza tą automatyzacją — nie dotykać

| Grupa | VMID | Uwaga |
|---|---|---|
| Poprzednicy ISA | `9010-9012`, `9040-9042`, `9050`, `9060` | poza pulą `claude-isa`, sprzed tej automatyzacji |
| RKE2 lab | `9000`, `9201-9235` | — |
| GitLab | `9301` | — |
| `qoder-*` | `9601-9620`, `9999` | **przenumerowane z `95xx` 2026-08-02** przez kogoś innego; nazwy `qoder-galera-*` mylnie przypominają nasze |

## Reguła stała

Każdy zasób tworzony przez tę automatyzację należy do puli Proxmox
`claude-isa`. Przynależność do puli służy jako **asercja** („to jest nasze"),
nigdy jako źródło listy do skasowania.

## Jak odtworzyć ten raport

```bash
qm list | sort -k1 -n
pvesh get /pools/claude-isa --output-format json
for m in terraform/shared terraform/finalclaude-r10; do terraform -chdir=$m output -json vms; done
ansible infra -i clusters/finalclaude-r10/inventory.yml -m shell -a 'docker ps; free -m'
ansible galera -i clusters/finalclaude-r10/inventory.yml -m shell -a \
  "curl -sk -o /dev/null -w 'pmm=%{http_code}' https://192.168.1.130/v1/readyz"
```
