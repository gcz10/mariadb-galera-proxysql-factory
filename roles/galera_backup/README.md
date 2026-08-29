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
