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

2026-09-22: katalogi `.terraform/` (binarka providera ~26 MB + metadane) przestaly
byc wersjonowane — regula w `.gitignore`. Zmierzone przed zdjeciem z indeksu:
115 plikow, 605 MB razem, 18 archiwow, czyli ~34 MB na root. Pliki zostaja na
dysku; z indeksu znikly, ale HISTORIA nadal je niesie, wiec rozmiar klonu nie
zmaleje, dopoki nikt nie przepisze historii (swiadomie nie robione).
Archiwum ma zamrazac DEKLARACJE i stan (`main.tf`, `terraform.tfstate`,
`.terraform.lock.hcl`), nie pobrane binarki: te odtwarza `terraform init`.
