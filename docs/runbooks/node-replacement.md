# Runbook: Node Replacement

**Status:** STUB — do uzupełnienia w F5/F13
**Powiązane ISC:** ISC-29, ISC-15

## Przeznaczenie

Wymiana uszkodzonego węzła Galera lub ProxySQL na nowy host.

## Procedura — Galera node

```bash
# 1. Usuń węzeł z klastra (jeśli jeszcze działa)
make cluster-remove-node CLUSTER=<name> NODE=<old_node>

# 2. Przygotuj nowy host (Rocky 9, F2 preflight)

# 3. Dodaj nowy węzeł do inventory.yml

# 4. Dołącz nowy węzeł (SST lub IST w zależności od okna gcache)
make cluster-add-node CLUSTER=<name>
```

## Weryfikacja

```bash
# Nowy węzeł osiąga Synced bez ręcznych kroków (ISC-29)
tests/validation/probe-galera-status.sh /var/lib/mysql/mysql.sock 3

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
