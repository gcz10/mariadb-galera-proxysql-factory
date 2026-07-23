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
# 1. Wybierz JEDEN węzeł do bootstrap (zwykle gnode1)
# 2. Uruchom bootstrap playbook z jawnym potwierdzeniem
make cluster-bootstrap CLUSTER=<name> BOOTSTRAP_NODE=gnode1
# ansible-playbook playbooks/bootstrap.yml -l gnode1 -e confirm=yes

# 3. Po sukcesie bootstrap, dołącz pozostałe węzły
make cluster-deploy CLUSTER=<name>
# ansible-playbook playbooks/site.yml --skip-tags bootstrap
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
