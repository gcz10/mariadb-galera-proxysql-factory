# Galera Backup Design

**Date:** 2026-07-29
**Status:** Approved in conversation; awaiting review of this written specification

## Goal

Provide one cluster-aware `galera-backup` program that creates a full physical backup of every database in a Galera cluster from one explicitly selected Galera node. The same program is invoked manually through `make` and automatically through a per-cluster cron entry. It supports three storage modes: a dedicated S3 bucket, a directly mounted SMB share, and an already-mounted filesystem such as NFS or CIFS.

A Galera cluster contains the same replicated dataset on every Synced member. The system therefore creates one backup from one healthy member, not one duplicate backup per node.

## Scope

This change includes:

- one local backup/restore program named `galera-backup`;
- one explicitly configured backup host from the cluster's `galera` inventory group;
- optional nightly cron installation on that host;
- manual and cron execution through the same code path;
- full physical backups using `mariadb-backup`;
- S3, managed SMB, and pre-mounted filesystem storage;
- a unique S3 bucket per cluster;
- a separate directory per cluster on shared SMB/NFS storage;
- encryption, checksums, metadata, retention, cleanup, persistent status, PMM metrics, and restore support for every backend;
- CI and runtime guards against cross-cluster storage reuse;
- live S3 and restore verification on `claude-r10b` plus isolated SMB verification on `rnode1`.

This change does not include:

- incremental backups;
- automatic restore drills from cron;
- scheduler failover to another Galera node;
- copying the repository, Ansible, or an SSH private key onto a Galera node;
- automatic reboot when the running kernel lacks the CIFS module;
- one ProxySQL pair routing multiple unrelated Galera clusters.

## User Decisions

The accepted decisions are:

1. Cron runs on one explicitly selected Galera node per cluster.
2. There is no automatic scheduler failover. PMM freshness alerts detect a stopped scheduler or failed host.
3. The selected host may perform a backup even when ProxySQL currently treats it as the writer.
4. Only backup is scheduled. Restore drill remains an explicit, confirmation-gated operation on the isolated restore host.
5. Manual and scheduled backup use one local program, not two implementations.
6. The program and its cluster files are grouped under `/opt/galera-backup`.
7. Product source is grouped under `roles/galera_backup`; test code remains under `tests`.
8. Every S3 cluster has its own bucket and bucket-scoped credentials.
9. A shared SMB/NFS filesystem may hold multiple clusters only in separate owned directories.

## Research Findings

### Existing repository

The current backup flow is controller-driven:

- `make cluster-backup` invokes `tests/lab/backup-run.sh`;
- the wrapper invokes `playbooks/f10_backup.yml`;
- the playbook queries ProxySQL, selects a non-writer, runs `mariadb-backup`, encrypts the artifact, and uploads it with `roles/backup/files/s3_object.py`;
- `roles/backup/templates/mariadb-backup.cron.j2` exists, but no role or playbook installs it;
- `full_backup_schedule` and `restore_test_schedule` are therefore configuration values, not active schedules;
- `f10_backup.yml` currently rejects every destination except S3;
- restore and the S3 probe list the newest metadata object from the entire bucket without first restricting the list to the current cluster prefix.

The last point means that an accidentally shared bucket can restore another cluster's backup. Dedicated buckets plus runtime ownership and metadata checks are required.

### Ansible cron

`ansible.builtin.cron` can manage a named file in `/etc/cron.d`. The job name must remain unique and a `user` is required for a `cron_file` entry. A separate cron file per cluster makes convergence and removal deterministic.

Source: [Ansible cron module](https://docs.ansible.com/ansible/latest/collections/ansible/builtin/cron_module.html)

### SMB on Rocky/RHEL

RHEL documents SMB mounts through `cifs-utils`, recommends a protected credentials file instead of credentials in command arguments or `/etc/fstab`, and supports SMB 3.x. The `seal` mount option requires SMB 3.0 or later and provides SMB transport encryption.

Sources:

- [RHEL 9 Managing file systems, Chapter 5: Mounting an SMB share](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/managing_file_systems/index)
- [Ansible POSIX mount collection](https://docs.ansible.com/ansible/latest/collections/ansible/posix/mount_module.html)

The current `claude-r10b` nodes run kernel `6.12.0-211.16.1.el10_2.0.1`, which lacks a matching `cifs` module. The already-installed kernel `6.12.0-211.39.1.el10_2` contains `/kernel/fs/smb/client/cifs.ko.xz`. Managed SMB must therefore fail with a clear kernel/module diagnostic until a node boots the matching kernel. Backup code must never reboot a database node automatically.

## Architecture

### Runtime layout

The main runtime files are grouped by purpose and cluster:

```text
/opt/galera-backup/
├── galera-backup
└── clusters/
    └── <cluster-name>/
        ├── config.json
        ├── secrets.env
        ├── state.json
        └── events.jsonl
```

Only operating-system integration files live elsewhere:

```text
/etc/cron.d/galera-backup-<cluster-name>
/run/lock/galera-backup-<cluster-name>.lock
/var/lib/node_exporter/textfile_collector/galera_backup-<cluster-name>.prom
/var/tmp/galera-backup/<cluster-name>/
```

`secrets.env`, the cluster directory, and runtime credential files are root-owned. `secrets.env` is mode `0600`. The executable and non-secret configuration are readable but not writable by non-root users.

### Repository layout

All production backup code is consolidated under one role:

```text
roles/galera_backup/
├── tasks/main.yml
├── files/galera-backup
└── templates/
    ├── config.json.j2
    └── cron.j2
```

`playbooks/f10_backup.yml` becomes a thin dispatcher to the configured Galera host. `playbooks/f10_restore.yml` invokes the same program on the isolated restore host. The current `tests/lab/backup-run.sh`, `roles/backup/files/s3_object.py`, and unfinished cron template are removed after all callers move to `galera-backup`.

### Execution paths

Manual backup:

```text
make cluster-backup CLUSTER=<name>
  -> Ansible invokes /opt/galera-backup/galera-backup backup <name>
     on backup.scheduler.host
```

Scheduled backup:

```text
/etc/cron.d/galera-backup-<name>
  -> /opt/galera-backup/galera-backup backup <name>
```

Restore drill:

```text
make cluster-restore-drill CLUSTER=<name> CONFIRM=yes
  -> Ansible invokes /opt/galera-backup/galera-backup restore <name> --confirm
     on the isolated restore host
```

Every path uses the same backend, encryption, checksum, metadata, ownership, and cleanup implementation.

## Configuration Contract

The backup schema supports exactly three destinations:

```yaml
backup:
  enabled: true
  destination: "s3"                 # s3 | smb | filesystem
  full_backup_schedule: "0 2 * * *"
  incremental_backup_schedule: "disabled"
  retention_days: 14
  encryption_enabled: true
  immutable_or_offsite_copy: true
  restore_test_schedule: "0 4 * * 0"

  scheduler:
    mode: "cron"                    # cron | manual
    host: "gnode4"                  # inventory hostname in galera
    timezone: "UTC"

  s3:
    endpoint: "192.168.1.47:9000"
    bucket: "r10b-galera-backups"
    region: "us-east-1"
    secure: false
```

Managed SMB replaces `s3` with:

```yaml
  destination: "smb"
  smb:
    source: "//nas01/backups"
    mount_point: "/mnt/galera-backup"
    options:
      - "vers=3.1.1"
      - "seal"
      - "nosuid"
      - "nodev"
      - "noexec"
```

An already-mounted filesystem replaces it with:

```yaml
  destination: "filesystem"
  filesystem:
    mount_point: "/mnt/company-backups"
    expected_fstype: "nfs4"
```

Rules:

- `scheduler.host` is required when backup is enabled and must name exactly one host in `groups['galera']`.
- `scheduler.mode=cron` installs the cron file. `mode=manual` removes it but keeps the executable and configuration for `make cluster-backup`.
- `full_backup_schedule` must be a five-field cron expression and is interpreted in `scheduler.timezone`.
- `incremental_backup_schedule` remains exactly `disabled`; incremental execution is not implemented.
- `encryption_enabled` remains `true`; disabling encryption is rejected.
- only the configuration block for the selected destination is allowed.
- all paths must be absolute and must pass the existing data-directory and staging path guards.

`claude-r10b` will use `scheduler.mode=cron`, `scheduler.host=gnode4`, and its existing `0 2 * * *` schedule. Inactive historical clusters remain `manual` after schema migration so this change cannot unexpectedly create jobs on old infrastructure.

## Backup Workflow

The `backup` command performs these steps in order:

1. Acquire `/run/lock/galera-backup-<cluster>.lock` without waiting.
2. Load and validate `config.json` and the required environment values from `secrets.env`.
3. Verify that the local hostname is the configured scheduler host.
4. Verify local Galera state: `Primary`, `Ready`, `Synced`, connected, and `wsrep_cluster_size == galera.nodes_expected`.
5. Record the starting flow-control counter and free-space values.
6. Create a root-only staging directory below the validated staging root.
7. Run `mariadb-backup --backup --galera-info` for the complete local datadir.
8. Run `mariadb-backup --prepare`.
9. Read the wsrep UUID and seqno from `mariadb_backup_galera_info`.
10. Create `backup.tar`, calculate its plaintext SHA-256, and encrypt it with AES-256-CBC, PBKDF2, and salt.
11. Calculate the encrypted artifact SHA-256 and create `backup.sha256` and `metadata.json`.
12. Check the final flow-control delta against the configured threshold. The user-approved writer policy permits execution on a writer, but an excessive delta still fails the run before publication.
13. Publish through the selected backend.
14. Verify that all three final objects are readable and match the expected size/checksum.
15. Apply retention only within the current cluster's owned storage namespace.
16. Persist success state, append a structured event, and atomically publish PMM textfile metrics.
17. Remove plaintext and staging data and release the lock.

On every failure, cleanup runs before exit. A non-zero exit is returned to both cron and `make`.

## Storage Backends

### Common artifact layout

Every complete backup contains:

```text
galera-<cluster-name>-<UTC timestamp>/
├── backup.tar.enc
├── backup.sha256
└── metadata.json
```

`metadata.json` contains at least:

- backup name and logical cluster name;
- source host;
- UTC creation time;
- MariaDB version;
- wsrep UUID and seqno;
- encrypted and plaintext SHA-256;
- encrypted size;
- encryption method;
- backend type;
- format version.

A backup is complete only when all files exist and `metadata.json` validates. Restore ignores partial data.

### S3

Each enabled S3 cluster owns one unique `(normalized endpoint, bucket)` pair.

Isolation has three layers:

1. A repository-wide validator rejects duplicate endpoint+bucket pairs across `clusters/*/cluster.yml`.
2. The bucket root contains `galera-backup-owner.json` with the exact logical cluster name and format version. A conflicting owner fails closed.
3. Backup, probe, retention, and restore all restrict operations to `galera-<cluster-name>-` and validate `metadata.cluster_name`.

For a repository-managed MinIO endpoint, the controller creates the bucket and a server-generated access key with an inline bucket-scoped policy. The secret is captured once into root-only files and never supplied in command arguments. The Galera host receives only that scoped access key and secret. MinIO root credentials remain on the controller/infrastructure path and are never written to the database node. For an external S3 service, the operator supplies equivalent bucket-scoped credentials.

For a pre-existing bucket without an owner marker, migration is fail closed: an empty bucket may be claimed; a non-empty bucket may be claimed only when every existing `metadata.json` has the current `cluster_name`; mixed, unreadable, or foreign metadata aborts migration.

Production configuration requires TLS for S3. Laboratory configuration may use an HTTP endpoint because the backup payload is encrypted before transport, but this exception does not weaken ownership or checksum checks.

S3 publication uploads data and checksum first and `metadata.json` last. A failed upload never creates a complete-backup marker.

### Managed SMB

The deployment role installs the platform-specific CIFS userspace package from the cluster lockfile. The runner verifies both the userspace helper and a CIFS module for the running kernel before touching staging.

At run time it:

1. creates a temporary root-only credentials file below `/run/galera-backup`;
2. mounts `smb.source` at `smb.mount_point` with the configured SMB 3 options;
3. verifies the actual source, fstype `cifs`, and required mount options;
4. uses `<mount_point>/<cluster-name>` as the owned storage root;
5. unmounts in unconditional cleanup;
6. removes the temporary credentials file.

The role rejects `credentials=`, `username=`, `password=`, SMB1/2 dialects, and any option that disables `seal`. It appends its own temporary credentials-file path so secrets cannot enter configuration or argv.

Credentials never appear in mount command arguments, cron, process listings, configuration JSON, or logs.

### Pre-mounted filesystem

The runner does not mount or unmount this backend. Before every write it requires:

- `filesystem.mount_point` to be an actual mount point, not a directory on `/`;
- the observed fstype to equal `expected_fstype`;
- the mount to be writable;
- the mount source and target to remain unchanged during the run;
- sufficient free space.

It writes only below `<mount_point>/<cluster-name>`. Publication occurs in `.partial-<backup-name>` and uses an atomic rename within the same mounted filesystem. A disappeared mount fails before any write, preventing an accidental backup onto the Galera node's root disk.

## Secrets

The deployed file accepts only the variables needed by the configured backend:

```text
GALERA_BACKUP_ENCRYPTION_KEY
GALERA_BACKUP_S3_ACCESS_KEY
GALERA_BACKUP_S3_SECRET_KEY
GALERA_BACKUP_SMB_USERNAME
GALERA_BACKUP_SMB_PASSWORD
GALERA_BACKUP_SMB_DOMAIN
```

S3 variables are required only for S3. SMB username/password are required only for managed SMB; domain is optional. A pre-mounted filesystem requires only the encryption key.

Ansible creates the file with `no_log: true`, root ownership, and mode `0600`. The runner rejects group/world-readable secrets. Logs include variable names that are missing, never values.

## Cron and Concurrency

A cluster in cron mode gets exactly one file:

```text
/etc/cron.d/galera-backup-<sanitized-cluster-name>
```

The job runs as root and invokes the absolute program path with the logical cluster name. It does not inline secrets. The cron name, state path, log path, metric labels, and lock path all include the sanitized cluster name.

The runner owns locking. If another manual or scheduled run holds the lock, the new run:

- performs no backup work;
- writes a `locked` event with the current holder metadata when available;
- updates failure state and the last-run metric;
- exits non-zero.

Because the user selected one designated host, there is no distributed lock and no scheduler election. Loss of that host is detected by stale-success PMM alerts.

## Restore

The same program implements `restore` on the inventory `restore` host. It remains confirmation-gated and refuses to run on a host in `galera` or `proxysql`.

For every backend it:

1. verifies storage ownership;
2. lists only complete backups with the exact current-cluster prefix;
3. validates `metadata.cluster_name` and format version;
4. downloads or copies all artifacts into isolated restore work;
5. verifies encrypted SHA-256 before decrypting;
6. verifies plaintext SHA-256 after decrypting;
7. verifies MariaDB major/minor compatibility from the platform lockfile;
8. performs `mariadb-backup --copy-back` into a validated empty datadir;
9. starts standalone MariaDB without wsrep;
10. runs `mariadb-check` for every user database and verifies non-empty restored data;
11. persists restore state;
12. stops standalone MariaDB and removes plaintext/work data unconditionally.

No restore cron is installed by this change.

## Failure State and Observability

The program appends one JSON object per decision or terminal event to:

```text
/opt/galera-backup/clusters/<cluster>/events.jsonl
```

Events include timestamp, event name, run ID, cluster, source host, backend, artifact, wsrep position, duration, size, status, and a stable error code. They never contain passwords, access keys, encryption keys, or command lines containing secrets.

`state.json` is written atomically and represents the latest run. It contains success, failure, or locked status plus the error code and diagnostic summary.

The runner writes node-exporter textfile metrics atomically, including:

- last successful backup Unix time;
- last failed run Unix time;
- last-run success boolean;
- last backup size;
- last duration;
- backend label.

The Prometheus `cluster` label is exactly `monitoring.pmm.cluster_name`, matching existing PMM queries and alerts. A separate `logical_cluster` label is exactly `cluster.name`; repository validation requires both identifiers to be unique across cluster configurations.

Alert rules cover both immediate failure and stale last success. The current `f11_freshness.yml` no longer needs Ansible to update backup success after a cron run; it must consume or coexist with the runner-owned metric without duplicate series.

## Safety Invariants

The implementation must enforce these invariants:

- one physical backup per run, from the configured Galera node;
- backup may run on the writer because the user explicitly selected that policy;
- no backup starts unless the local member reports a full healthy Primary component;
- no unencrypted backup is published;
- plaintext and SMB credentials are removed after success or failure;
- no destination operation escapes the owned cluster namespace;
- an S3 bucket cannot be claimed by two clusters;
- a filesystem owner marker cannot be overwritten by another cluster;
- a missing mount never degrades into writing to the local root filesystem;
- partial uploads/directories are never treated as restorable backups;
- cron and manual execution cannot overlap;
- restore never touches a Galera or ProxySQL host;
- credentials never appear in logs, argv, repository files, or metadata.

## Migration

All cluster configurations are migrated in one cutover:

- add `backup.scheduler`;
- add the destination-specific schema required for SMB/filesystem;
- retain existing S3 endpoint and unique bucket values;
- set active `claude-r10b` to cron on `gnode4`;
- set inactive/historical clusters to manual;
- update `example-cluster` with a complete managed-SMB example;
- add lockfile fields for platform-specific CIFS package names without hardcoded OS-major branches in playbooks;
- remove obsolete backup wrapper/helper/template files after all callers move to `galera-backup`.

No compatibility alias or deprecated old entrypoint remains.

## Verification and Acceptance

### Static and unit checks

1. Schema accepts each valid backend and rejects mixed/missing backend blocks.
2. Semantic validation requires scheduler host membership in `galera`.
3. Cron expression validation rejects malformed or unsafe values.
4. Repository validation rejects duplicate S3 endpoint+bucket pairs.
5. Path validation rejects `/`, datadir, staging/datadir overlap, and a filesystem path that is not a real mount.
6. Unit tests cover owner claim, conflicting owner, atomic filesystem publication, partial backup exclusion, retention scoping, and metadata validation.
7. Shell/subprocess tests verify that secret values do not enter argv or logs.

### Failure injection

1. Missing encryption key produces persisted failure state and non-zero exit.
2. Lock contention produces `locked`, starts no second backup, and updates metrics.
3. Wrong S3 or SMB credentials fail without publishing metadata.
4. A foreign bucket/directory owner fails before upload.
5. A disappeared mounted share fails without writing to the root filesystem.
6. Missing CIFS module reports running-kernel and installed-kernel facts without rebooting.
7. Interrupted upload leaves only a partial, non-restorable artifact.

### Live S3 proof

On `claude-r10b`:

1. deploy the program/config/secrets and the per-cluster cron to `gnode4`;
2. confirm no backup cron exists on `gnode5` or `gnode6`;
3. trigger the installed cron path and observe a new S3 backup within the bounded test window;
4. verify dedicated bucket ownership, encryption, both checksums, metadata, UUID/seqno, retention scope, state, JSONL events, and PMM metrics;
5. run the confirmation-gated restore drill on `rnode1` and verify all user tables with `mariadb-check`;
6. run the existing backup, restore, Galera, ProxySQL, PMM, and zero-hardcode probes.

### Live SMB and mounted-filesystem proof

1. Boot isolated `rnode1` into its already-installed kernel containing `cifs`; do not reboot a Galera member for this test.
2. Start a temporary isolated Samba test endpoint with SMB 3 encryption support.
3. run managed-SMB backup backend operations against that endpoint and restore the resulting artifact on `rnode1`;
4. verify mount options, credential cleanup, encrypted artifact, checksums, owner marker, atomic publication, retention, and unconditional unmount;
5. test pre-mounted filesystem publication and restore using a real mounted test filesystem;
6. remove all temporary Samba/mount test infrastructure.

Acceptance requires every relevant command to exit zero, all new probes to pass, a real cron-triggered S3 artifact, a real SMB artifact, successful restore of each backend, and no regression in the existing cluster verification suite.
