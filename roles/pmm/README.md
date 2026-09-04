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

- Tryb metryk push jest WYMUSZONY wykonawczo przez `push_metrics: true` w
  `tasks/agent_register.yml` (`auto` wymagaloby portow 42000-51999 blokowanych firewalldem).
- Serwer PMM ≥ klient (lockfile `pmm` vs `pmm_client`).
- `percona-release` instalowany wyłącznie z przypiętego URL: sha256 RPM i klucza
  GPG oraz odcisk palca klucza żyją w lockfile (`pmm_client.*`), bez
  `disable_gpg_check`; pilnuje sonda `verify-no-state-latest` (P1-A).
- Weryfikacja: `make lab-monitoring-verify`, sonda PMM-native w post-build gate.
