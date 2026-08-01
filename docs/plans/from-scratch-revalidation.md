# Plan: rewalidacja kodu od zera (teardown + odbudowa)

**Status:** propozycja, przed akceptacją operatora
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

### D1. Zakres kasowania

| Grupa | VMID | Rekomendacja |
|---|---|---|
| Nasze | `9123-9126`, `9130-9132`, `9150-9153`, `9170-9173`, `9193-9195` (18) | **kasuj** |
| Poprzednicy ISA, poza Terraform | `9010-9012`, `9040-9042`, `9050`, `9060` (8) | **kasuj** — zatrzymane, w żadnym inventory, ~30 GB na `rpool` |
| Obce | `9000`, `9201-9235`, `9301`, `9501-9999` | **nie dotykaj** — RKE2, GitLab, `qoder-*` |

Uwaga na nazwy: `qoder-galera-01`, `qoder-proxysql-01`, `qoder-pmm-01` brzmią
jak nasze, **nie są nasze** i działają. Rozróżnia je wyłącznie prefiks `qoder-`.

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

Jedna pętla, storage-agnostyczna. `--purge` usuwa dyski i odwołania z innych
konfiguracji niezależnie od tego, na którym poolu leżą — dlatego nie
potrzebujemy per-modułowego `PVE_STORAGE`, który był głównym źródłem ryzyka
w poprzedniej wersji tego planu.

```bash
# na hoście PVE
for id in 9123 9124 9125 9126 9130 9131 9132 \
          9150 9151 9152 9153 \
          9170 9171 9172 9173 \
          9193 9194 9195 \
          9010 9011 9012 9040 9041 9042 9050 9060; do
  qm stop    "$id" 2>/dev/null || true
  qm destroy "$id" --purge --destroy-unreferenced-disks 1
done
```

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
qm list | grep -E '\b(912[3-6]|913[0-2]|915[0-3]|917[0-3]|919[3-5]|901[0-2]|904[0-2]|9050|9060)\b'
# oczekiwane: brak wyników

# Zero osieroconych wolumenów na OBU poolach
zfs list -t volume -o name | grep -E 'vm-(912[3-6]|913[0-2]|915[0-3]|917[0-3]|919[3-5]|901[0-2]|904[0-2]|9050|9060)-'
# oczekiwane: brak wyników

# Rezerwacja data1 zwolniona: ~906G -> ~580G used
zfs list -o name,used,avail data1
```

Osierocone wolumeny są jedynym realnym ryzykiem teardownu: blokują późniejsze
`apply` komunikatem `dataset already exists` i utrzymują grubą rezerwację,
którą ta operacja ma zwolnić.

## Faza 3 — Nowy układ Terraform

Praca w kodzie, przed dotknięciem PVE:

1. `terraform/infra/` — nowy moduł, jedna VM `.47` (wzorzec: wpis `infra`
   z usuwanego `claude-r10b`).
2. `terraform/claude-r10c/` — mapa `vms` rozszerzona o `pnode1 .44`,
   `pnode2 .45`, `rnode1 .46`; zasoby czytają `each.value.cpu/ram/disk`
   zamiast zaszytych wartości (wzorzec: `claude-r9t`).
3. `terraform/claude-r9t/` — bez zmian funkcjonalnych; weryfikacja, że
   deklaruje `9180-9185` / `.61-.66` spójnie z inventarzem.

Bramka: `terraform fmt -check` i `terraform validate` w każdym module, plus
pełna statyka repo. Dopiero potem cokolwiek się stawia.

## Faza 4 — Odbudowa

Wyłącznie skodyfikowanymi celami. **Zero ręcznego `terraform apply`** —
`make infra-provision` wymusza `-parallelism=1`, a jego ominięcie już raz
wywaliło locki ZFS na PVE (`HTTP 596 Broken pipe` i VM utworzona poza stanem).

Kolejność kroków jest wymuszona zależnościami z `README.md`: F6 asertuje granty
`pmm_monitor`, więc idzie po F11; F11 rejestruje metryki ProxySQL, więc idzie po
F7; drill restore wymaga danych, więc idzie po zasiewie.

```bash
# Etap 1 — infra
make infra-provision CLUSTER=infra

# Etap 2 — claude-r10c (Etap 3 dla claude-r9t: identycznie, bez cluster-infra)
C=claude-r10c
make infra-provision   CLUSTER=$C
make cluster-trust-hosts CLUSTER=$C          # po WSZYSTKICH provision — klucze hostów są nowe
make cluster-validate  CLUSTER=$C
make cluster-deploy    CLUSTER=$C            # F2+F3
make cluster-infra     CLUSTER=$C            # PMM + MinIO + maildev — JEDEN RAZ w całym planie
make cluster-bootstrap CLUSTER=$C CONFIRM=yes
make cluster-join      CLUSTER=$C
make cluster-monitoring CLUSTER=$C           # F11 przed F6
make cluster-harden    CLUSTER=$C            # F6
make cluster-firewall  CLUSTER=$C
make cluster-proxysql  CLUSTER=$C            # F7
make cluster-endpoint  CLUSTER=$C            # F8 — VIP
make lab-seed-smoke    CLUSTER=$C            # dane, bez których drill pada
make cluster-backup-configure CLUSTER=$C
make cluster-backup    CLUSTER=$C
make cluster-restore-drill CLUSTER=$C CONFIRM=yes
make cluster-alerts    CLUSTER=$C            # F15
make cluster-monitoring-refresh CLUSTER=$C
```

`make infra-provision CLUSTER=infra` wymaga, by `TF_DIR` odwzorowywał nazwę na
`terraform/infra/`, a `cluster_guard` nie blokował celu bez katalogu
`clusters/infra/`. Do sprawdzenia w Fazie 3 — jeśli bramka odbije, moduł infra
stawiamy osobnym celem `infra-provision-shared`.

Etap 3 (`claude-r9t`) to ta sama sekwencja bez `cluster-infra`. `tls.mode: full`
jest już w `cluster.yml`; certy rozprowadza `playbooks/tls_certs.yml` włączany
przez site/bootstrap/join/f7 — nie ma dodatkowego celu.

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
