# Zasady floty

Ten plik zawiera **wyłącznie to, co nie zmienia się razem z flotą**: limity,
polityki i granice własności. Nie ma tu spisu maszyn ani klastrów — i nie
powinien się pojawić.

Do 2026-08-26 stał tu ręcznie wpisywany spis maszyn. Ogłaszał jako aktywny stack
skasowany dwa dni wcześniej i wyliczał najemców zniszczonych tydzień wcześniej.
Przyczyna była strukturalna: klastry powstają i znikają w tym repo w każdej
sesji, więc zapisane zdjęcie floty gnije szybciej, niż ktokolwiek je poprawia.
Tamta treść jest zamrożona w `docs/records/2026-08-26-fleet-snapshot.md`.

## Trzy źródła prawdy

| Pytanie | Źródło | Uwaga |
|---|---|---|
| Jaki jest zamiar? | `clusters/<nazwa>/`, `platform/<nazwa>/` | walidowane schematem, sprawdzane sondami |
| Co naprawdę teraz działa? | `make fleet-state` | odczyt z hypervisora, nic nie zapisuje |
| Jak było kiedyś? | `docs/records/<data>-*.md` | zamrożone, nigdy nie aktualizowane |

Model najemców — jedna warstwa wspólna, wielu najemców, rozdział wyłącznie przez
rozłączność hostgroup i kont — opisuje README, sekcja „Warstwa wspolna".
Egzekwują go `make verify-proxysql-tenancy` i `make verify-address-collision`.

## Limit zasobów

Operator: **max 5 GB RAM na VM**, podnoszone tylko na dowód.

- Host monitoringu 5 GB — PMM zajmował 1.4 GB z 3 GB przy zerze usług.
- ProxySQL 3 GB — preflight wymaga `ansible_memtotal_mb >= 2048`, a przydział
  2048 MB daje 1769 MB widzianych przez OS. W próg trzeba uderzyć z zapasem.
- Galera 3 GB, `innodb_buffer_pool_size: 768M` — zapas na `mariabackup` w SST.

## Strojenie MariaDB — czego nie robimy

Ogólnych poradników wydajnościowych nie stosujemy hurtowo: są pisane dla
dedykowanych serwerów, a węzeł laboratorium ma 3 GB RAM, Galerę, SST, backup
i QAN na tej samej maszynie. Obowiązująca konfiguracja (`768M` buffer pool,
`256M` redo, `max_connections=100`) jest ograniczona pomiarem i bramkami.

Zmiana strojenia to **pojedynczy parametr, po baseline i benchmarku**. Nie
włączamy `innodb_dedicated_server` ani query cache. Bezpieczny domyślny
`innodb_flush_log_at_trx_commit` to `1`; laboratoria mają jawny opt-out `0`,
a produkcja wymaga `1` albo `durability_risk_accepted: true`.

Rozmiar `gcache` nie jest wpisywany z palca — liczy go sonda ISC-68 z
mierzonego `write_rate` na okno IST.

## Zmiana obrazu PMM jest fail-closed

`platform-infra` przed podmianą obrazu klasyfikuje parę kontener + wolumen
danych. Dozwolony jest tylko stan całkowicie pusty albo kompletna para;
osierocony wolumen, brak danych przy istniejącym kontenerze i błąd Dockera
blokują operację **przed** zmianą obrazu. Upgrade tworzy oznaczony wolumen
backupu z timestampem i wersją, montuje źródło read-only, weryfikuje marker
i katalogi danych przed odtworzeniem Compose, a błąd sprzątania raportuje
zamiast ukrywać. Retencja zachowuje dwie najnowsze zweryfikowane generacje;
backupy ręczne bez oznaczenia pozostają nietknięte.

Obowiązuje kolejność: **PMM Server >= PMM Client**. Serwer aktualizujemy przed
klientami, wersje pochodzą z aktywnego lockfile'a.

## Poza tą automatyzacją — nie dotykać

| Grupa | VMID | Uwaga |
|---|---|---|
| Poprzednicy ISA | `9010-9012`, `9040-9042`, `9050`, `9060` | poza pulą `claude-isa`, sprzed tej automatyzacji |
| RKE2 lab | `9000`, `9201-9235` | — |
| GitLab | `9301` | — |
| `qoder-*` | `9601-9620`, `9999` | przenumerowane z `95xx` 2026-08-02 przez kogoś innego |

## Reguła stała

Każdy zasób tworzony przez tę automatyzację należy do puli `claude-isa`.
Przynależność do puli to **asercja** („to jest nasze"), nigdy źródło listy do
skasowania. Lista do skasowania pochodzi zawsze z definicji, którą kasujesz —
`terraform destroy` dla maszyn terraformowych albo jawna lista VMID dla maszyn
z REST API.

Wolny VMID to nie to samo co wolny magazyn: przed utworzeniem maszyny sprawdź
także wolumeny (`local-zfs`), bo osierocony `vm-<id>-cloudinit` zatrzyma
`terraform apply` w połowie. Procedurę opisuje
`docs/runbooks/machines-from-elsewhere.md`.
