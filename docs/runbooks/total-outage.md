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
2. Jeśli `safe_to_bootstrap: 1` → bootstrap na tym węźle (`ansible-playbook playbooks/bootstrap.yml -i clusters/<name>/inventory.yml -e @clusters/<name>/cluster.yml -e bootstrap_node=<node> -e confirm=yes`)
3. Dołącz pozostałe węzły po odzyskaniu

## Scenariusz B: Wszystkie węzły down

**Zweryfikowane w praktyce na Rocky 10 (2026-07-28)** po restarcie całego labu.
Objaw: `mariadb.service` jest `enabled`, próbuje wstać i pada po ~35 s;
`grastate.dat` na KAŻDYM węźle ma `seqno: -1` i `safe_to_bootstrap: 0`.
To poprawne zachowanie — Galera odmawia zgadywania, który węzeł ma najświeższy stan.

1. **Odczytaj odzyskane pozycje.** `mariadbd --wsrep-recover` uruchomione ręcznie
   NIE wypisuje pozycji (kończy się po jednej linii `[Note]`). Pozycję wylicza
   `ExecStartPre` unitu i loguje ją do journala — dlatego czytamy stamtąd:
   ```bash
   ansible galera -i clusters/<name>/inventory.yml -b -m shell \
     -a "systemctl start mariadb >/dev/null 2>&1; journalctl -u mariadb --no-pager \
         | grep 'Recovered position' | tail -1"
   ```
   (start i tak padnie — chodzi wyłącznie o wpis `WSREP: Recovered position <uuid>:<seqno>`.)
2. **Wybierz węzeł z najwyższym seqno.** Gdy wszystkie są równe (klaster zatrzymał się
   zsynchronizowany), nie ma ryzyka utraty danych — wybierz `galera_node_idx: 1`.
   Gdy się różnią, MUSISZ wziąć najwyższy: bootstrap niższego = cicha utrata transakcji.
3. **Pokaż plan i potwierdź** (jaki węzeł, jakie seqno, czy równe).
4. **Odblokuj bootstrap na wybranym węźle** — `bootstrap.yml` celowo odmawia przy
   `safe_to_bootstrap: 0` (ISC-65), więc flagę ustawia świadomie operator:
   ```bash
   ansible <node> -i clusters/<name>/inventory.yml -b -m shell \
     -a "sed -i 's/^safe_to_bootstrap: 0/safe_to_bootstrap: 1/' /var/lib/mysql/grastate.dat"
   ansible-playbook playbooks/bootstrap.yml -i clusters/<name>/inventory.yml \
     -e @clusters/<name>/cluster.yml -e bootstrap_node=<node> -e confirm=yes
   ```
5. **Dołącz pozostałe:** `make cluster-join CLUSTER=<name>`.
6. **Odtwórz warstwy zależne.** `cluster-join` przywraca węzły do Galery, ale NIE do
   ProxySQL ani PMM. Po recovery uruchom `make cluster-proxysql CLUSTER=<name>` i
   `make cluster-monitoring CLUSTER=<name>` — bez jawnego `CLUSTER=` obie komendy
   zatrzyma straznik Makefile. Inaczej `lab-proxysql-verify` zgłosi za mało
   backendów, a metryki wsrep nie wrócą.

## Anti-criteria

- ISC-30: Split-brain nie powstaje — nigdy nie bootstrapuj dwóch węzłów jako niezależne Primary
- ISC-65: Drugi bootstrap przy istniejącym Primary jest blokowany
- `bootstrap.yml` odpytuje wszystkie osiągalne węzły i odmawia startu, jeśli którykolwiek już raportuje `Primary`.

## Weryfikacja po recovery

```bash
# Sprawdź cluster size, UUID, synced
make lab-galera-verify CLUSTER=<name>
```

## Wymagany dostęp

- SSH do wszystkich węzłów Galera
- `grastate.dat` odczyt
- `mariadb --wsrep-recover` na każdym węźle
