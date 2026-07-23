# Runbook: Node Replacement

**Status:** Aktualny (F5/F13 complete)
**Powiązane ISC:** ISC-29, ISC-15

## Przeznaczenie

Wymiana uszkodzonego węzła Galera lub ProxySQL na nowy host.

## Procedura — Galera node

```bash
# 1. Plan usunięcia starego węzła (read-only: quorum guard, writer-detection)
make cluster-remove-node-plan CLUSTER=<name> NODE=<old_node>

# 2. Usuń stary węzeł (confirm-gated, quorum-guarded)
make cluster-remove-node CLUSTER=<name> NODE=<old_node> CONFIRM=yes

# 3. Przygotuj nowy host (Rocky 9, F2 preflight), dodaj do inventory.yml

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

## Procedura — ProxySQL node

```bash
# 1. Zatrzymaj stary ProxySQL
# 2. Keepalived VIP failover na drugi ProxySQL (ISC-25)
# 3. Przygotuj nowy host, dołącz do inventory
# 4. Deploy ProxySQL na nowym hoście
# 5. Keepalived wraca do normalnego stanu
```
