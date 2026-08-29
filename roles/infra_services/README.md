# infra_services

**ASSETY, NIE ROLA** (brak `tasks/main.yml` — nigdy nie wywoływać nazwą roli;
patrz `tests/validation/probe-role-contract.py`).

## Zawartość

- `templates/docker-user-firewall.sh.j2` — filtr `DOCKER-USER` (xt_conntrack)
  dla opublikowanych portów.
- `templates/isa-docker-firewall.service.j2` — unit trwałości filtra.
- `templates/compose.yml.j2` — PMM Server + MinIO + Maildev (bez sekretów
  w argv; konfiguracja `0600`).

## Konsumenci

- `playbooks/infra_services.yml:166,175,254` — host infra warstwy wspólnej
  (`x10mon`), wyłącznie z definicji `platform.yml` (odrzucza konfigurację
  najemcy — `platform.name is defined` + `galera is not defined`).

## Kontrakty

Kolejność krytyczna: filtr DOCKER-USER przed startem Dockera (unit
`isa-docker-firewall` jest Requires dockera); kernel zainstalowany-a-niezaładowany
blokuje iptables — playbook sprawdza i wymaga rebootu (`allow_kernel_reboot`).
Sekrety usług wyłącznie z env (`PMM_ADMIN_PASSWORD`, `MINIO_ROOT_*`).
