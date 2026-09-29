# PMM: funkcjonalny restore kopii z MinIO na izolowanej VM

**Wynik: PASS, 2026-09-29.** Dla opiekuna monitoringu: wskazana kopia odtworzyła działający PMM na nowej VM, z poprawnym uwierzytelnianiem oraz odczytem historycznych metryk i QAN. To dowód funkcjonalny dla tej kopii, nie tylko sprawdzenie struktury archiwum ani gwarancja kolejnych backupów.

## Źródło i izolacja

- Kopia z MinIO: `pmm-xenonv17-20260927T201532Z.tar.gz`.
- SHA-256: `ac98c029c467f58c02c7c33bfc7a3fa8edc0a5031ed30b86b343d12540b1e522`; zgodny z sidecarem po pobraniu i po przeniesieniu do VM.
- Obraz z aktywnego lockfile'a: `percona/pmm-server:3.9.1@sha256:003f9c25f84256e53dccbd71975e3aaf98ca06e891bda86a35c262ef21ca58c1`. Wyeksportowano go z hosta źródłowego i załadowano offline; ID obrazu w próbie był identyczny ze źródłowym. Odtworzone API raportowało `3.9.1`.
- Jednorazowa VM `10100`, `isa-pmm-restore-20260929`: 2 vCPU, 5120 MiB RAM, nowy dysk 40 GiB. Konfiguracja PVE nie zawierała żadnego NIC. Transport archiwum, obrazu i runtime'u odbył się przez wirtualny CD-ROM; kontrola przez agent gościa i rootowy proces uruchomiony przez cloud-init.
- SELinux pozostał `enforcing`; końcowa kontrola używała domyślnej, ograniczonej domeny agenta gościa, nie `unconfined_service_t`.
- Kontener PMM: limit pamięci `4g`, `nofile=1000000`, bez publikowania portów, aktualizacje PMM i SMTP wyłączone. Nie podłączano go do klastrów ani żywych agentów.

Próby z `--network none` zakończyły się podczas konfiguracji wewnętrznego agenta komunikatem `pmm-agent: error: required argument 'node-address' not provided, try --help`. Odczytany z przypiętego obrazu `/opt/entrypoint.sh` wykonywał `pmm-agent setup` bez jawnego adresu węzła; mapowanie hostname na loopback nie wystarczyło. Nie zmieniano obrazu ani jego entrypointu.

Działający wariant używał sieci Dockera `pmm-drill-isolated`, `Internal=true`, z `gateway_mode_ipv4=isolated`, mostem `pmmbr0` i rzeczywistym adresem kontenera `172.31.252.2`. VM nadal nie miała NIC ani trasy domyślnej; kontener nie miał `PortBindings`. [Dokumentacja Docker, „Gateway modes”](https://github.com/docker/docs/blob/main/content/manuals/engine/network/port-publishing.md) opisuje tryb `isolated` dla sieci `internal`, bez adresu mostu po stronie hosta.

## Integralność odtworzonego wolumenu

Archiwum rozpakowano jako root do nowego, pustego wolumenu `/srv`, z `--numeric-owner --same-owner --same-permissions`. **Przed uruchomieniem PMM** porównano każdy wpis z manifestem wygenerowanym ze zweryfikowanego archiwum:

| Sprawdzenie | Wynik |
| --- | --- |
| 20 577 zwykłych plików: typ, rozmiar, SHA-256, UID/GID i tryb Unix | zgodne |
| 298 katalogów: typ, UID/GID i tryb Unix | zgodne |
| 9 symlinków: typ, UID/GID, tryb i dokładny cel `readlink` | zgodne |
| Zbiór ścieżek: brak dodatkowych i brakujących wpisów | zgodny |
| Katalog główny wolumenu: UID `1000`, GID `0`, tryb `0775` | zgodny z wpisem `.` archiwum |

## Uruchomienie i historyczny odczyt

Stan kontenera osiągnął `healthy`. Żądania wykonywano wewnątrz kontenera przez loopback, ale z weryfikacją TLS względem odtworzonego CA i deklarowanej tożsamości `192.168.1.70`; nie wyłączano weryfikacji certyfikatu. Bajty CA z archiwum były identyczne z bieżącym, przypiętym CA platformy.

- `GET /graph/api/user`: produkcyjne poświadczenie administratora zwróciło HTTP `200`, użytkownik `admin`.
- To samo żądanie z celowo błędnym hasłem: HTTP `401`.
- `GET /v1/version`: HTTP `200`, wersja `3.9.1`.

Przed porównaniem pobrano z działającego produkcyjnego PMM baseline dla **stałego okresu sprzed kopii**, nie dla bieżących danych. Odtworzony PMM zwrócił te same wyniki:

| Historyczny odczyt | Okres / chwila UTC | Wynik odtworzenia |
| --- | --- | --- |
| `max by (service_name) (mysql_up)` | 2026-09-27 20:05:00 | 6 serii, każda `1` |
| `max by (service_name) (mysql_global_status_wsrep_cluster_size)` | 2026-09-27 20:05:00 | 6 serii, każda `3` |
| `max by (node_name) (node_load1)` | 2026-09-27 20:05:00 | 10 serii |
| `POST /v1/qan/metrics:getReport`, `group_by: queryid`, `order_by: load` | 2026-09-27 19:45:00–20:10:00 | HTTP `200`, `total_rows=62`, 11 zwróconych wierszy, agregat `num_queries=39631` |

Porównanie objęło etykiety, timestampy i wartości wszystkich powyższych serii oraz pełną odpowiedź QAN: liczbę grup, zwrócone wiersze, agregaty, metryki i sparklines. Kolejność serii i wierszy znormalizowano; wartości całkowite i tekstowe porównano dokładnie, zmiennoprzecinkowe z tolerancją względną `1e-9`, bezwzględną `1e-12`. **Bez różnic.** Raport QAN obejmował 11 zwróconych wierszy, nie osobny odczyt wszystkich 62 grup. Wariant sortowania `load` odpowiada [wcześniej potwierdzonemu obejściu błędu raportu](2026-09-27-pmm-qan-report.md).

Nie sprawdzano ponownego zbierania danych z żywych klastrów ani interaktywnego logowania w przeglądarce. Dowód uwierzytelniania pochodzi z rzeczywistego API Grafany; dowód danych z rzeczywistych API metryk i QAN. Nie mierzono RTO/RPO.

## Sprzątanie i stan produkcji

Po zachowaniu wyniku usunięto i potwierdzono brak VM `10100`, jej dysku `local-zfs:vm-10100-disk-0` oraz wszystkich **9** utworzonych obrazów ISO. Bazowy obraz importowy Rocky pozostał nietknięty. Usunięto katalog roboczy próby na `x17mon`, lokalne kopie archiwum i obrazu, ISO oraz wszystkie skrypty jednorazowe. Nie kasowano kopii z MinIO; pliku poświadczeń S3 użytego do pobrania nie przenoszono do VM.

Produkcyjny `pmm-server` przed i po próbie miał ten sam ID `abf66706510e02dbaf76bdd9b1f0130a09954d68da46d035aaf2d2c7c42bc5e6`, ten sam `StartedAt=2026-09-27T20:16:50.809219828Z`, stan `running` i `healthy`. Końcowe uwierzytelnione żądanie API również działało. Próba nie zatrzymała ani nie zrestartowała produkcyjnego PMM.
