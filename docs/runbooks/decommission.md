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
#    usuwa wszystkie typy usług z PMM, zatrzymuje i wyłącza mariadbd)
make cluster-remove-node CLUSTER=<name> NODE=<node> CONFIRM=yes

# 3. Natychmiast usuń <node> z clusters/<name>/inventory.yml.
#    Inaczej kolejny converge odtworzy konfigurację usuniętego węzła.

# 4. Po usunięciu sprawdź dryf konfiguracji ProxySQL/Galera
make cluster-drift CLUSTER=<name>
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
