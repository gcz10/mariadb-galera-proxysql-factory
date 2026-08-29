# Projekt: cutover v9 → v10

**Data:** 2026-08-29
**Status:** zatwierdzony przez operatora
**Zakres:** laboratorium PVE, pełna flota

## Cel

Zatrzymać całą obecną flotę v9 (`xenonv9`, `orionv9-r9`,
`cassiopeiav9-r9`) bez niszczenia VM-ów i bez usuwania definicji z repozytorium,
a następnie zbudować od zera pełną flotę Rocky Linux 10.

v10 jest świeżą instalacją. Migracja danych v9 nie należy do tego przebiegu.

## Stan wejściowy i założenia

- Obecnie działa 12 VM-ów v9; wszystkie należą do puli PVE `claude-isa`.
- Obraz `local:import/Rocky-10.2-GenericCloud.qcow2` jest dostępny na PVE.
- Terraform działa lokalnie w wersji 1.15.6; provider `bpg/proxmox` jest przypięty
  w repozytorium do `0.111.1`.
- Rooty Terraform używają lokalnego stanu ignorowanego przez Git.
- PVE `local-zfs` ma około 278 GB wolnego miejsca.
- VMID-y `10000–10011` oraz odpowiadające wolumeny nie istnieją.
- Skan aktywności `.154–.178` wykrył `.156`; adres zostaje wyłączony z puli
  i dopisany do `clusters/reserved-addresses.yml`.
- Obecna gałąź robocza to `audit/t3-judo`; operacja nie przełącza ani nie scala
  gałęzi.

## Topologia v10

| Warstwa | Hosty | VMID | Adresy |
|---|---|---:|---|
| `xenonv10` | `x10mon`, `x10p1`, `x10p2`, `x10app` | `10000–10003` | `.160–.163` |
| `orionv10-r10` | `o10db1`, `o10db2`, `o10db3`, `o10r1` | `10004–10007` | `.164–.167` |
| `cassiopeiav10-r10` | `c10db1`, `c10db2`, `c10db3`, `c10r1` | `10008–10011` | `.168–.171` |
| VIP | — | — | `192.168.1.172:6033` |

v10 używa Rocky Linux 10.2, `versions/versions-el10.lock.yml` oraz polityki
`locked`. Platforma ma własne PMM, MinIO, Maildev i parę ProxySQL.

Dla separacji najemców v10 dostaje nowe hostgroupy i użytkowników ProxySQL;
nie używa zakresów ani kont v8/v9. PKI jest odrębne: `pki/xenonv10`, `pki/o10`
i `pki/c10`.

Konfiguracja MariaDB dziedziczy sprawdzone parametry v9, w tym jawne
`gcache_size: 512M`. Wszystkie trzy rooty używają wspólnego modułu
`terraform/modules/pve_vm_set`; rooty Rocky 10 ustawiają obraz EL10, `os_type`,
interfejs inicjalizacji i istniejący snippet `local:snippets/r10-cloud-init.yaml`.

## Kolejność

1. Dodać definicje platformy, obu tenantów i trzy rooty Terraform. Nie zmieniać
   żadnego pliku v9.
2. Dodać `.156` do rejestru zarezerwowanych adresów.
3. Uruchomić walidatory repozytorium oraz `terraform fmt`/`validate`.
4. W każdym rootcie wykonać `terraform plan -out`. Plan musi obejmować wyłącznie
   nowe zasoby i zawierać zero operacji `destroy`.
5. Gracefully zatrzymać v9 przez API PVE: najpierw osiem VM-ów tenantów, potem
   cztery VM-y platformy. Dla każdego zadania sprawdzić zakończenie i końcowo
   potwierdzić 0/12 VM-ów `running`. Nie stosować automatycznego hard-stopu po
   timeoutcie ACPI.
6. Zastosować zatwierdzone plany Terraform z `-parallelism=1`.
7. Odświeżyć `known_hosts`, wygenerować certyfikaty wspólnej platformy i obu
   tenantów, a następnie wykonać `platform-build`.
8. Wykonać pełne `cluster-build` dla `orionv10-r10` i `cassiopeiav10-r10`.
9. Uruchomić bramki końcowe: stan PVE, zdrowie Galery, routing ProxySQL i VIP,
   TLS, PMM, backup/restore oraz `lab-post-build-gate`.
10. Pozostawić v9 zatrzymane. Nie uruchamiać `infra-teardown` ani
    `cluster-deregister` dla v9.

## Niezmienniki bezpieczeństwa

- Stare rooty Terraform i katalogi `clusters/*v9*` pozostają nietknięte.
- Żaden plan v10 nie może niszczyć zasobu v9 ani używać jego VMID-u.
- Nowe adresy nie mogą kolidować z żadną definicją repozytorium, VIP-em,
  hypervisorem ani rejestrem adresów zewnętrznych.
- Terraform apply jest wykonywany wyłącznie po odczytaniu planu i zawsze
  serialnie (`parallelism=1`), aby nie wywołać konfliktów ZFS na PVE.
- Awaria walidacji, planu, shutdownu lub builda zatrzymuje procedurę zamiast
  przechodzić dalej po cichu.

## Rollback

Przed provisioningiem v10 rollback oznacza ponowne uruchomienie zachowanych
VM-ów v9 i ich platformy; definicje oraz dane pozostają na miejscu.

Po częściowym provisioning v10 nie wolno ręcznie kasować VM-ów. Najpierw należy
zachować wynik planu i stan Terraform, ustalić zakres utworzonych zasobów, a potem
użyć odpowiedniego rootu i kontrolowanego teardownu. v9 nadal pozostaje
niezmienione.

## Kryteria akceptacji

- `fleet-state` pokazuje wszystkie 12 VM-ów v9 jako `stopped`.
- `fleet-state` pokazuje 12 nowych VM-ów v10 jako `running` i przypisuje je do
  trzech nowych definicji.
- Oba klastry v10 mają 3/3 węzły `Primary`, `Synced`, `wsrep_ready=ON`.
- VIP `.172` działa na nowej parze ProxySQL; każdy tenant ma dokładnie jednego
  writera ONLINE i rozłączne hostgroupy/użytkownika.
- Przechodzą walidacja konfiguracji, TLS, monitoring, backup/restore i końcowa
  bramka po budowie.
- Repozytorium zachowuje wszystkie definicje v9 oraz nową, kompletną topologię
  v10.
