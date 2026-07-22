# Runbook: Node Decommission

**Status:** STUB — do uzupełnienia w F13
**Powiązane ISC:** ISC-29, drift detection

## Przeznaczenie

Bezpieczne usunięcie węzła z klastra Galera lub ProxySQL.

## Procedura — Galera node

```bash
# 1. Plan (read-only)
make cluster-decommission-plan CLUSTER=<name> NODE=<node>

# 2. Sprawdź quorum po usunięciu (3→2 wymaga garbd lub zostaw 3)
# 3. Drain węzła (wsrep_desync + wait for Synced=false)
# 4. Usuń z ProxySQL hostgroups
# 5. Zatrzymaj MariaDB
# 6. Usuń z inventory
make cluster-decommission CLUSTER=<name> NODE=<node>
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
# Cluster size po decommission
mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "SHOW STATUS LIKE 'wsrep_cluster_size'"
# Sprawdź że pozostałe węzły są nadal Primary + Synced
tests/validation/probe-galera-status.sh /var/lib/mysql/mysql.sock <new_expected_size>
```

## Anti

- Nie usuwaj ostatniego węzła bez pełnego backupu i restore testu
- Nie usuwaj węzła jeśli cluster spadnie poniżej quorum (2 z 3 = OK, 1 z 3 = blocker)
