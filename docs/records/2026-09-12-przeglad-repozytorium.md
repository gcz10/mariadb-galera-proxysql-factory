# Przegląd repozytorium — 2026-09-12 (baza `bbc3eda`)

Przegląd całego repozytorium, nie tylko ostatniego commita. Obszary: cykl życia
maszyn, backup i odtwarzanie, monitoring backupu, ProxySQL/TLS, sondy weryfikacyjne.

Każde ustalenie poniżej zostało odtworzone lokalnie: atrapy procesów zewnętrznych,
lokalny `ansible-playbook` na inwentarzu z `connection: local`, SQLite w pamięci.
Żadna reprodukcja nie dotknęła działającej floty (brak SSH, Proxmoksa, MinIO, PMM).

## Podsumowanie

| Priorytet | Liczba | Charakter |
|---|---|---|
| HIGH | 4 | zakres kasowania, izolacja najemców, rotacja kluczy, fałszywy PASS przy nieudanym przywróceniu |
| MEDIUM | 6 | wiarygodność sond i monitoringu, fail-open bramki VIP, sekret w argv |

Kolejność napraw: najpierw kasowanie danych i rotacja poświadczeń (1–3), potem
wiarygodność testów destrukcyjnych (4, 6, 7), na końcu pozostałe.

---

## HIGH

### 1. Wznowiony teardown kasuje wolumeny spoza wskazanego zakresu

Lista VMID trafiała do pliku wznowienia **przed** bramką potwierdzenia, a wznowienie
przyjmowało ją w całości, bez porównania z listą węzłów żądaną w bieżącym wywołaniu.

Reprodukcja (atrapy `terraform` i `curl`): odrzucone wywołanie dla całego roota
zapisało VMID `991,992`; kolejne wywołanie wyłącznie dla nieistniejącego już węzła
wysłało `DELETE` również dla wolumenu sąsiada `992`, obecnego w stanie Terraform.

Skutek: utrata wolumenów maszyny, której operator nie wskazał.

### 2. Derejestracja najemcy usuwa wpisy TLS innego najemcy

Czyszczenie wpisów `mysql_servers_ssl_params` używało `LIKE` na nazwie klastra.
W SQLite `_` jest wieloznacznikiem pojedynczego znaku
([dokumentacja](https://www.sqlite.org/lang_expr.html#the_like_glob_regexp_match_and_extract_operators)),
a walidator dopuszcza nazwy z `_`.

Reprodukcja: nazwa `tenant_a` przeszła walidację; wygenerowane zapytanie usunęło
w SQLite wiersze zarówno `tenant_a`, jak i odrębnego `tenant-a`.

Skutek: derejestracja jednego najemcy odbiera zaufanie TLS sąsiadowi na wspólnej parze.

### 3. Odwołanie starego klucza MinIO następuje mimo nieudanej dystrybucji nowego

Play odwołujący stare konta serwisowe był ostatni w kolejności, ale nie wymagał
powodzenia wcześniejszej dystrybucji. Domyślnie Ansible usuwa tylko host, który
zawiódł, i kontynuuje kolejne play'e
([dokumentacja](https://github.com/ansible/ansible-documentation/blob/devel/docs/docsite/rst/playbook_guide/playbooks_error_handling.rst)).

Reprodukcja (lokalny `ansible-playbook` na grafie play'ów z repozytorium): kandydat
zakończył dystrybucję błędem, a koordynator mimo to wykonał etap odwołania.

Skutek: węzeł zostaje z odwołanym kluczem; jego backup po wyborze na donora nie
uwierzytelni się w MinIO.

### 4. Sondy destrukcyjne akceptują nieudane przywrócenie węzła

ProxySQL oznaczał węzeł jako przywrócony także wtedy, gdy żaden start usługi się nie
powiódł, a port administracyjny pozostał zamknięty. Galera przy węźle, który nie wrócił
do `Synced`, wypisywała jedynie ostrzeżenie.

Reprodukcja (podstawione wykonanie poleceń, zegar wirtualny): oba scenariusze
zakończyły się `PASS` i kodem `0`.

Skutek: kolejne przebiegi mierzą zdegradowaną warstwę, a raport twierdzi, że
failover jest w pełni sprawny.

---

## MEDIUM

### 5. Monitoring backupu zakłada stałego producenta metryk

Donor jest wybierany dynamicznie, natomiast sprzątanie metryk w Ansible i sonda PMM
były przypięte do hosta harmonogramu i pierwszego hosta z inwentarza.

Reprodukcja: przy harmonogramie `g1` i donorze `g2` rzeczywisty task sprzątający usunął
świeżą metrykę donora, zostawiając starą; blok sondy PMM odrzucił pojedynczą świeżą
serię wyłącznie z powodu nazwy węzła.

Skutek: fałszywy alarm „backup nieaktualny” i czerwona bramka przy sprawnych kopiach.

### 6. Tryb „hard” uznaje awarię za wykonaną bez sprawdzenia

Wynik polecenia wymuszającego restart maszyny nie był sprawdzany, a jedynym warunkiem
było wznowienie zapisów przez workload.

Reprodukcja: odrzucone uruchomienie polecenia (`FAILED`, kod 2) nadal dało `PASS`.

Skutek: przebieg raportuje odporność na utratę maszyny, która nigdy nie nastąpiła.

### 7. Nieczytelny log transakcji zamienia się w „zero utraconych”

Błąd odczytu logu dawał pustą listę potwierdzonych transakcji, a asercja integralności
na pustej liście przechodzi.

Reprodukcja: po potwierdzonych zapisach końcowy odczyt zwrócił `Permission denied` →
`PASS … 0 committed transactions … (0 lost)`.

Skutek: utrata materiału dowodowego jest raportowana jako dowód poprawności.

### 8. Bramka VIP działa fail-open bez pliku poświadczeń

Brak pliku poświadczeń administracyjnych pomijał kontrolę backendów; wystarczał otwarty
port klienta.

Reprodukcja: przy zerowej liczbie zdrowych writerów bramka zwracała `1` z plikiem
i `0` bez pliku.

Skutek: VIP zostaje na instancji odpowiadającej błędem na każde zapytanie.

### 9. Sonda gcache zatwierdza klaster po odczycie jednego węzła

Reprodukcja: `g1=512M`, `g2=g3=128M`, wymagane `344M` → sonda odpytała wyłącznie `g1`
i zwróciła `PASS`.

Skutek: rozjazd konfiguracji między węzłami przechodzi bramkę; węzeł wracający po
awarii wpada w pełny SST zamiast IST.

### 10. Hasło aplikacyjne trafia do argumentów procesu

Sonda ProxySQL budowała plik poświadczeń poleceniem powłoki przekazanym jako argument
`ansible`. Docelowe uprawnienia pliku nie cofają ujawnienia w `argv`.

Reprodukcja: przechwycenie argumentów przed uruchomieniem procesu pokazało pełne hasło.

Skutek: każdy użytkownik hosta kontrolnego widzi sekret przez `ps` w trakcie przebiegu.

---

## Zgłoszenia rozpatrzone, nieprzyjęte jako ustalenia

Poniższe hipotezy pojawiły się w przeglądzie, ale **nie zostały potwierdzone
reprodukcją** i nie wchodzą do listy powyżej. Nie są przez to uznane za nieszkodliwe.

- **Wybór kopii po niepodpisanym `created_unixtime`.** Poświadczenie publikacji leży na
  każdym kandydacie i pozwala na zapis w całym prefiksie klastra, więc dostęp do podmiany
  jest realny. Nie zweryfikowałem natomiast, czy zarządzany bucket wymusza wersjonowanie
  lub blokadę obiektów; odmowa nadpisania po stronie runnera nie jest tu dowodem, bo
  nadpisanie nie wymaga udziału runnera. Temat do osobnej weryfikacji.
- **Nieescapowane `app_user` i `cluster.name` w poleceniach administracyjnych ProxySQL.**
  Wymaga rozstrzygnięcia, czy operator najemcy jest oddzielną granicą uprawnień wobec
  administratora warstwy wspólnej.
- **Sonda firewalla: brak odpowiedzi hosta raportowany jako naruszenie polityki**
  oraz diagnoza braku modułów jądra wyprowadzona z ciszy. Nie odtworzyłem częściowej
  nieosiągalności.
- **Detektor mutacji w sondzie dryfu nie rozpoznaje kwalifikowanych `UPDATE`.**
  Luka w ochronie przed regresją; brak takiego polecenia w bieżącym playbooku.
- **Czyszczenie katalogu danych przed kopiowaniem wsteczym przy odtwarzaniu.**
  Dotyczy hosta odtwarzania, którego zawartość jest z założenia nadpisywana.
- **Kolizja zakresów hostgroup przy derejestracji.** Rozłączność jest sprawdzana
  statycznie, nie w momencie kasowania; nie odtworzyłem konfiguracji z kolizją.

## Obserwacja poza dziesiątką (potwierdzona czytaniem, bez fixu)

**`chaos-failover.py`, tryb `hard`: `WORKLOAD_HOST` może być zabitym writerem.**
`WORKLOAD_HOST` to pierwszy węzeł grupy `galera` (L54), a `killed_host` to aktywny
writer wybrany z mapowania IP (L299) — gdy pokrywają się, `hard` restartuje maszynę,
na której działa workload i leży `LOG_REMOTE`. Potwierdzone czytaniem, nie reprodukcją
na flocie. Klasyfikacja: **fałszywa czerwień, nie fałszywy PASS** — każda gałąź kończy
się głośnym błędem, nigdy zielonym werdyktem:

- `rm -f /tmp/workload.run` z `check=True` (L384) na niedostępnym hoście rzuca wyjątek
  → `finally` przywraca węzeł → przebieg przerywa się z `exit 1`;
- nawet gdyby maszyna wróciła na czas, `nohup` workload jest martwy → brak commita
  z `t > kill_ts` → `resumed=False` (L402) trafia do `failures`.

To odróżnia ją od dziesięciu ustaleń powyżej: tamte wszystkie były fałszywymi PASS-ami
lub fail-open. Tu sonda w wariancie kolizji przerywa przebieg zamiast coś zaświadczać,
więc nie ma ryzyka cichego przyjęcia zdegradowanego stanu. Poza zakresem tej rundy —
naprawa (osadzenie workloadu na węźle spoza zbioru `killed_host`) to osobna zmiana
sondy, nie bramki bezpieczeństwa.

## Ograniczenia przeglądu

Nie jest to pełny audyt bezpieczeństwa modelu uprawnień najemców ani odporności kopii
zapasowych na złośliwego operatora magazynu. Nie uruchomiono żadnej destrukcyjnej sondy
na działającej flocie, więc żadne ustalenie nie orzeka o bieżącym stanie klastrów.

## Stan napraw (ta sama sesja)

Wszystkie dziesięć ustaleń naprawiono, każde z regresją, która przed poprawką jest
czerwona, a po niej zielona.

| # | Poprawka | Regresja |
|---|---|---|
| 1 | Plik wznowienia teardownu powstaje dopiero po potwierdzeniu i niesie nazwy węzłów; wznowienie odrzuca wpisy spoza wskazanego zakresu oraz w starym formacie | `tests/unit/test_pve_teardown_contract.py::PveTeardownResumeScopeTests` |
| 2 | Czyszczenie wpisów TLS porównuje dosłowny prefiks katalogu najemcy zamiast `LIKE` | `tests/unit/test_deregister_ssl_ca_scope.py` |
| 3 | `any_errors_fatal` na play'ach dystrybucji sekretów — nieudana konwergencja zatrzymuje przebieg przed odwołaniem klucza | `tests/unit/test_backup_rotation_ordering.py` |
| 4 | Przywrócenie węzła znaczy „usługa znów odpowiada”: brak portu administracyjnego ProxySQL i brak powrotu do `Synced` to teraz porażki | `tests/unit/test_chaos_proxysql_failover_safety.py`, `tests/unit/test_chaos_failover_verdict.py` |
| 5 | Metrykę kopii posiada donor: węzeł pominięty w elekcji usuwa własny plik, sonda PMM ocenia serię z dowolnego węzła klastra | `tests/unit/test_backup_metric_ownership.py` |
| 6 | Tryb `hard` czeka, aż maszyna faktycznie przestanie odpowiadać | `tests/unit/test_chaos_failover_verdict.py` |
| 7 | Końcowy odczyt logu jest surowy; pusty zbiór to niewykonalna asercja, nie sukces | `tests/unit/test_chaos_failover_verdict.py` |
| 8 | Bramka VIP bez pliku poświadczeń kończy się błędem | `tests/unit/test_platform_vip_contract.py` |
| 9 | Sonda gcache czyta wszystkie węzły i orzeka po najmniejszej wartości | `tests/unit/test_probe_gcache_fleet.py` |
| 10 | Profil klienta jedzie plikiem `copy`, nie argumentem powłoki | `tests/unit/test_chaos_proxysql_failover_safety.py` |

Weryfikacja lokalna: pełny przebieg kroków CI z `.github/workflows/ci.yml`
(job `validate` + `lint`), wykonany dosłownie, w izolowanym venv z pinami z
`requirements-dev.txt` na Pythonie 3.14 — **20/20 kroków PASS**. Kluczowe:
`tests/unit` — 762 testów, `OK`; `pyflakes` — 0 naruszeń; `ansible-lint playbooks roles`
— 0 failure(s), 0 warning(s) (profile `production`); `make verify-*` — wszystkie statyczne
bramki zielone; `ansible --syntax-check`, walidatory schematów, sondy adresu i hooka —
wszystkie PASS. Żadnego destrukcyjnego przebiegu na flocie: reprodukcje i testy
opierały się na atrapach procesów, lokalnym `ansible-playbook` (`connection: local`)
i SQLite w pamięci.
