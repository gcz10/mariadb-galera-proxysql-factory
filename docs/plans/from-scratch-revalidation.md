# Plan: rewalidacja kodu od zera (teardown + odbudowa)

**Status:** ✅ WYKONANY 2026-08-02. Flota `finalclaude` stoi i przeszła wszystkie
kryteria z Fazy 5. Bieżący obraz: `docs/infrastructure-state.md`.
Rezultat ćwiczenia — pięć defektów, które mogły ujawnić się WYŁĄCZNIE przy
budowie od zera: `svcaccs: null` na świeżym MinIO, zaszyty `rnode1` w sondzie
restore, nieaktualna lista reguł alertowych, przydział RAM trafiający dokładnie
w próg preflightu, oraz brak pojęcia własności warstwy współdzielonej
w f2/f11 (commity `7e1429e`, `9eed120`, `a9ab24a`).

**Data sporządzenia:** 2026-08-02

## Cel

Udowodnić, że repozytorium odtwarza działający klaster od zera — bez ręcznej
interwencji i bez stanu narosłego przez miesiąc iteracji. Dopóki nie skasujemy
wszystkiego i nie odtworzymy z kodu, **nie wiemy, które kroki są skodyfikowane,
a które przetrwały tylko dlatego, że ktoś je kiedyś zrobił z palca**.

Precedens: odbudowa `claude-r10c` (commit `d123980`) poszła w całości ze
skodyfikowanych celów i wykryła dwa realne braki — bramkę poświadczeń
odrzucającą token API oraz niezasiane dane, bez których drill restore nie mógł
przejść. Ten plan robi to samo w pełnej skali.

## Co jest przedmiotem testu, a co rusztowaniem

**Testujemy:** playbooki, role, `roles/galera_backup/`, definicje
`clusters/<name>/`, sondy i bramkę statyczną. To jest produkt.

**Rusztowaniem jest Terraform.** Jego jedyne zadanie to postawić maszyny pod
adresami, których oczekuje inventory. Stare moduły i ich stan nie mają
wartości, którą warto chronić — dlatego ten plan ich nie odtwarza, nie migruje
i nie sprząta „ostrożnie". Kasujemy VM wprost i piszemy układ modułów na nowo,
zgodny z tym, co repo faktycznie deklaruje.

To upraszcza teardown o rząd wielkości: nie ma `terraform destroy` per moduł,
nie ma dobierania `PVE_STORAGE` do datastore'u, nie ma zależności od tego, co
pamiętają `terraform output`. Jest lista VMID i jedna pętla.

## Problem, który przy okazji znika

`infranode .47` (PMM, MinIO, maildev) jest dziś tworzony przez moduł
`terraform/claude-r10b`, a wskazują na niego wszystkie klastry. Ten sam moduł
niesie współdzielony ProxySQL `.44`/`.45`, restore `.46` i VIP `.50`, a jego
własne inventory nadal deklaruje trwale wyłączone `gnode4-6` (`.51-.53`).

Skutek: uruchomienie `f7_proxysql` albo `f8_keepalived` z inventarza
`claude-r10b` przepięłoby żywy VIP na martwe węzły. To rozbieżność #5
z `docs/infrastructure-state.md` — dziś obchodzona wyłącznie dyscypliną
operatora.

W nowym układzie **znika, bo `claude-r10b` przestaje istnieć**.

### Docelowy układ modułów

| Moduł | Zawartość | Uwaga |
|---|---|---|
| `terraform/infra/` | `infranode .47` (8 GB RAM, 80 GB) | jedyna rzecz naprawdę współdzielona |
| `terraform/claude-r10c/` | galera `.71-.73` + proxysql `.44`/`.45` + restore `.46` | samowystarczalny poza infrą |
| `terraform/claude-r9t/` | galera `.61-.63` + proxysql `.64`/`.65` + restore `.66` | już dziś samowystarczalny |

Nakład jest mały, bo to kopiuj-dostosuj z istniejących modułów, nie projekt od
zera: `claude-r10b` i `claude-r9t` już czytają `each.value.cpu/ram/disk`
i `try(each.value.store, local.storage)`. `claude-r10c` ma dziś mapę z samymi
`{id, ip}` i zaszytym `size = 40` — dochodzi mu ten sam wzorzec plus trzy wpisy.

**Żadne `clusters/*/inventory.yml` ani `cluster.yml` się nie zmieniają.**
Adresy pozostają te same; zmienia się wyłącznie to, który moduł je tworzy.

Moduły klastrów, których nie odbudowujemy — `claude-pve`, `claude-r10`,
`claude-r10t`, `claude-r9g`, `claude-r10b` — są usuwane razem ze stanem.
Definicje `clusters/<name>/` zostają: są tanie, walidowane przez CI i służą
jako warianty referencyjne.

## Co musi przetrwać

| Artefakt | Dlaczego krytyczny |
|---|---|
| `tests/lab/.env` | wszystkie sekrety: PMM, ProxySQL, MinIO, `BACKUP_ENCRYPTION_KEY`. **Bez tego odbudowa jest niemożliwa.** |
| playbooki, role, `clusters/*/`, testy | produkt pod testem — nietykalne |

Na hoście PVE (leżą w `local`, teardown ich nie dotyka) — **zweryfikowane
2026-08-02**:

- `local:import/Rocky-10.2-GenericCloud.qcow2` (519 MiB)
- `local:import/Rocky-9.8-GenericCloud.qcow2` (616 MiB)
- `local:snippets/r10-cloud-init.yaml` — wymagany tylko przez moduły EL10
- pula `claude-isa`

## Co ginie bezpowrotnie

Świadomie — to maszyny testowe:

- **Wszystkie backupy w MinIO.** Jedyna zawartość to `isa_test.restore_probe`.
- **Historia metryk PMM** i reguły alertowe (odtwarza `f15_alerts`).
- **Dane w Galerze** — schemat `isa_test` odtwarza `make lab-seed-smoke`.
- **Stan i kod Terraform starych klastrów.**

Jeżeli którykolwiek punkt jest fałszywy — **zatrzymaj się przed Fazą 1**.

## Decyzje wymagane przed startem

### D1. Zakres kasowania — rozstrzygnięte

| Grupa | VMID | Decyzja |
|---|---|---|
| Nasze — utworzone przez tę automatyzację | `9123-9126`, `9130-9132`, `9150-9153`, `9170-9173`, `9193-9195` (18) | **kasuj** |
| Poprzednicy ISA | `9010-9012`, `9040-9042`, `9050`, `9060` (8) | **nie dotykaj** — powstały przed tą automatyzacją, decyzja o nich nie należy do nas |
| Obce | `9000`, `9201-9235`, `9301`, `9501-9999` | **nie dotykaj** — RKE2, GitLab, `qoder-*` |

Uwaga na nazwy: `qoder-galera-01`, `qoder-proxysql-01`, `qoder-pmm-01` brzmią
jak nasze, **nie są nasze** i działają. Rozróżnia je wyłącznie prefiks `qoder-`.

### D1a. Reguła stała: pula `claude-isa`

Każdy zasób tworzony lub zmieniany przez tę automatyzację należy do puli
Proxmox `claude-isa` — bez wyjątków. Wszystkie moduły ustawiają już
`pool_id = "claude-isa"`, a nowy `terraform/infra/` musi to powtórzyć.

Kierunek użycia puli jest jednostronny: **sprawdzamy przynależność, nigdy nie
wyprowadzamy z niej listy do kasowania.** Pula mówi „to jest nasze", nie „to
wolno skasować" — te dwa zdania nie są równoważne, a pomylenie ich zamiata
wszystko, co ktoś kiedyś do puli wrzucił.

### D2. Zakres odbudowy

| Etap | Co | Dowodzi | VM |
|---|---|---|---:|
| 1 | `terraform/infra/` | PMM + MinIO od zera | 1 |
| 2 | `claude-r10c` | pełny klaster EL10, `tls=disabled` | 6 |
| 3 | `claude-r9t` | EL9 + `tls=full` — druga platforma, druga oś TLS | 6 |

Etapy 1+2 są nierozdzielne. Etap 3 jest opcjonalny, ale bez niego nie testujemy
ani EL9, ani TLS — a ścieżka TLS nie była ruszana od czasu lab2.

## Faza 0 — Zapis stanu

Cel: wiedzieć, co było, gdy trzeba będzie porównać. Powstaje
`docs/records/2026-08-02-pre-teardown.md`, commitowany **przed** teardownem —
to jedyny moment, w którym te dane istnieją.

Zapisujemy fakty o maszynach i usługach, nie archeologię Terraform:

1. `qm list` w całości (43 VM) + `qm config <id>` dla 18 naszych: rdzenie, RAM,
   dyski, MAC, bridge.
2. Rezerwacja ZFS per VM + `zpool list` — punkt odniesienia dla dowodu, że
   rezerwacja `data1` wróciła.
3. PMM: zarejestrowane węzły, usługi, reguły alertowe.
4. MinIO: buckety i zawartość.
5. `state.json` runnera backupu z `gnode7` i zdrowie Galery w chwili zamrożenia.

## Faza 1 — Teardown

Zakres jest **zamkniętą listą 18 VMID**, nie zapytaniem. Nie wyprowadzamy go
z puli, z prefiksu nazwy ani z tagów: pula `claude-isa` służy wyłącznie jako
*asercja* („każda maszyna, którą kasuję, musi w niej być"), nigdy jako źródło
listy. Odwrotny kierunek wciągnąłby wszystko, co ktoś kiedyś do puli wrzucił.

`--purge` usuwa dyski i odwołania niezależnie od datastore'u, więc nie
potrzebujemy per-modułowego `PVE_STORAGE` — głównego źródła ryzyka
w poprzedniej wersji tego planu.

```bash
# na hoście PVE
MINE="9123 9124 9125 9126 9130 9131 9132 9150 9151 9152 9153 9170 9171 9172 9173 9193 9194 9195"

# Bramka: przerwij, jeśli którakolwiek nie należy do claude-isa
POOL=$(pvesh get /pools/claude-isa --output-format json | python3 -c \
  'import sys,json; print(" ".join(str(m["vmid"]) for m in json.load(sys.stdin)["members"]))')
for id in $MINE; do
  case " $POOL " in *" $id "*) ;; *) echo "STOP: $id poza claude-isa"; exit 1;; esac
done

for id in $MINE; do
  qm stop    "$id" 2>/dev/null || true
  qm destroy "$id" --purge --destroy-unreferenced-disks 1
done
```

**Nie dotykamy `9010-9012`, `9040-9042`, `9050`, `9060`** (`galera-01..03`,
`galera10-01..03`, `proxysql-01`, `monitoring-01`). To nie są maszyny tej
automatyzacji — powstały przed nią i nie jest naszą rzeczą o nich decydować.

Następnie w repo — usunięcie modułów i ich stanu:

```bash
git rm -r terraform/claude-pve terraform/claude-r10 terraform/claude-r10t \
          terraform/claude-r9g terraform/claude-r10b
rm -rf terraform/claude-r10c/.terraform terraform/claude-r10c/terraform.tfstate* \
       terraform/claude-r9t/.terraform  terraform/claude-r9t/terraform.tfstate*
```

Stan `claude-r10c` i `claude-r9t` kasujemy, bo ich moduły i tak dostają nowy
kształt — pusty stan jest właściwym punktem wyjścia.

## Faza 2 — Dowód czystego pola

Teardown bez tej fazy jest twierdzeniem, nie faktem.

```bash
# Zero naszych VM
qm list | grep -E '\b(912[3-6]|913[0-2]|915[0-3]|917[0-3]|919[3-5])\b'
# oczekiwane: brak wyników

# Zero osieroconych wolumenów na OBU poolach
zfs list -t volume -o name | grep -E 'vm-(912[3-6]|913[0-2]|915[0-3]|917[0-3]|919[3-5])-'
# oczekiwane: brak wyników

# Rezerwacja data1 zwolniona: ~906G -> ~580G used
zfs list -o name,used,avail data1
```

Osierocone wolumeny są jedynym realnym ryzykiem teardownu: blokują późniejsze
`apply` komunikatem `dataset already exists` i utrzymują grubą rezerwację,
którą ta operacja ma zwolnić.

## Faza 3 — Układ Terraform ✅ zrobione

Zrealizowany kształt (inny niż pierwotnie szkicowany — flota nazywa się
`finalclaude`, a warstwa wspólna `shared`, nie `infra`):

| Moduł | Zawartość | VMID | IP |
|---|---|---|---|
| `terraform/shared/` | `fcinfra` (PMM+MinIO+maildev), `fcp1`, `fcp2` (ProxySQL HA) | 9400-9402 | .130-.132, VIP .133 |
| `terraform/finalclaude-r10/` | `f10g1-3` (galera), `f10r1` (restore) | 9410-9413 | .140-.143 |
| `terraform/finalclaude-r9/` | `f9g1-3` (galera), `f9r1` (restore) | 9420-9423 | .150-.153 |

`make infra-provision CLUSTER=shared` działa bez zmian w Makefile: `TF_DIR`
domyślnie to `terraform/$(CLUSTER)`, a cel nie czyta `clusters/`.

RAM — limit operatora 5 GB, podnoszony wyłącznie na dowód:

- `fcinfra` 5 GB — PMM zajmował 1.4 GB z 3 GB przy **zerze** usług.
- `fcp1`/`fcp2` 3 GB — preflight wymaga `ansible_memtotal_mb >= 2048`, a
  przydział 2048 MB daje 1769 MB widzianych przez OS. W próg trzeba uderzyć
  z zapasem, nie trafić w niego dokładnie.
- Galera 3 GB, `innodb_buffer_pool_size: 768M` — zapas na `mariabackup` w SST.

## Faza 4 — Odbudowa

Wyłącznie skodyfikowanymi celami. **Zero ręcznego `terraform apply`** —
`make infra-provision` wymusza `-parallelism=1`, a jego ominięcie już raz
wywaliło locki ZFS na PVE (`HTTP 596 Broken pipe` i VM utworzona poza stanem).

### Etap 1 — warstwa wspólna ✅

```bash
make infra-provision CLUSTER=shared
make platform-trust-hosts PLATFORM=shared
make platform-build PLATFORM=shared ANSIBLE_OPTS='-e allow_kernel_reboot=yes'
make infra-provision CLUSTER=finalclaude-r10
make cluster-trust-hosts CLUSTER=finalclaude-r10
```

Warstwa wspólna ma własny inventory i lifecycle. `platform-build` wykonuje
kanoniczną kolejność validate → deploy → **platform-firewall** → infra →
ProxySQL → endpoint → monitoring → alerty → verify. Jawny krok firewalla jest
granicą ownership: tylko platforma zarządza `fcp1/fcp2/fcinfra/fcapp`.
Późniejszy `cluster-trust-hosts` obejmuje inventory przygotowywanego najemcy.

`allow_kernel_reboot=yes` jest konieczne przy świeżym obrazie: VM bootuje
starszy kernel niż zainstalowany, brakuje modułów `xtables` i filtr ingress
Dockera by nie powstał. Strażnik odmawia — słusznie.

### Etap 2 — klaster

Kolejność wymuszona zależnościami z `README.md:32-37` (sekwencja zweryfikowana
od zera): **F7 przed F11**, bo F11 asertuje niezerową liczbę metryk ProxySQL;
**F11 przed F6**, bo hardening asertuje granty `pmm_monitor`.

```bash
C=finalclaude-r10
make cluster-validate  CLUSTER=$C
make cluster-deploy    CLUSTER=$C            # F2+F3
make cluster-bootstrap CLUSTER=$C CONFIRM=yes
make cluster-join      CLUSTER=$C            # F5
make cluster-proxysql  CLUSTER=$C            # F7 — MUSI poprzedzać F11
make cluster-monitoring CLUSTER=$C           # F11
make cluster-harden    CLUSTER=$C            # F6 — MUSI następować po F11
make cluster-firewall  CLUSTER=$C
make cluster-endpoint  CLUSTER=$C            # F8 — VIP
make lab-seed-smoke    CLUSTER=$C            # dane, bez których drill pada
make cluster-backup-configure CLUSTER=$C
make cluster-backup    CLUSTER=$C
make cluster-restore-drill CLUSTER=$C CONFIRM=yes
make cluster-alerts    CLUSTER=$C            # F15
make cluster-monitoring-refresh CLUSTER=$C
```

### Etap 3 — wielodostęp ProxySQL (wymagany przed drugim klastrem)

Obecny kod **nie obsługuje** dwóch klastrów na jednej parze ProxySQL:
`playbooks/f7_proxysql.yml:271` robi
`DELETE FROM mysql_servers WHERE hostgroup_id IN (10,20,30,40)`, a te ID są
globalnymi stałymi z `playbooks/vars/proxysql_hostgroups.yml`. Drugi klaster
skasowałby backendy pierwszego.

Potrzebne: baza hostgroup per klaster (`10`, `110`, …) plus osobny port na
wspólnym VIP (`proxysql.endpoint.port` już istnieje w `cluster.yml`).

### Etap 4 — `finalclaude-r9`

Ta sama sekwencja co Etap 2; lifecycle warstwy wspólnej nie jest powtarzany.

## Faza 5 — Kryteria akceptacji

Odbudowa jest udana wtedy i tylko wtedy, gdy **każdy** punkt przechodzi bez
ręcznej interwencji. Każda interwencja to luka w kodzie — zapisywana jako
zadanie, nie „naprawiana z palca".

1. **Galera** — `Primary`, `wsrep_cluster_size=3`, `wsrep_ready=ON` na każdym
   węźle każdego odbudowanego klastra.
2. **Endpoint** — zapis przez VIP dociera do writera; dokładnie jeden ProxySQL
   trzyma VIP.
3. **Backup** — `state.json` z `last_failure: null`, artefakt w MinIO, zgodna
   suma `sha256`, zaszyfrowany.
4. **Drill restore** — `status: success`, ≥1 baza i ≥1 tabela zweryfikowane, za
   **pierwszym** podejściem.
5. **PMM** — wszystkie cele `up`, `mysql_up=1` na węzłach Galery, 8 reguł
   alertowych na klaster.
6. **TLS (etap 3)** — `have_ssl=YES`, szyfrowana replikacja Galera, ProxySQL
   `use_ssl=1` do backendów.
7. **Statyka** — testy jednostkowe, 5 sond, schema + inventory wszystkich
   klastrów, syntax-check playbooków, `terraform fmt` i `validate`.
8. **Repo czyste** — `git diff` pusty po odbudowie. Jeżeli cokolwiek trzeba było
   zmienić, żeby przeszło, to **wynik testu**: osobny commit z uzasadnieniem.

## Ryzyka

| Ryzyko | Prawdopodobieństwo | Reakcja |
|---|---|---|
| Osierocone wolumeny blokują `apply` | niskie po przejściu na `--purge` | Faza 2 wykrywa przed odbudową; ręcznie `zfs destroy` |
| `HTTP 596` przy równoległym `apply` | wysokie poza celami z Makefile | wyłącznie `make infra-provision`; przy sierocie skasuj przez API i wznów |
| Utrata `tests/lab/.env` | niskie, ale **katastrofalne** | zweryfikuj obecność i czytelność w Fazie 0 |
| `cluster_guard` odbija moduł `infra` | średnie | osobny cel `infra-provision-shared` |
| Etap 3 (TLS) odsłania regresję | średnie — ścieżka nietykana od lab2 | to jest **cel** ćwiczenia; zapisz i napraw w kodzie |

## Czego ten plan NIE robi

- Nie zmienia adresacji ani zawartości `clusters/*/` — tylko właściciela maszyn
  po stronie Terraform.
- Nie rusza laboratorium dockerowego (`lab-cluster`, `lab2-cluster`).
- Nie odbudowuje `claude-r9g` — pokrywa się z `claude-r9t` co do platformy,
  różni tylko brakiem TLS. Definicja zostaje w repo.
