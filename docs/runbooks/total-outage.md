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

## Scenariusz A: Utrata większości (2/3 węzłów down / utrata quorum)

Gdy klaster traci większość (2 z 3 węzłów), ocalały węzeł przechodzi w stan `wsrep_cluster_status = non-Primary`
i blokuje operacje (`wsrep_ready = OFF`). To zamierzone zachowanie Galery (fail-closed fencing), chroniące
przed split-brain (ISC-17, ISC-30).

### Warunki bezpieczeństwa i rozróżnienie strażników

1. **Wymóg zewnętrznego sfencowania (fencing wykluczający aktywny Primary):**
   Samo odcięcie ocalałego węzła od replikacji nie odcina ruchu klientów ani ProxySQL od pozostałych maszyn.
   Gdyby odcięte 2 węzły nadal komunikowały się ze sobą, posiadałyby większość (2/3) i tworzyły działający
   Primary Component. Zanim podejmiesz próbę bootstrapu na ocalałym węźle, MUSISZ niezawodnie zatrzymać
   pozostałe hosty (np. wyłączenie VM w hypervisorze, fencing IPMI/PDU), aby wykluczyć istnienie aktywnego
   Primary Component przyjmującego zapisy.

2. **Weryfikacja najświeższego stanu (latest-state verification):**
   Ocalały węzeł NIE jest automatycznie węzłem o najświeższym stanie — pozostałe węzły mogły zatwierdzić
   nowsze transakcje tuż przed awarią lub ocalały węzeł mógł wypaść z synchronizacji wcześniej. Jeśli dyski
   pozostałych węzłów są dostępne, zweryfikuj i porównaj pozycje (jak w Scenariuszu B krok 1–2).
   Jeśli nie można potwierdzić najświeższego stanu, zatrzymaj tę procedurę; odzyskiwanie z możliwą
   utratą transakcji wymaga osobnej, jawnej decyzji o akceptacji utraty danych.

3. **Rozróżnienie klasyfikacji sondy a potwierdzenia operatora:**
   - **Sonda wykrywa brak usługi (`galera_state_down_verified`):** Gdy hosty odpowiadają po SSH, a sonda
     `tasks/galera_state_probe.yml` zwraca `ERROR 2002` (błąd połączenia z lokalnym gniazdem MariaDB),
     węzły trafiają do `galera_state_down_verified`. Wówczas zbiór `galera_state_unreachable` jest pusty
     i strażnik `Audit#2` w `playbooks/bootstrap.yml` przechodzi bez flagi potwierdzenia.
   - **Węzły nieosiągalne po SSH (`galera_state_unreachable`):** Gdy hosty nie odpowiadają na SSH,
     strażnik `Audit#2` bezwzględnie wymaga jawnej flagi operatora: `-e bootstrap_confirm_all_down=true`.
     Flagę wolno przekazać WYŁĄCZNIE po uprzednim, wiarygodnym sfencowaniu maszyn wykluczającym aktywny Primary.
   - **Stan nieznany (`galera_state_unknown`):** Żadna flaga nie zdejmuje blokady przy niejednoznacznej
     odpowiedzi bazy (np. błąd uprawnień). Sytuację na takim węźle należy zbadać przed jakimkolwiek bootstrapem.

### Procedura

1. **Sfencuj wyłączone węzły i zatrzymaj bazę na węźle bootstrap:**
   - Potwierdź out-of-band (hypervisor/IPMI), że pozostałe węzły są definitywnie zatrzymane.
   - Jeśli MariaDB na ocalałym węźle nadal działa w non-Primary, zatrzymaj usługę przez systemd:
     ```bash
     ansible <node> -i clusters/<name>/inventory.yml -b -m systemd -a "name=mariadb state=stopped"
     ```

2. **Sprawdź `grastate.dat` i odblokuj bootstrap:**
   ```bash
   ansible <node> -i clusters/<name>/inventory.yml -b -m command -a "cat /var/lib/mysql/grastate.dat"
   ```
   Jeśli `safe_to_bootstrap: 0`, a węzeł został zweryfikowany jako posiadający najświeższy stan:
   ```bash
   ansible <node> -i clusters/<name>/inventory.yml -b -m shell \
     -a "sed -i 's/^safe_to_bootstrap: 0/safe_to_bootstrap: 1/' /var/lib/mysql/grastate.dat"
   ```

3. **Bootstrap ocalałego węzła:**
   ```bash
   # Wymagane SST_PASSWORD w środowisku (strażnik bootstrap.yml):
   export SST_PASSWORD="<haslo_sst>"

   # Bezpośrednie wywołanie playbooka (z flagą potwierdzenia wymaganą przy nieosiągalnych SSH węzłach):
   ansible-playbook playbooks/bootstrap.yml -i clusters/<name>/inventory.yml \
     -e @clusters/<name>/cluster.yml \
     -e bootstrap_node=<node> \
     -e confirm=yes \
     -e bootstrap_confirm_all_down=true

   # LUB równoważne wywołanie przez cel Makefile:
   make cluster-bootstrap CLUSTER=<name> CONFIRM=yes \
     ANSIBLE_OPTS="-e bootstrap_node=<node> -e bootstrap_confirm_all_down=true"
   ```

4. **Weryfikacja jednoelementowego Primary Component:**
   ```bash
   ansible <node> -i clusters/<name>/inventory.yml -b -m shell \
     -a "mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e \"SHOW STATUS WHERE Variable_name IN ('wsrep_cluster_status','wsrep_cluster_size','wsrep_ready')\""
   # Oczekiwane: wsrep_cluster_status=Primary, wsrep_cluster_size=1, wsrep_ready=ON
   ```

5. **Dołącz pozostałe węzły po naprawie i zachowaj topologię:**
   - Po usunięciu awarii pozostałych węzłów przywróć je do klastra:
     ```bash
     make cluster-join CLUSTER=<name>
     make cluster-proxysql CLUSTER=<name>
     make cluster-monitoring CLUSTER=<name>
     make lab-galera-verify CLUSTER=<name>
     ```
   - **Ograniczenie topologii:** Stan 1- lub 2-węzłowy jest STANEM AWARII PRZEJŚCIOWEJ. Klaster 2-węzłowy
     bez arbitra (`garbd`) nie zapewnia HA. Sam ADR nie legalizuje pracy w zredukowanej topologii bez arbitra
     — repozytorium wymaga 3 węzłów (`galera.nodes_expected: 3`), a bramy zdrowia będą słusznie alertować
     brak węzła do momentu pełnego dołączenia lub wymiany zgodnie z `docs/runbooks/node-replacement.md`.

## Scenariusz B: Wszystkie węzły down

**Zweryfikowane w praktyce na Rocky 10 (2026-07-28)** po restarcie całego labu.
Objaw: `mariadb.service` jest `enabled`, próbuje wstać i pada po ~35 s;
`grastate.dat` na KAŻDYM węźle ma `seqno: -1` i `safe_to_bootstrap: 0`.
To poprawne zachowanie — Galera odmawia zgadywania, który węzeł ma najświeższy stan.

**Ścieżka zautomatyzowana:** `make cluster-recover CLUSTER=<name> CONFIRM=yes`
(odmawia przy żywym Primary, zatrzymuje klaster serialnie, wybiera węzeł bootstrap
z `grastate.dat`, reuse'uje kanoniczny `playbooks/bootstrap.yml`, potem join).
Automat STAJE PRZED wyborem węzła, gdy jakikolwiek `grastate.dat` ma `seqno: -1`
lub wartość uszkodzoną: `-1` to pozycja nieznana, nieporównywalna — automat
nie porównuje jej i nie wybiera na jej podstawie. Operator uruchamia wtedy
`mariadbd --wsrep-recover` na KAŻDYM węźle (krok 1 poniżej), porównuje
odzyskane pozycje i wybiera węzeł z NAJWYŻSZĄ, po czym powtarza
`make cluster-recover CLUSTER=<name> CONFIRM=yes BOOTSTRAP_NODE=<węzeł>`
(jawne wskazanie węzła z `-1` jest dopuszczalne właśnie i tylko po tym
odzyskaniu). Także przy remisie znanych seqno/safe_to_bootstrap automat staje
i wymaga `BOOTSTRAP_NODE=<węzeł>` — kroki poniżej to procedura ręczna
dla przypadków, których `grastate.dat` nie rozstrzyga (np. pozycje
odzyskane z journala po nieczystym zamknięciu).

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
     -e @clusters/<name>/cluster.yml -e bootstrap_node=<node> -e confirm=yes \
     -e bootstrap_confirm_all_down=true
   # (Flaga bootstrap_confirm_all_down=true jest wymagana przez Audit#2, gdy pozostałe węzły
   # są nieosiągalne po SSH; automat make cluster-recover przekazuje ją zawsze).
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
- Audit#2: Nieosiągalne węzły (`galera_state_unreachable`) wymagają jawnego `-e bootstrap_confirm_all_down=true` po uprzednim fencingu out-of-band.
- Fail-closed: Węzły w stanie nieznanym (`galera_state_unknown`) bezwzględnie blokują bootstrap i cold recovery — żadna flaga nie znosi tej blokady.
- Brak redukcji topologii bez arbitra: Sam ADR nie pozwala na stałą pracę w topologii 2-węzłowej bez `garbd`; wymagane jest odtworzenie 3 węzłów.

## Weryfikacja po recovery

```bash
# Sprawdź cluster size, UUID, synced
make lab-galera-verify CLUSTER=<name>
```

## Wymagany dostęp

- SSH do wszystkich węzłów Galera
- `grastate.dat` odczyt
- `mariadb --wsrep-recover` na każdym węźle
