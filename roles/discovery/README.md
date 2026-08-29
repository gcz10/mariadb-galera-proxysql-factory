# discovery

**ASSETY, NIE ROLA** (brak `tasks/main.yml` — nigdy nie wywoływać nazwą roli;
patrz `tests/validation/probe-role-contract.py`).

## Zawartość

- `templates/discovery-report.json.j2` — raport JSON z faktów hosta i stanu
  Galery (wsrep, brakujące PK, NUMA, wersja MariaDB).

## Konsumenci

- `playbooks/f0_discovery.yml:143` — `lookup('template', ...)` do
  `ansible.builtin.copy` (raport per węzeł).

## Kontrakty

Read-only (ISC-53 analogia: zapis raportu to jedyna zmiana). Raporty lądują
w raporcie klastra (`cluster-discover`).
