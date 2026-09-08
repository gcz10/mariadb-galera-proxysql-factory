# Runbook: Node Replacement

**Status:** Aktualny (F5/F13 complete)
**Powiązane ISC:** ISC-29, ISC-15

## Przeznaczenie

Wymiana uszkodzonego węzła Galera lub ProxySQL na nowy host.

## Procedura — Galera node

```bash
# 1. Plan usunięcia starego węzła (read-only: quorum guard, writer-detection)
make cluster-remove-node-plan CLUSTER=<name> NODE=<old_node>

# 2. Usuń stary węzeł (confirm-gated, quorum-guarded).
#    Usuniecie z klastra 3-wezlowego zmienia topologie na 2 wezly, wiec
#    playbook wymaga jawnej zgody (audit#18). Przy wymianie wezla stan
#    2-wezlowy jest przejsciowy — krok 4 przywraca 3.
make cluster-remove-node CLUSTER=<name> NODE=<old_node> CONFIRM=yes \
  ANSIBLE_OPTS="-e accept_topology_change=yes"

# 3. Przygotuj nowy host (Rocky 9, F2 preflight), dodaj do inventory.yml
#    Wymiana jest przypadkiem, w ktorym galera.nodes_expected ZOSTAJE na 3:
#    stan 2-wezlowy jest przejsciowy i konczy sie w kroku 4. Do tego czasu
#    bramy zdrowia i alert `node-loss` slusznie raportuja brak wezla.
#    Gdyby dolaczenie wezla zastepczego mialo NIE nastapic, zejscie do 2
#    wezlow nie jest zwykla edycja pliku: validate-cluster-schema.py odrzuca
#    `nodes_expected: 2` ("v1 scope requires 3 — 2+garbd/5/multi-DC needs
#    ADR"), wiec wymaga ADR i arbitra garbd (docs/runbooks/decommission.md,
#    krok 4).

# 4. Dołącz nowy węzeł (SST mariabackup lub IST w zależności od okna gcache)
make cluster-join CLUSTER=<name>

# 5. Skonfiguruj ProxySQL hostgroups + monitoring dla nowego węzła
make cluster-proxysql CLUSTER=<name>
make cluster-monitoring CLUSTER=<name>
```

## Weryfikacja

```bash
# Nowy węzeł osiąga Synced bez ręcznych kroków (ISC-29)
make lab-galera-verify CLUSTER=<name>

# Jeśli węzeł powraca w oknie gcache → IST (ISC-15)
# Jeśli poza oknem → SST przez mariadb-backup (ISC-14)
```

## Procedura — węzeł Galera po nieudanym buildzie (bez kasowania maszyny)

Nieudany `cluster-build` (np. przerwany SST) zostawia host z pakietami
i `datadir`, ale bez tożsamości w `server.cnf`. Preflight słusznie odmawia —
nie zgaduje, czyj jest `datadir` — więc kanoniczna ścieżka nie wstaje:

```
Datadir istnieje, ale /etc/my.cnf.d/server.cnf nie istnieje albo nie jest
plikiem. Odmowa: nie da sie dowiesc tozsamosci bez ryzyka wipe.
```

Do 2026-09-08 jedynym wyjściem był `infra-teardown` + `infra-provision`, czyli
skasowanie MASZYNY. Teraz kasuje się wyłącznie DANE jednego węzła:

```bash
# 1. Reset: kasuje datadir i tożsamość TEGO węzła.
#    Odmawia, gdy: brak innego węzła w grupie (nie ma skąd odtworzyć danych),
#    dawca nie jest zsynchronizowanym Primary, datadir deklaruje INNY klaster,
#    albo na hoście nadal działa mariadbd.
make cluster-node-reset CLUSTER=<name> NODE=<node> CONFIRM=yes

# 2. Powrót: konfiguruje TYLKO ten węzeł i dołącza go przez SST od żywego dawcy.
#    Pełny `cluster-deploy` tu nie przejdzie — brama zdrowia na sąsiadach żąda
#    pełnego rozmiaru klastra, którego brakuje właśnie z powodu tego węzła.
make cluster-node-rejoin CLUSTER=<name> NODE=<node>
```

Czego ta ścieżka NIE robi: nie kasuje `datadir` należącego do innego klastra.
Ten przypadek rozstrzyga człowiek, po sprawdzeniu, czyje są dane.


## Procedura — ProxySQL node

```bash
# 1. Zatrzymaj stary ProxySQL
# 2. Keepalived VIP failover na drugi ProxySQL (ISC-25)
# 3. Przygotuj nowy host, dołącz do inventory
# 4. Deploy ProxySQL na nowym hoście
# 5. Keepalived wraca do normalnego stanu
```
