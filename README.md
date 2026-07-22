# Galera + ProxySQL Cluster Factory

Powtarzalna, idempotentna fabryka produkcyjnych klastrów MariaDB Galera z ProxySQL na istniejących hostach Rocky Linux 9.

**Status: Faza OBSERVE — F0 Discovery przygotowany, nie uruchomiony (brak testowych hostów).**

Zobacz `ISA.md` — jedyne źródło prawdy dla idealnego stanu, kryteriów, mapy testów i postępu.

## Szybki start (gdy dostępne będą hosty)

```bash
# F0 discovery (read-only)
make cluster-discover CLUSTER=example-cluster

# Walidacja konfiguracji klastra
make cluster-validate CLUSTER=example-cluster
```

## Struktura

```
clusters/<name>/     — inventory.yml + cluster.yml + secrets per klaster
versions/            — lockfile, discovered-versions, compatibility-report
profiles/            — production/staging/laboratory
playbooks/           — feature po feature
roles/               — standardowe katalogi, gdy potrzebne
tests/               — integration/idempotence/failure/recovery/upgrade/validation
docs/                — architecture, adr, runbooks
```

Nowy klaster = nowy katalog `clusters/<name>/`. Kod ról nie zawiera danych klastra.

## Kontrakt

Pełny kontrakt pracy, format ISA i wymagania w `MASTER_PROMPT.md`.
Bieżący stan projektu w `ISA.md`.
