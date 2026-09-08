# Runbook: Node Decommission

**Status:** Aktualny (F13 complete)
**Powiązane ISC:** ISC-29, drift detection

## Przeznaczenie

Bezpieczne usunięcie węzła z klastra Galera lub ProxySQL.

## Procedura — Galera node

```bash
# 1. Plan usunięcia (read-only: quorum guard 3→2, writer-detection)
make cluster-remove-node-plan CLUSTER=<name> NODE=<node>

# 2. Usuń węzeł (confirm-gated; wymaga zdrowego klastra, drain na obu ProxySQL,
#    usuwa wszystkie typy usług z PMM, zatrzymuje i wyłącza mariadbd).
#    Gdy operacja zmienia liczebnosc klastra (np. 3 -> 2), playbook wymaga
#    dodatkowo jawnej zgody na zmiane topologii (audit#18) — klaster
#    2-wezlowy bez arbitra nie przetrwa awarii jednego wezla.
make cluster-remove-node CLUSTER=<name> NODE=<node> CONFIRM=yes \
  ANSIBLE_OPTS="-e accept_topology_change=yes"

# 3. Natychmiast usuń <node> z clusters/<name>/inventory.yml.
#    Inaczej kolejny converge odtworzy konfigurację usuniętego węzła.

# 4. Doprowadz deklaracje i stan trwaly do zgodnosci. Dopoki tego nie zrobisz,
#    validate-inventory zglasza "group 'galera' ma 2 hostów, a
#    galera.nodes_expected=3", bramy zdrowia (tasks/assert_galera_healthy.yml)
#    czekaja na rozmiar 3 az do timeoutu, alert `node-loss` pali sie bez konca,
#    a backup odmawia startu (backup.py: E_GALERA size=2/3). Tego samego wymaga
#    komunikat bramki audit#18 w samym playbooku usuniecia.
#
#    UWAGA — samo wpisanie `galera.nodes_expected: 2` NIE przechodzi bramki:
#    validate-cluster-schema.py odrzuca to komunikatem "v1 scope requires 3
#    (2+garbd/5/multi-DC needs ADR)". Sa wiec dokladnie dwie legalne drogi:
#      a) topologia 2-wezlowa jest DOCELOWA -> wymaga ADR (docs/adr/) i arbitra
#         garbd; bez decyzji architektonicznej repo nie przyjmie tej wartosci,
#         bo klaster 2-wezlowy bez arbitra nie przetrwa awarii jednego wezla;
#      b) topologia 3-wezlowa ma wrocic -> zostaw nodes_expected=3, traktuj stan
#         jako przejsciowy i dolacz wezel zastepczy (docs/runbooks/node-replacement.md).
#         Do tego czasu alerty i bramy zdrowia SLUSZNIE raportuja brak wezla.

# 5. Po usunięciu sprawdź dryf konfiguracji ProxySQL/Galera
make cluster-drift CLUSTER=<name>
# cluster-validate przechodzi dopiero, gdy deklaracja i inwentarz sa zgodne
# (droga a po ADR, albo droga b po dolaczeniu wezla zastepczego).
make cluster-validate CLUSTER=<name>
```

## Procedura — ProxySQL node

```bash
# 1. Keepalived VIP failover na drugi ProxySQL
# 2. Usuń z ProxySQL config
# 3. Zatrzymaj usługę
# 4. Usuń z inventory
```

## Weryfikacja

```bash
# Cluster size po decommission + zdrowie pozostałych węzłów
make lab-galera-verify CLUSTER=<name>
make cluster-drift CLUSTER=<name>
```

## Anti

- Nie usuwaj ostatniego węzła bez pełnego backupu i restore testu
- Nie usuwaj węzła jeśli cluster spadnie poniżej quorum (2 z 3 = OK, 1 z 3 = blocker)
