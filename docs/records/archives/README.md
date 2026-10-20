# Archiwum zarchiwizowanych definicji

Zamrozone definicje skasowanych flot. Zrodlo prawdy dla historii; NIE uruchamiaj z nich buildow.
Zywa flota: `make fleet-state`. Zasady: `docs/infrastructure-state.md`.

- katalogi terraform: 32
- katalogi clusters: 29
- katalogi platform: 8

2026-10-20: wycofano najemcę weryfikacyjnego `lyrav19-r9` (Rocky 9, MariaDB 12.3.3,
świeża budowa od zera) do `clusters-lyrav19-r9/` i `terraform-lyrav19-r9/`. VMID
10066–10069 usunięte, stan Terraform ma zero zasobów, wpisy PMM/Grafana/ProxySQL/MinIO
zdjęte (`cluster-deregister-verify` PASS). Kopie w buckecie `lyrav19-galera-backups-worm`
zostają pod blokadą GOVERNANCE do końca retencji. Nie uruchamiać tych rootów.

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
