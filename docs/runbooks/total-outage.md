# Runbook: Total Outage — utrata quorum i pełna awaria klastra

**Status:** Aktualny (F4/F9/F13 complete)
**Powiązane ISC:** ISC-17, ISC-30, ISC-55

## Przeznaczenie

Klaster utracił quorum (większość węzłów niedostępna) lub cała baza jest niedostępna.

## Diagnoza

```bash
# Na każdym węźle sprawdź:
mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "SHOW STATUS LIKE 'wsrep_cluster_status'"
# non-Primary = utrata quorum
# wsrep_ready=OFF = zapisy zablokowane (ISC-17)
```

## Scenariusz A: Utrata większości (2/3 węzłów down)

1. Sprawdź `grastate.dat` i `safe_to_bootstrap` na ostatnim węźle
2. Jeśli `safe_to_bootstrap: 1` → bootstrap na tym węźle (`bootstrap.yml -l <node> -e confirm=yes`)
3. Dołącz pozostałe węzły po odzyskaniu

## Scenariusz B: Wszystkie węzły down

1. Zbierz `grastate.dat`, `safe_to_bootstrap`, `--wsrep-recover` z każdego węzła
2. Wybierz węzeł z najnowszym seqno
3. Pokaż plan i wymagaj potwierdzenia
4. Bootstrap na wybranym węźle
5. Dołącz pozostałe

## Anti-criteria

- ISC-30: Split-brain nie powstaje — nigdy nie bootstrapuj dwóch węzłów jako niezależne Primary
- ISC-65: Drugi bootstrap przy istniejącym Primary jest blokowany

## Weryfikacja po recovery

```bash
# Sprawdź cluster size, UUID, synced
make lab-galera-verify CLUSTER=<name>
```

## Wymagany dostęp

- SSH do wszystkich węzłów Galera
- `grastate.dat` odczyt
- `mariadb --wsrep-recover` na każdym węźle
