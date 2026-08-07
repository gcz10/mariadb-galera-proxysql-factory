# Runbook: Initial Bootstrap

**Status:** Aktualny (F4/F13 complete)
**Powiązane ISC:** ISC-12, ISC-13, ISC-65

## Przeznaczenie

Uruchomienie nowego klastra Galera od zera — pierwszy Primary Component.

## Warunki wstępne

- [ ] F0 discovery wykonany
- [ ] F2 preflight PASS (SELinux, firewalld, pakiety, time sync)
- [ ] `versions.lock.yml` potwierdzony na hostach
- [ ] Inventory i cluster.yml zwalidowane (`make cluster-validate`)
- [ ] Wszystkie 3 węzły Galera osiągalne
- [ ] Sekrety w Ansible Vault (`clusters/<name>/secrets.yml`)

## Procedura

```bash
# 1. Instalacja pakietow i konfiguracja (idempotentny converge; NIE bootstrapuje)
make cluster-deploy CLUSTER=<name>

# 2. Bootstrap JEDNEGO wezla. Domyslnie galera[0]; inny wezel wskazuje sie
#    przez ANSIBLE_OPTS, bo Makefile nie zna zmiennej BOOTSTRAP_NODE.
make cluster-bootstrap CLUSTER=<name> CONFIRM=yes
#   inny wezel niz galera[0]:
#   make cluster-bootstrap CLUSTER=<name> CONFIRM=yes ANSIBLE_OPTS="-e bootstrap_node=gnode2"

# 3. Dolacz pozostale wezly (SST mariabackup, serial:1).
#    site.yml tego NIE robi — dolaczanie ma wlasny playbook f5_join.yml.
make cluster-join CLUSTER=<name>
```

## Anti-criteria (ISC-13, ISC-65)

- `site.yml` NIGDY nie bootstrapuje — tylko `bootstrap.yml` z `confirm=yes`
- Drugi bootstrap przy istniejącym Primary jest BLOKOWANY
- Dwa węzły nigdy nie są bootstrapowane jako niezależne Primary Components

## Weryfikacja

```bash
# Sprawdź Primary Component na każdym węźle
for node in gnode1 gnode2 gnode3; do
  ssh $node "mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e \"SHOW STATUS LIKE 'wsrep_cluster_status'\""
done
# Oczekiwane: Primary na wszystkich
```

## Powrót z błędu

- Jeżeli bootstrap nie powiódł się: sprawdź `grastate.dat`, `safe_to_bootstrap`, `--wsrep-recover`
- Recovery runbook: `docs/runbooks/total-outage.md`
