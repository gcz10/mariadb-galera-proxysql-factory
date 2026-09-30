# Cassiopeia v18 — kontrolowane cold recovery (2026-09-30)

## Wynik i zakres

**PASS:** po łagodnym zatrzymaniu wszystkich trzech usług MariaDB klastra
`cassiopeiav18-r9` istniejące `make cluster-recover` wybrało najświeższy węzeł,
odtworzyło 3/3 Primary/Synced/Ready i przywróciło ruch przez VIP. Wszystkie
potwierdzone transakcje pomiarowe zachowały pełną treść na każdym z trzech
węzłów. Sąsiedni `orionv17-r10` obsłużył wszystkie swoje próby bez błędów.

Run ID: `cold-20260930T092705Z-8c217d76`.

[Zamrożony dowód JSON](2026-09-30-cassiopeia-cold-recovery.evidence.json) zawiera
czasy komend, liczniki i zakresy ACK, odciski odczytanych wierszy, wyniki
weryfikacji każdego węzła, migawki usług oraz potwierdzenie sprzątania.
Nie zawiera środowiska ani profili z poświadczeniami.

| Pomiar | Cassiopeia | Sąsiedni Orion |
|---|---:|---:|
| Próby transakcji przez VIP | 384 | 468 |
| Potwierdzone commity (ACK) | 373 | 468 |
| Timeouty / wynik niepewny u klienta | 11 | 0 |
| Brakujące ACK na którymkolwiek węźle | 0 | 0 |
| Różnice treści na którymkolwiek węźle | 0 | 0 |
| Dodatkowe wiersze po recovery | 0 | 0 |
| Największa przerwa między ACK | 97,433 s | 2,751 s |

**Granica dowodu:** to kontrolowana, pełna awaria usług, nie utrata zasilania VM,
SIGKILL, uszkodzenie dysku ani restore z kopii. Integralność dotyczy mierzonego
workloadu, nie porównania każdej istniejącej tabeli ze wzorcem. Nie zadeklarowano
SLA RTO dla pełnej awarii; próg `availability.rto_node_failure` nie jest takim SLA.

## Preflight i bezpieczeństwo

Przed zatrzymaniem potwierdzono 3/3 zdrowych węzłów i poprawną konfigurację
ProxySQL obu najemców. Dla Cassiopei sprawdzono także kopię
`galera-cassiopeiav18-r9-20260930-023002` w buckecie
`cassiopeiav18-galera-backups`: pobranie, SHA-256 i metadane przechodzą;
format szyfrowania `aes-256-gcm-stream-pbkdf2-sha256`. Kopii nie przywracano
w tej próbie.

```bash
make lab-galera-verify lab-proxysql-verify lab-backup-verify CLUSTER=cassiopeiav18-r9
make lab-galera-verify lab-proxysql-verify CLUSTER=orionv17-r10
```

Wszystkie komendy zakończyły się `rc=0`. Stop dotyczył wyłącznie usług
`mariadb` na `c18db1`, `c18db2`, `c18db3`, kolejno. Nie zatrzymywano maszyn,
Oriona, ProxySQL, Keepalived ani PMM i nie rekonfigurowano warstwy wspólnej.
Przed wywołaniem recovery niezależnie potwierdzono na 3/3 węzłach:
`inactive/dead`, `MainPID=0`, brak procesu `mariadbd`, odrzucenie lokalnego
połączenia przez socket (kod 2002).

Stan `grastate.dat` po stopie, wspólny UUID
`90150c56-b60d-11f1-8c6d-429633e4a3f8`:

| Węzeł | seqno | safe_to_bootstrap |
|---|---:|---:|
| c18db1 | 359383 | 0 |
| c18db2 | 359387 | 0 |
| c18db3 | **359391** | **1** |

Automat wybrał **c18db3**, nie pierwszy węzeł inwentarza. Wywołanie:

```bash
make cluster-recover CLUSTER=cassiopeiav18-r9 CONFIRM=yes
```

Bez `BOOTSTRAP_NODE`, ręcznej zmiany `safe_to_bootstrap` i omijania bramki
żywego Primary. Artefakt wyboru: recovery run ID
`6bec677d-2616-44bb-a4cb-8cc219a41c80`, wygenerowany o `10:11:47 UTC`,
`node=c18db3`. Po bootstrapie c18db3 dołączyły c18db1 i c18db2. Wszystkie
rekapitulacje Ansible: `failed=0`, `unreachable=0`; końcowy `rc=0`.

## Przebieg i znaczenie czasów

Wszystkie poniższe etykiety czasu: 2026-09-30, UTC.

| Zdarzenie | Czas |
|---|---|
| Start obserwatora na x17app | 10:08:32.140314 |
| Gotowość: po 5 ACK każdego najemcy | 10:08:36.397496 |
| Stop c18db1 | 10:10:26.278585–10:10:29.350556 |
| Stop c18db2 | 10:10:29.351213–10:10:32.109186 |
| Stop c18db3 | 10:10:32.109832–10:10:33.920431 |
| Ostatni ACK Cassiopei przed przerwą, ID 122 | 10:10:33.381750 |
| Niezależny odczyt potwierdzający wszystkie usługi down | 10:11:32 |
| Start kanonicznego `make cluster-recover` | 10:11:35.374819 |
| Pierwszy ACK Cassiopei po przerwie, ID 134 | 10:12:10.815284 |
| Koniec `make cluster-recover`, `rc=0` | 10:12:53.652549 |
| Koniec obserwatora, bez błędu fatalnego | 10:16:21.628847 |

Czas samej komendy recovery: **78,276628750 s**, mierzony zegarem
monotonicznym kontrolera. Klient odzyskał możliwość zatwierdzania transakcji
jeszcze przed dołączeniem wszystkich węzłów i końcem komendy.

Największa przerwa między ACK Cassiopei: **97,433296737 s**, różnica
znaczników monotonicznych tego samego obserwatora na x17app. Nie jest to
dokładny czas fizycznej niedostępności: obejmuje próbkowanie, timeouty klienta,
czas sprawdzania stanu down i zwłokę przed uruchomieniem recovery. Nie odejmowano
znaczników monotonicznych różnych hostów ani ich etykiet UTC.

Znaczniki operacyjne nie zastępują rzeczywistych czasów komend:
`outage_start` zapisano o 10:09:33, przed rozpoczęciem stopu;
`recovery_end` o 10:14:50, już po zakończeniu recovery. Ich odstęp nie jest
RTO. Odpowiednie rzeczywiste granice znajdują się w tabeli i dowodzie JSON.

## Metoda pomiaru i integralność

Obserwator wykonywał niezależne strumienie dla obu najemców na x17app,
przez VIP `192.168.1.74:6033`, co około sekundę. Każda próba otwierała nowe
połączenie MariaDB z wymaganym TLS i weryfikacją certyfikatu frontendu;
automatyczny reconnect był wyłączony. Limit połączenia: 3 s; limit procesu
SQL: 8 s. Nie zmieniano deklarowanego `tls.mode=disabled` Cassiopei — badany
TLS dotyczył ścieżki klient→VIP.

Każda jawna transakcja zapisywała unikalne ID próby, payload, run ID,
najemcę i znacznik startu. ACK zapisywano dopiero po udanym finalnym `COMMIT`
i `rc=0`. Timeout lub przerwane połączenie nie dowodzą, czy serwer zatwierdził
zapis: takie próby pozostają niepewne, nie są liczone jako utracone ACK.

Po zatrzymaniu obserwatora odczytano obie tabele oddzielnie na każdym z sześciu
węzłów, z `wsrep_sync_wait=1`. Porównanie obejmowało **każde ID oraz pełną
treść payloadu, run ID, najemcę i znacznik startu**, nie tylko liczbę wierszy.

- Cassiopeia: ACK ID **1–122 i 134–384**. Wszystkie **122 ACK sprzed przerwy**
  oraz **251 po powrocie** zgodne na 3/3 węzłach. Próby **123–133**: 11
  timeoutów bez ACK; po recovery żaden z tych ID nie występuje w tabeli.
- Orion: ACK ID **1–468**, pełna zgodność na 3/3 węzłach.
- Zero ACK bez rekordu wyniku, niedokończonych zamiarów i ACK bez wymaganego
  TLS. Zero nieznanych lub dodatkowych wierszy.

Dodatkowe, niezależne porównanie zamrożonych odczytów SQL z logiem ACK
potwierdziło identyczność wszystkich pól. SHA-256 uporządkowanego zbioru
wierszy był identyczny ze zbiorem oczekiwanym na każdym z trzech węzłów:

| Najemca | SHA-256 zbioru wierszy |
|---|---|
| Cassiopeia | `f076447b5b31eb4e3ee9fce148f44df78648774d4bab6197db12a46061152dbe` |
| Orion | `221b465e282d163bffe982ad9e56165c0d94698ba4a171db095e9abdae5d16fd` |

Kodowanie zbioru: UTF-8, wiersze zakończone LF, kolejność po numerycznym ID;
kolumny rozdzielone tabulatorem: ID, uppercase HEX(payload), uppercase
HEX(run ID), uppercase HEX(najemcy), start monotonic_ns. JSON zachowuje też
odciski logów ACK; surowe logi i odczyty nie są dołączone do repozytorium.
Same odciski nie pozwalają ponownie wykonać porównania po usunięciu danych.

**Korekta instrumentacji, bez powtarzania awarii:** kolektor wymagał poprawy
odczytu płaskich pól czasu snapshotu oraz jawnego wyłączenia TLS wyłącznie
w odczytach przez lokalny Unix socket. Wstępnych wyników `UNDETERMINED` nie
uznano za PASS. Końcowy PASS opiera się na udanych odczytach wszystkich
sześciu węzłów i niezależnym porównaniu z niezmienionymi ACK. Nie zmieniano
usług ani sieciowej polityki TLS; lokalny verifier wymusza `--protocol=SOCKET`,
bez możliwości fallbacku na TCP. Unix socket jest
[bezpiecznym transportem według kontraktu MariaDB](https://github.com/mariadb-corporation/mariadb-docs/blob/main/server/ha-and-performance/optimization-and-tuning/system-variables/server-system-variables.md#require_secure_transport).

## Sąsiedni Orion i warstwa wspólna

Orion: **468/468 udanych prób, 0 błędów, 0 timeoutów**. Najdłuższy odstęp
między ACK: **2,751435132 s** przy nominalnej kadencji 1 s — nie deklarujemy
zerowego opóźnienia ani idealnie równego próbkowania. Wszystkie 468 pełnych
wierszy zachowane na 3/3 węzłach.

`MainPID`, `InvocationID`, czas startu i stan unitów MariaDB Oriona pozostały
identyczne przed próbą, podczas recovery i po nim. Tożsamości obu wspólnych
ProxySQL i Keepalived były identyczne przed i po:

| Host / usługa | MainPID | InvocationID |
|---|---:|---|
| o17db1 / mariadb | 96603 | `9316c04264d54d2d902b889e64c909bd` |
| o17db2 / mariadb | 89422 | `b25c5d8f2c97454389edadbf3b9eda39` |
| o17db3 / mariadb | 90154 | `43ecfbe33e7245c9a3910d47b0a677ec` |
| x17p1 / proxysql | 1165 | `c5c6aed58a984196ba7b8fee05f1255f` |
| x17p1 / keepalived | 447839 | `0a19f7ebe6d3439c984aa9daa9e42719` |
| x17p2 / proxysql | 17549 | `ca29ca96ac414d3a89db39759fb0a863` |
| x17p2 / keepalived | 433924 | `282086f8918849b3af2cbfd173aa8b41` |

UUID Oriona pozostał `492f6568-ae41-11f1-bd0e-e7772e3e342d`;
Cassiopei — `90150c56-b60d-11f1-8c6d-429633e4a3f8`.

## Stan po recovery i sprzątanie

```bash
make lab-galera-verify lab-proxysql-verify lab-endpoint-verify lab-monitoring-verify CLUSTER=cassiopeiav18-r9
make lab-galera-verify lab-proxysql-verify lab-endpoint-verify CLUSTER=orionv17-r10
```

Oba przebiegi: `rc=0`. Galera 3/3 Primary/Synced/Ready; ProxySQL ma poprawnego
writera, trzy zdrowe backendy, zgodny runtime i konfigurację dyskową;
VIP/frontend TLS PASS. PMM Cassiopei: natywna sonda, świeże metryki Galery,
QAN i wymagane reguły alertingu PASS. Rejestracje backendów i monitoringu
przetrwały stop usług — nie wykonywano `cluster-proxysql` ani
`cluster-monitoring` jako naprawy po tej próbie.

Po kompletnym porównaniu usunięto wyłącznie tabele ze znacznikiem właściciela
tego run ID:

- `isa_test.cold_20260930T092705Z_8c217d76_cv18`;
- `isa_test.cold_20260930T092705Z_8c217d76_ov17`.

Brak każdej tabeli potwierdzono na wszystkich trzech węzłach jej najemcy.
Usunięto prywatne profile klienta i katalog obserwatora na x17app. Zamrożony
publiczny dowód zachowano przed usunięciem lokalnych sond, logów tymczasowych
i wygenerowanych artefaktów wyboru bootstrapu.

**Konsekwencja dla gotowości projektu:** istnieje aktualny dowód kanonicznego
cold recovery po pełnym, łagodnym stopie usług oraz zachowania ACK i ciągłości
sąsiedniego najemcy. Nie zamyka to decyzji o profilu produkcyjnym/SLA,
nieczystej utraty wszystkich VM, odtwarzania całego klastra z kopii ani
zaakceptowanego niedoboru gcache.
