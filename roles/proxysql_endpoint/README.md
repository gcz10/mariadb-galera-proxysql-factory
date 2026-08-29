# proxysql_endpoint

**ASSETY, NIE ROLA** (brak `tasks/main.yml` — nigdy nie wywoływać nazwą roli;
patrz `tests/validation/probe-role-contract.py`).

## Zawartość

- `templates/keepalived.conf.j2` — para VRRP dla VIP endpointu (priorytet,
  `keepalived_connect_any` przy SELinux, auth ≤8 znaków — polityka
  `KEEPALIVED_AUTH_PASS`).
- `files/check_proxysql.sh` — backend-aware health-check: przez
  `/etc/proxysql/admin-check.cnf` (0600, admin bez argv); bez pliku degraduje
  do sondy TCP-open.

## Konsumenci

- `playbooks/f8_keepalived.yml:113,122` — `/usr/local/bin/check_proxysql.sh`
  (0755) + `/etc/keepalived/keepalived.conf` (0600); systemd restart gdy
  `kl_config` lub `kl_script` zmienione.

## Kontrakty

Health-check decyduje o utrzymaniu VIP-a (węzeł odpowiadający TCP, ale
zwracający błędy backendom, traci VIP — zapobiega „czarnej dziurze" endpointu).
Zmiana ścieżki/tożsamości admin-check.cnf = zmiana kontraktu z warstwą
wspólną (`make platform-proxysql`).
