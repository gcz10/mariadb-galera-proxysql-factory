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
- Transport PMM→MariaDB idzie za deklaracją najemcy: przy `tls.mode=full`
  `tasks/agentless_register.yml` ustawia `mysqld_exporter` i agentowi QAN
  `tls=true`, `tls_skip_verify=false` i **zawartość** `ca_reference` w `tls_ca`
  (API przyjmuje PEM, nie ścieżkę na hoście PMM). Przy `disabled` zostaje
  `tls=false` bez wczytywania CA.
- PMM (3.9.1) **nie odsyła `tls_ca`** w `GET /v1/inventory/agents` — pole
  przychodzi puste, choć w bazie `pmm-managed` leży pełny łańcuch. Zbieżność
  liczymy więc po etykiecie `tls_ca_sha256` (odcisk CA), bo porównanie
  z odczytem `tls_ca` przepisywałoby agentów w każdym przebiegu.
