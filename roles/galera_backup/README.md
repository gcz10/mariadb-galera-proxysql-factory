# galera_backup

**ROLA** (ma `tasks/main.yml`) — backup i restore Galery: pakiet Pythona wdrażany
na `/opt/galera-backup/`, konfiguracja per klaster, cron, konta MinIO.

## Zawartość

- `files/galera-backup` — cienki wrapper (wejście cron/systemd).
- `files/galera_backup/` — pakiet: pipeline, storage/{s3,filesystem}, locking,
  secrets, state (dekompozycja: `docs/superpowers/plans/galera-backup-decomposition.md`).
- `templates/` — `config.json.j2`, `cron.j2`, `restore-cron.j2`,
  `minio-policy{,-prune}.json.j2`, `secrets.env.j2`.
- `filter_plugins/minio_access_keys.py` — wydobywanie kluczy z configu.
- `tasks/main.yml` + `tasks/{provision_minio,reconcile_minio_account,minio_owned_keys,minio_root_env,deregister_minio}.yml`.

## Konsumenci

- `playbooks/f10_backup.yml` (sekcja `roles:` ×3 — backup, restore, scheduler).
- `playbooks/cluster_deregister.yml` (`include_role`, tasks `deregister_minio`).
- Cron na węźle: `/etc/cron.d/galera-backup-<klaster>` (scheduler `20 3 * * *`).

## Kontrakty

ISC-32..39 (backup szyfrowany off-cluster, checksum, restore drill). Weryfikacja:
`make lab-backup-verify`, `make lab-restore-verify`. Sdk `minio.sdk_version`
z lockfile — przypięty, pływające wersje zabronione.

Metryki ostatniego udanego restore:
- `galera_restore_last_duration_seconds`: czas monotoniczny od rozpoczęcia
  pracy po uzyskaniu blokady do sprawdzenia danych i zatrzymania serwera
  weryfikacyjnego; nie czas całego playbooka ani RTO aplikacji.
- `galera_restore_last_size_bytes`: rozmiar pobranego szyfrowanego artefaktu
  w bajtach, nie rozmiar odtworzonego datadir.

Jeden pomiar trafia do `state.json`, znacznika drillu w backendzie i lokalnego
pliku metryk. Ręczny `cluster-restore-drill` publikuje go przez F11 na
`galera[0]`; drill cronowy dociera do monitoringu przez mostek przy następnym
udanym backupie. Hosty restore nie potrzebują własnego eksportera.
Obie ścieżki zapisują `galera_restore_drill-<cluster>.prom`.

Znaczniki formatu 2 zawierają czas i rozmiar. Stare znaczniki formatu 1 oraz
stany bez tych pól nadal potwierdzają świeżość, ale nie publikują brakujących
pomiarów jako zer. Niepoprawne pomiary są odrzucane przed publikacją.
Brak potwierdzonego znacznika nie nadpisuje istniejącego pliku mostka.

Po zmianie donora niewybrany kandydat usuwa lokalny plik mostka. Pomiędzy
publikacją F11 a następnym backupem serie mogą istnieć na różnych węzłach:
należy wybierać pomiary z serii mającej najnowszy
`galera_restore_last_success_unixtime` dla danego `logical_cluster`, nie
sumować rozmiarów ani zakładać stałego hosta producenta. Jeśli zapis znacznika
do backendu zawiedzie po udanym drillu, mostek pozostaje zależny od starszego
znacznika; jego czas sukcesu pozwala rozpoznać stary pomiar.

Lokalny plik `galera_restore-<cluster>.prom` opisuje także nieudane próby
(czas próby, rozmiar zero); nie jest źródłem zbieranym z hostów restore do PMM.
