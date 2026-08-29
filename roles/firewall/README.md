# firewall

**ASSETY, NIE ROLA** (brak `tasks/main.yml` — nigdy nie wywoływać nazwą roli;
patrz `tests/validation/probe-role-contract.py`).

## Zawartość

- `templates/public.xml.j2` — strefa `public` firewalld z DOKŁADNEJ allowlisty
  portów per grupa hostów, porty z `playbooks/vars/infra_ingress.yml` +
  `network.*` z `cluster.yml`/`platform.yml`.

## Konsumenci

- `playbooks/firewall.yml:168` — render `/etc/firewalld/zones/public.xml`
  (mode 0640) + `firewall-cmd --reload` (usuwa też dryf runtime).
- Importowany przez `infra_services.yml` i cele `*-firewall`.

## Kontrakty

Kompletna polityka: strefa jest nadpisywana, nie dopisywana (drift runtime
usuwalny reloadem). Zachowanie na `fcp1/fcp2` pilnowane decyzją ownership
(plan `ownership-and-safety-hardening.md` — firewall_target_hosts).
