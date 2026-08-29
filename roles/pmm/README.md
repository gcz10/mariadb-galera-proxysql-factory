# pmm

**ROLA** (ma `tasks/main.yml`) — węzły monitoringu: instalacja/rejestracja
pmm-client, tryb agentless dla hostów bez agenta, konto monitorujące ProxySQL.

## Zawartość

- `tasks/main.yml` + `tasks/{agent_install,agent_register,agentless_preflight,agentless_legacy,agentless_register,monitor_account}.yml`.
- `defaults/main.yml` — niskopriorytetowe domyślne.
- `meta/main.yml` — zależności roli.

## Konsumenci

- Playbooki `f11_pmm_agent.yml`, `f11_pmm_client.yml`, `f11_proxysql_metrics.yml`
  (grupy `galera`/`proxysql`/`infra`).

## Kontrakty

- `metrics_mode: push` jest WYMUSZONY (auto wybrałoby pull → porty 42000-51999
  blokowane firewalldem; patrz lockfile `pmm_client`).
- Serwer PMM ≥ klient (lockfile `pmm` vs `pmm_client`).
- Weryfikacja: `make lab-monitoring-verify`, sonda PMM-native w post-build gate.
