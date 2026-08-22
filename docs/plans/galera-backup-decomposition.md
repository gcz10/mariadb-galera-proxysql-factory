# Plan: dekompozycja runnera `galera-backup` (2741 linii → pakiet modułów)

**Status:** ✅ WYKONANY 2026-08-15 (rola galera_backup z pakietem galera_backup/, 12 modułów + wrapper). Plan spisany 2026-08-15 po audycie thermo-nuclear.
**Świadoma decyzja:** nie rozpoczynać bez dedykowanej sesji — uzasadnienie niżej.

## Problem

`roles/galera_backup/files/galera-backup` to 2741 linii w jednym pliku, mieszające
osiem niezależnych domen: parsowanie CLI, walidacja konfiguracji, backend S3/MinIO,
backend SMB, backend lokalny, silnik MariaDB/Galera (`wsrep_desync`, seqno, gcache),
writer-guard po TCP do ProxySQL, metryki Prometheus przez textfile collector oraz
obsługa sygnałów i blokad współbieżności.

## Dlaczego to NIE jest zwykły refaktor

Trzy sprzężenia, przez które „rozbij plik na moduły" nie jest operacją lokalną.

**1. Wdrożenie jest jednoplikowe.** `roles/galera_backup/tasks/main.yml` robi
`ansible.builtin.copy: src: galera-backup → dest: /opt/galera-backup/galera-backup`,
a cron woła `/opt/galera-backup/galera-backup backup <cluster>`. Pakiet wymaga albo
synchronizacji katalogu (`ansible.posix.synchronize` / `copy` z `directory`), albo
zbudowania artefaktu `zipapp`. Jedno i drugie zmienia ścieżkę wdrożenia produkcyjnego.

**2. Testy ładują plik jako JEDEN moduł.** `tests/unit/galera_backup_testlib.py`
używa `importlib.machinery.SourceFileLoader` i zwraca pojedynczą przestrzeń nazw;
136 testów odwołuje się przez `self.mod.<symbol>`. Po rozbiciu każdy symbol żyje
w innym module, więc loader i pięć plików testowych (`test_galera_backup_core`,
`_restore`, `_workflow`, `_filesystems`, `_s3`) wymagają przepisania.

**3. Z punktu 2 wynika najgorsza własność tej zmiany:** siatka bezpieczeństwa
zostałaby przebudowana RÓWNOCZEŚNIE z tym, co zabezpiecza. Zdanie „136 testów
przechodzi" przestaje być dowodem, bo to już inne testy. Nie ma bezpiecznego
podzbioru — pliku nie da się rozbić „częściowo" i zostawić jednoplikowym.

## Dlaczego mimo to warto — i dlaczego nie teraz

Wartość jest wyłącznie utrzymaniowa. Wszystkie REALNE defekty, które audyt znalazł
w tym pliku, są już naprawione i zmergowane osobno:

- zerowanie `last_success_unixtime` przy kolizji blokady (`b24a8ff`) — fałszywy
  alert braku świeżości kopii, mimo poprawnego backupu;
- osierocanie grupy procesów potomnych przy `SIGTERM` (`2a9c099`) — `tar` pisał
  dalej do skasowanego stagingu;
- brak `wsrep_desync` na czas zrzutu fizycznego (PR #6) — flow control hamował
  zapisy w całym klastrze.

Zostaje sama struktura. Rozpoczynanie wielogodzinnego refaktoru ścieżki
produkcyjnej backupu (cron 02:00 i 02:30) bez działającej siatki testów to zły
handel — zwłaszcza że stan pośredni „część modułów wydzielona, reszta w monolicie"
jest gorszy niż każdy z brzegów. Ten sam błąd popełniono w
`wip/consolidate-helpers` i trzeba go było wycofać.

## Proponowany podział (7 modułów, żaden > 250 linii)

```
roles/galera_backup/files/galera_backup/
├── __main__.py     # entrypoint CLI + obsługa sygnałów      (<150)
├── config.py       # RunConfig, S3Config, PathsConfig, walidacja JSON (<200)
├── pipeline.py     # orkiestracja etapów backup/restore     (<200)
├── engine.py
│   ├── mariabackup.py  # wrapper mariadb-backup + prepare   (<180)
│   └── galera.py       # wsrep_desync, seqno, gcache, health (<200)
├── storage/
│   ├── base.py     # interfejs StorageBackend + retencja     (<80)
│   ├── s3.py       # MinIO / AWS S3                          (<220)
│   └── filesystem.py # SMB + lokalny, atomic rotate          (<120)
└── metrics.py      # writer textfile collectora Prometheus   (<90)
```

## Kolejność wykonania

1. **Najpierw kontrakt testów, nie kod.** Przepisać `galera_backup_testlib.py` tak,
   by ładował pakiet i eksponował fasadę zgodną z dzisiejszym `self.mod` (moduł
   agregujący re-eksporty). Wtedy 136 testów przechodzi BEZ ZMIAN na monolicie
   i to jest punkt odniesienia.
2. Wydzielać moduł po module, po każdym uruchamiając pełną suitę. Fasada z kroku 1
   wchłania przenosiny, więc testy pozostają nietknięte aż do samego końca.
3. Dopiero po komplecie zdjąć fasadę i zaktualizować testy na docelowe importy —
   osobnym commitem, żeby diff refaktoru nie mieszał się z diffem testów.
4. Zmienić wdrożenie roli na artefakt `zipapp` (jeden plik na hoście, jak dziś —
   cron i ścieżka bez zmian) albo na synchronizację katalogu.
5. Weryfikacja na żywo: `make cluster-backup-configure` + realny backup na
   `finalclaude-r9` i `finalclaude-r10`, z potwierdzeniem sekwencji zdarzeń
   `galera.desync → galera.resync → backend.publish → backend.verify → state.success`
   oraz obecności artefaktu w MinIO.

## Kryteria akceptacji

- Żaden plik pakietu nie przekracza 250 linii.
- 136 testów przechodzi na każdym pośrednim commicie (dzięki fasadzie z kroku 1).
- `python3 -m pyflakes` czysty.
- Realny backup na obu klastrach kończy się `state.success`, artefakt w MinIO,
  suma `sha256` zgodna.
- Cron na hostach wskazuje na działający artefakt (`galera-backup backup <cluster>`).
