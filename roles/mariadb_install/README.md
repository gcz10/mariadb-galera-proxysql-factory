# mariadb_install

**ASSETY, NIE ROLA** (brak `tasks/main.yml` — nigdy nie wywoływać nazwą roli;
patrz `tests/validation/probe-role-contract.py`).

## Zawartość

- `templates/server.cnf.j2` — generowany `server.cnf`: Galera (wsrep),
  tuning z `mariadb_tuning`, TLS (opcjonalny `tls.mode=full`),
  `wsrep_provider_options` z `gcache.size` + `socket.ssl_*`.

## Konsumenci (asset WSPÓLNY — dwaj konsumenti)

- `playbooks/site.yml:98` — converge klastra (register `config_result`
  steruje restartem i `FLUSH SSL`).
- `playbooks/f5_join.yml:60` — join nowego węzła (ten sam template).

## Kontrakty

- Zero wersji/platformy w treści — wszystko z lockfile per klaster
  (`versions.lock_file`, schema tego wymaga).
- `innodb_flush_log_at_trx_commit` wymaga jawnego wpisu w `mariadb_tuning`
  (schemat + test kontraktu; produkcja = 1, dopuszczalny lab = 0 z decyzją).
- `gcache_size` WYMAGANY statycznie w `cluster.yml` (playbook bez fallbacku —
  ISC-68). Zmiana wartości = edycja cluster.yml + config na węzłach razem
  (pilnuje F13 drift).
- `skip_name_resolve = ON` na sztywno: konta sieciowe mają host = adres IP
  źródła (`playbooks/vars/db_account_sources.yml`, `roles/pmm`
  `pmm_monitor_sources`, filtr `playbooks/filter_plugins/mariadb_accounts.py`),
  więc konto z nazwą hosta albo wzorcem `%` nigdy nie jest poprawne
  (`probe-hardening.py` mierzy oba warunki). Zmiana wymaga restartu.
