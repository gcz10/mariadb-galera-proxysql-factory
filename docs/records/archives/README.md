# Archiwum zarchiwizowanych definicji

Zamrozone definicje skasowanych flot. Zrodlo prawdy dla historii; NIE uruchamiaj z nich buildow.
Zywa flota: `make fleet-state`. Zasady: `docs/infrastructure-state.md`.

- katalogi terraform: 31
- katalogi clusters: 28
- katalogi platform: 8

2026-09-22: wycofano `cassiopeiav17-r9` do `clusters-cassiopeiav17-r9/`
i `terraform-cassiopeiav17-r9/`. VMID 10058–10061 usunięte; stan Terraform
odświeżony ma zero zasobów. Lokalna kopia poprzedniego stanu pozostaje poza Git.
Historyczne wyjście `vms` w archiwalnym stanie opisuje dawną deklarację,
nie istniejące maszyny. Nie uruchamiać tego roota.
