# Galera Backup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the controller-driven, S3-only backup path with one cluster-aware `galera-backup` program that runs manually or from cron on one configured Galera node and safely supports S3/MinIO, managed SMB, and an already-mounted network filesystem.

**Architecture:** One Python executable under `roles/galera_backup/files/galera-backup` owns configuration loading, locking, Galera safety checks, physical backup/restore, encryption, checksums, backend publication, retention, state, events, and node-exporter metrics. An Ansible role deploys that executable plus one per-cluster configuration/secrets directory and, only on the configured scheduler, one cron file. Repository validation and backend owner markers enforce cluster isolation before runtime credentials or storage can cross cluster boundaries.

**Tech Stack:** Python 3 standard library, MinIO Python SDK pinned by platform lockfile, MariaDB `mariadb-backup`, OpenSSL AES-256-CBC/PBKDF2, Ansible Core with `ansible.posix`, cron, CIFS/SMB 3, node-exporter textfile metrics, PMM/Grafana alert provisioning, Python `unittest`.

**Approved design:** `docs/superpowers/specs/2026-07-29-galera-backup-design.md`.

---

## Research basis

- Ansible can own a dedicated `/etc/cron.d` file, but `cron_file` jobs require a user and stable unique name: [ansible.builtin.cron](https://docs.ansible.com/ansible/latest/collections/ansible/builtin/cron_module.html).
- Ephemeral mounts must not be represented as persistent fstab state; the runner therefore performs a temporary SMB mount and validates the observed mount separately: [ansible.posix.mount](https://docs.ansible.com/ansible/latest/collections/ansible/posix/mount_module.html).
- Red Hat requires `cifs-utils`; a root-only credentials file avoids putting SMB credentials directly in the mount command, and `seal` requires SMB 3.x: [RHEL file-system administration](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/managing_file_systems/index).
- MinIO supports custom policies scoped to bucket resources: [MinIO multi-user guide](https://github.com/minio/minio/blob/master/docs/multi-user/README.md).
- `mc admin accesskey create` can generate both access and secret keys server-side and attach an inline policy, so the secret need not appear in argv: [MinIO access-key reference](https://docs.min.io/aistor/reference/cli/admin/mc-admin-accesskey/mc-admin-accesskey-create/).
- Live EL10 discovery already showed `cifs-utils` absent and the running `6.12.0-211.16.1.el10_2` kernel missing `cifs`, while installed `6.12.0-211.39.1.el10_2` contains it. The role must report this mismatch and fail; it must never reboot a database node.

## Global invariants

1. No playbook or role contains a literal OS-major branch, repository URL, package version, or cluster-specific hostname/address.
2. No compatibility wrapper, alias, legacy cron template, or old S3 helper remains after cutover.
3. Plaintext backup data, temporary SMB credentials, and restore work are removed in `finally`/`always` paths on both success and failure.
4. Secret values never enter command argv, JSONL events, state, metrics, metadata, Ansible output, or repository files.
5. `metadata.json` is always the final completeness marker. Missing metadata means non-restorable partial data.
6. A cluster can access only its configured bucket or owned share directory; both repository validation and runtime owner markers enforce this.
7. The cron scheduler is single-host by design. Reconfiguration removes the same cluster's cron file from every non-selected Galera node before installing it on the selected node.
8. Restore remains explicit (`CONFIRM=yes`), runs only on inventory group `restore`, and never touches Galera or ProxySQL hosts.
9. Production S3 requires TLS. Plain HTTP remains laboratory-only and does not weaken payload encryption or ownership checks.
10. No automatic reboot. EL10 SMB proof may reboot only isolated `rnode1`, after an explicit execution-time checkpoint.

## Final file map

**Create**

- `roles/galera_backup/files/galera-backup`
- `roles/galera_backup/tasks/main.yml`
- `roles/galera_backup/templates/config.json.j2`
- `roles/galera_backup/templates/secrets.env.j2`
- `roles/galera_backup/templates/cron.j2`
- `roles/galera_backup/templates/minio-policy.json.j2`
- `tests/unit/galera_backup_testlib.py`
- `tests/unit/test_backup_config_validator.py`
- `tests/unit/test_galera_backup_core.py`
- `tests/unit/test_galera_backup_s3.py`
- `tests/unit/test_galera_backup_filesystems.py`
- `tests/unit/test_galera_backup_workflow.py`
- `tests/unit/test_galera_backup_restore.py`
- `tests/validation/validate-backup-config.py`
- `tests/live/probe-galera-backup-backends.py`

**Modify**

- `clusters/schema/cluster.schema.json`
- every `clusters/*/cluster.yml`
- `versions/versions.lock.yml`
- `versions/versions-el10.lock.yml`
- `tests/validation/validate-lockfile.py`
- `.github/workflows/ci.yml`
- `playbooks/f10_backup.yml`
- `playbooks/f10_restore.yml`
- `playbooks/f11_node_exporter.yml`
- `playbooks/f11_freshness.yml`
- `playbooks/f15_alerts.yml`
- `tests/lab/probe-backup.py`
- `tests/lab/probe-restore.py`
- `tests/lab/probe-pmm-native.py`
- `tests/validation/probe-no-secrets-leak.sh`
- `Makefile`
- `README.md`
- `docs/runbooks/backup.md`
- `ISA.md` only where its current backup decisions/limitations become false

**Delete after every caller has moved**

- `roles/backup/files/s3_object.py`
- `roles/backup/templates/mariadb-backup.cron.j2`
- `tests/lab/backup-run.sh`

---

### Task 1: Make backup configuration structurally and semantically fail closed

**Files:**
- Create: `tests/unit/test_backup_config_validator.py`
- Create: `tests/validation/validate-backup-config.py`
- Modify: `clusters/schema/cluster.schema.json`
- Modify: all six `clusters/*/cluster.yml`
- Modify: `.github/workflows/ci.yml`

**Step 1: Write failing contract tests**

Use temporary cluster/inventory documents and invoke the validator as a subprocess. The tests must cover valid S3, SMB, and filesystem configurations and reject:

- missing `backup.scheduler`;
- scheduler host not in inventory group `galera`;
- malformed or non-five-field cron;
- `incremental_backup_schedule` other than `disabled`;
- `encryption_enabled: false`;
- mixed destination blocks, such as `destination: s3` plus `smb:`;
- relative staging, SMB, or filesystem paths;
- unsafe SMB options (`username=`, `password=`, `credentials=`, SMB 1/2 dialect, `noseal`);
- missing required `seal`, `nosuid`, `nodev`, or `noexec`;
- `secure: false` for a production S3 cluster;
- duplicate normalized `(endpoint, bucket)` pairs across two clusters;
- duplicate logical `cluster.name` or duplicate `monitoring.pmm.cluster_name` across cluster directories.

Core test shape:

```python
class BackupConfigValidatorTests(unittest.TestCase):
    def validate(self, *fixtures: tuple[dict, dict]) -> subprocess.CompletedProcess[str]:
        # Write cluster.yml + inventory.yml pairs below TemporaryDirectory,
        # then execute validate-backup-config.py against that root.
        ...

    def test_rejects_duplicate_normalized_s3_ownership(self):
        first = valid_s3(endpoint="HTTPS://s3.example:443/", bucket="orders")
        second = valid_s3(endpoint="s3.example", bucket="orders")
        result = self.validate(first, second)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate S3 ownership", result.stderr)
```

**Step 2: Run the focused tests and prove red**

Run:

```bash
python3 -m unittest tests.unit.test_backup_config_validator -v
```

Expected: `FAILED` because `validate-backup-config.py` and the new schema contract do not exist.

**Step 3: Extend the JSON schema**

Change `backup.destination` to exactly `s3 | smb | filesystem`. Add required `scheduler`:

```json
"scheduler": {
  "type": "object",
  "required": ["mode", "host", "timezone"],
  "additionalProperties": false,
  "properties": {
    "mode": { "type": "string", "enum": ["cron", "manual"] },
    "host": { "type": "string", "minLength": 1 },
    "timezone": { "type": "string", "const": "UTC" }
  }
}
```

Add mutually exclusive `s3`, `smb`, and `filesystem` blocks. Root-level schema conditions must require `backup.s3.secure=true` when `cluster.environment=production`. Keep `incremental_backup_schedule` and `restore_test_schedule` in the schema, but semantic validation requires incremental to be exactly `disabled`; no restore cron is installed.

**Step 4: Implement one cross-cluster semantic validator**

`validate-backup-config.py <clusters-root>` loads every directory containing both `cluster.yml` and `inventory.yml`. Implement these pure functions so tests can import them:

```python
def normalize_s3_endpoint(value: str, secure: bool) -> str: ...
def validate_cron(value: str) -> list[str]: ...
def validate_smb_options(options: list[str]) -> list[str]: ...
def validate_pair(cluster_path: Path, inventory_path: Path) -> list[str]: ...
def validate_unique_s3_owners(records: list[BackupRecord]) -> list[str]: ...
```

Endpoint normalization lowercases the host, strips a trailing slash, and removes only the matching default port (`443` for secure, `80` otherwise). It never resolves DNS. Report every error in one run; return non-zero if any exist.

**Step 5: Migrate every cluster in one cutover**

- `claude-r10b`: `mode: cron`, `host: gnode4`, `timezone: UTC`.
- `claude-r10`, `claude-pve`, `lab-cluster`, `lab2-cluster`: `mode: manual`, their first Galera inventory host, `timezone: UTC`.
- `example-cluster`: `mode: manual`, its first Galera host, plus a complete managed-SMB example with absolute mount point and required SMB 3.1.1 security options.
- Retain every existing S3 endpoint and bucket unchanged.

**Step 6: Gate CI**

Add a blocking CI step after schema/inventory validation:

```yaml
- name: Backup configuration semantics and ownership
  run: python3 tests/validation/validate-backup-config.py clusters
```

Add the unit test command later in Task 9 when all runner tests exist.

**Step 7: Verify green**

Run:

```bash
python3 -m unittest tests.unit.test_backup_config_validator -v
python3 tests/validation/validate-backup-config.py clusters
```

Expected: all unit cases `ok`; validator prints one `OK` line per cluster and a final unique-ownership summary.

**Step 8: Commit**

```bash
git add clusters .github/workflows/ci.yml tests/unit/test_backup_config_validator.py tests/validation/validate-backup-config.py
git commit -m "feat(backup): validate per-cluster scheduler and storage"
```

---

### Task 2: Put every backup platform dependency in lockfiles

**Files:**
- Modify: `versions/versions.lock.yml`
- Modify: `versions/versions-el10.lock.yml`
- Modify: `tests/validation/validate-lockfile.py`
- Modify: `tests/unit/test_backup_config_validator.py`

**Step 1: Add a failing lockfile test**

Add a test that removes each new key from a locked fixture and expects the validator to fail with the exact missing key. Required data:

```yaml
backup_tools:
  python_pip_package: "python3-pip"
  encryption_package: "openssl"
  archive_package: "tar"
  cron_package: "cronie"
  cifs_userspace_package: "cifs-utils"
minio:
  # existing fields remain
  mc_image: "minio/mc:RELEASE.2025-08-13T08-35-41Z"
  mc_image_digest: "sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727"
```

These values were resolved from the Docker Hub manifest list on 2026-07-29. The digest contains both `linux/amd64` and `linux/arm64`, and a live container invocation confirmed that this exact image implements `mc admin accesskey create/list/remove/edit`.

**Step 2: Prove red**

Run:

```bash
python3 -m unittest tests.unit.test_backup_config_validator.BackupLockfileTests -v
```

Expected: failure for missing `backup_tools` and MinIO client image keys.

**Step 3: Reverify the pinned MinIO client image**

Run:

```bash
docker buildx imagetools inspect minio/mc:RELEASE.2025-08-13T08-35-41Z
docker run --rm minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727 admin accesskey --help
```

Expected: the first command reports the pinned manifest-list digest with `linux/amd64` and `linux/arm64`; the second lists `create`, `list`, `remove`, and `edit`.

**Step 4: Update lockfiles and validator**

Extend `REQUIRED` in `validate-lockfile.py` with every `backup_tools` key plus `minio.mc_image` and `minio.mc_image_digest`. Add digest-format validation identical in strictness to existing image digest checks.

**Step 5: Verify**

Run:

```bash
python3 tests/validation/validate-lockfile.py versions/versions.lock.yml versions/versions-el10.lock.yml
python3 -m unittest tests.unit.test_backup_config_validator.BackupLockfileTests -v
```

Expected: two `OK:` lines and all tests `ok`.

**Step 6: Commit**

```bash
git add versions tests/validation/validate-lockfile.py tests/unit/test_backup_config_validator.py
git commit -m "build(backup): lock platform tools and MinIO client"
```

---

### Task 3: Build the runner's safe configuration, lock, state, event, and metric core

**Files:**
- Create: `roles/galera_backup/files/galera-backup`
- Create: `tests/unit/galera_backup_testlib.py`
- Create: `tests/unit/test_galera_backup_core.py`

**Step 1: Write failing core tests**

Load the extensionless executable with `importlib.machinery.SourceFileLoader`. Cover:

- cluster names accept only `^[A-Za-z0-9_-]+$` and cannot traverse directories;
- config JSON requires `format_version == 1` and exact `cluster_name`;
- `secrets.env` must be root-owned in production, mode no broader than `0600`, contain only selected-backend keys, and parse quoted special characters without shell execution;
- unknown, duplicate, multiline, or missing secret entries fail;
- a non-blocking second `fcntl.flock` attempt returns `E_LOCKED` and starts no work;
- atomic JSON writes survive an injected pre-rename failure without corrupting the previous state;
- failure state preserves the previous last-success timestamp;
- JSONL and metric output redact every loaded secret;
- subprocess argv containing any loaded secret is rejected before process creation;
- metric label escaping handles quotes, backslashes, and newlines.

Representative assertion:

```python
def test_secret_cannot_enter_subprocess_argv(self):
    runner = module.CommandRunner(secret_values={"s3cr3t"})
    with self.assertRaisesRegex(module.BackupError, "E_SECRET_IN_ARGV"):
        runner.run(["mount", "-o", "password=s3cr3t"])
    self.assertEqual(self.popen.call_count, 0)
```

**Step 2: Prove red**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_core -v
```

Expected: import or symbol failures because the executable is not implemented.

**Step 3: Implement the core, not the backends yet**

Use explicit types:

```python
@dataclass(frozen=True)
class Paths:
    install_root: Path
    cluster_dir: Path
    staging_root: Path
    datadir: Path
    socket: Path
    metric_file: Path

@dataclass(frozen=True)
class RunConfig:
    cluster_name: str
    metric_cluster_label: str
    local_role: str
    scheduler_system_hostname: str
    galera_nodes_expected: int
    mariadb_version: str
    retention_days: int
    flow_control_threshold_ns: int
    backend: Mapping[str, object]
    paths: Paths
```

Provide `BackupError(code, public_message)` and a fixed error-code set. Do not pass raw exception strings directly to events; sanitize against every secret first.

State shape:

```json
{
  "format_version": 1,
  "cluster": "claude-r10b",
  "last_run": {"command": "backup", "status": "failed", "error_code": "E_STORAGE"},
  "last_success": {"command": "backup", "unixtime": 0, "artifact": null},
  "last_failure": {"command": "backup", "unixtime": 0, "error_code": "E_STORAGE"}
}
```

Write state and metrics through temporary files in each target file's own directory, call `fsync`, set the final mode, then `os.replace`. The metric path is `/var/lib/node_exporter/textfile_collector/galera_backup-<sanitized-cluster-name>.prom`, so two logical clusters on one host cannot overwrite one another. Open events with `O_APPEND|O_CREAT` and emit one compact JSON object per line.

Metrics owned by the runner:

```text
galera_backup_last_success_unixtime{cluster="r10b-galera",logical_cluster="claude-r10b",backend="s3"} 0
galera_backup_last_failure_unixtime{cluster="r10b-galera",logical_cluster="claude-r10b",backend="s3"} 0
galera_backup_last_run_success{cluster="r10b-galera",logical_cluster="claude-r10b",backend="s3"} 0
galera_backup_last_size_bytes{cluster="r10b-galera",logical_cluster="claude-r10b",backend="s3"} 0
galera_backup_last_duration_seconds{cluster="r10b-galera",logical_cluster="claude-r10b",backend="s3"} 0
```

**Step 4: Verify green and compile**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_core -v
python3 -m py_compile roles/galera_backup/files/galera-backup
```

Expected: all tests `ok`; compile exits zero.

**Step 5: Commit**

```bash
git add roles/galera_backup/files/galera-backup tests/unit/galera_backup_testlib.py tests/unit/test_galera_backup_core.py
git commit -m "feat(backup): add fail-closed runner core"
```

---

### Task 4: Implement S3 ownership, publication, verification, and retention

**Files:**
- Modify: `roles/galera_backup/files/galera-backup`
- Create: `tests/unit/test_galera_backup_s3.py`

**Step 1: Write failing S3 backend tests with a fake MinIO client**

Test these observable contracts:

1. Empty bucket gets `galera-backup-owner.json` with exact cluster and format version.
2. Matching owner is idempotent.
3. Foreign owner fails before any backup object write.
4. A legacy non-empty bucket is claimed only when every object belongs to complete backups whose metadata has the current cluster; random, unreadable, mixed, or foreign objects fail.
5. Keys outside `galera-<cluster>-` are never listed as restore candidates or retention candidates.
6. Publication order is encrypted payload, checksum, then metadata.
7. Failure before metadata leaves no complete backup.
8. Read-back size and SHA-256 mismatch fail the run.
9. Retention uses validated metadata timestamps and deletes only all objects under expired current-cluster backup prefixes.
10. A malformed current-cluster metadata object blocks retention instead of being silently skipped.

**Step 2: Prove red**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_s3 -v
```

Expected: failures for missing `S3Backend` and ownership behavior.

**Step 3: Implement `S3Backend` behind a narrow interface**

```python
class StorageBackend(Protocol):
    def preflight(self) -> None: ...
    def publish(self, artifact: ArtifactSet) -> PublishedArtifact: ...
    def fetch_latest(self, work_dir: Path) -> ArtifactSet: ...
    def prune(self, now: datetime, retention_days: int) -> int: ...
    def close(self) -> None: ...
```

Instantiate `minio.Minio` lazily so static compile/unit tests do not require the SDK. Never call `make_bucket` with scoped runtime credentials: managed MinIO provisioning creates the bucket; external S3 operators create it. Runtime `preflight()` requires the configured bucket to exist.

Owner marker body:

```json
{"format_version":1,"cluster_name":"claude-r10b"}
```

For legacy claim, enumerate the whole bucket once. Any object not equal to the owner marker and not under a complete, valid, current-cluster backup prefix aborts with `E_OWNER_CONFLICT`.

**Step 4: Verify green**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_s3 -v
```

Expected: all ownership, ordering, partial, checksum, and retention cases `ok`.

**Step 5: Commit**

```bash
git add roles/galera_backup/files/galera-backup tests/unit/test_galera_backup_s3.py
git commit -m "feat(backup): enforce S3 ownership and atomic completeness"
```

---

### Task 5: Implement atomic pre-mounted filesystem storage

**Files:**
- Modify: `roles/galera_backup/files/galera-backup`
- Create: `tests/unit/test_galera_backup_filesystems.py`

**Step 1: Write failing filesystem tests**

Using real temporary directories plus mocked mount discovery, test:

- target is rejected when it is not an actual mount point;
- root filesystem and unexpected fstype are rejected;
- source, target, fstype, and mount ID must remain identical between preflight and final publish;
- foreign `galera-backup-owner.json` fails;
- publication writes `.partial-<backup>` and a same-filesystem `os.replace` exposes the final directory atomically;
- injected copy failure removes partial data and never creates final metadata;
- a disappeared mount fails before a write can fall through to the underlying local directory;
- restore sees only complete current-cluster directories;
- retention cannot leave the owned cluster directory.

**Step 2: Prove red**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_filesystems -v
```

Expected: failures for missing `FilesystemBackend`.

**Step 3: Implement mount identity and filesystem backend**

Read mount state from `findmnt --json --target <path> --output TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN,FSROOT`. Persist all returned identity fields from preflight and compare them again immediately before the first copy, before rename, and after rename.

The owned root is exactly:

```text
<configured-mount-point>/<cluster-name>/
```

The marker is created with `O_EXCL`; an existing marker is read and compared, never overwritten.

**Step 4: Verify green**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_filesystems -v
```

Expected: all atomicity, owner, mount-loss, and retention tests `ok`.

**Step 5: Commit**

```bash
git add roles/galera_backup/files/galera-backup tests/unit/test_galera_backup_filesystems.py
git commit -m "feat(backup): add owned mounted-filesystem backend"
```

---

### Task 6: Add managed SMB with unconditional credential and mount cleanup

**Files:**
- Modify: `roles/galera_backup/files/galera-backup`
- Modify: `tests/unit/test_galera_backup_filesystems.py`

**Step 1: Write failing managed-SMB tests**

Test:

- missing `mount.cifs` or unavailable running-kernel module yields `E_CIFS_MODULE` and reports running/installed kernel versions without calling mount;
- an already-mounted target fails rather than unmounting somebody else's share;
- credentials file is `0600`, below `/run/galera-backup`, and contains username/password/domain only;
- mount argv contains only the credentials-file path, never a secret;
- `vers=3.1.1`, `seal`, `nosuid`, `nodev`, and `noexec` are all required and verified from observed mount options;
- success unmounts and removes credentials;
- mount failure, publish failure, and unmount failure all remove credentials and return non-zero;
- an unmount failure is persisted as failure even if publication itself succeeded.

**Step 2: Prove red**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_filesystems.ManagedSMBTests -v
```

Expected: failures for missing `SMBBackend` and kernel/mount lifecycle behavior.

**Step 3: Implement `SMBBackend` as lifecycle around `FilesystemBackend`**

The only mount command form is:

```python
[
    "mount", "-t", "cifs", source, mount_point,
    "-o", ",".join(validated_options + [f"credentials={credentials_path}"])
]
```

No secret is in that list. Use `try/finally` so `umount <mount_point>` and credential unlink always execute. Verify `findmnt` source equals the configured UNC source and fstype equals `cifs` before delegating publication/restore to `FilesystemBackend`.

**Step 4: Verify green**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_filesystems -v
```

Expected: all filesystem and SMB lifecycle tests `ok`.

**Step 5: Commit**

```bash
git add roles/galera_backup/files/galera-backup tests/unit/test_galera_backup_filesystems.py
git commit -m "feat(backup): add fail-closed managed SMB backend"
```

---

### Task 7: Implement one physical backup workflow for every backend

**Files:**
- Modify: `roles/galera_backup/files/galera-backup`
- Create: `tests/unit/test_galera_backup_workflow.py`

**Step 1: Write failing workflow tests**

Use fake executables and a recording backend. Assert:

- hostname mismatch starts neither MariaDB query nor staging;
- local Galera must report `Primary`, `wsrep_ready=ON`, `wsrep_connected=ON`, state `4/Synced`, and exact expected cluster size;
- free space must cover raw backup plus archive plus encrypted artifact and safety margin;
- staging cannot be `/`, datadir, a datadir parent/child, or another protected system path;
- `mariadb-backup --backup --galera-info` precedes `--prepare`;
- wsrep UUID/seqno are taken from `mariadb_backup_galera_info` and validated;
- plaintext SHA and encrypted SHA are both correct;
- OpenSSL receives `enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt -pass env:GALERA_BACKUP_ENCRYPTION_KEY`, never the key value; restore uses the same recorded parameters;
- metadata contains every required field and exact cluster/backend/format version;
- flow-control excess prevents publication even when scheduler is the writer;
- backend preflight occurs before the physical backup;
- backend metadata verification occurs before success state;
- every injected failure removes staging and plaintext and returns non-zero.

One ordering assertion must look like:

```python
self.assertLess(events.index("backend.preflight"), events.index("mariadb-backup.backup"))
self.assertLess(events.index("backend.verify"), events.index("state.success"))
```

**Step 2: Prove red**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_workflow -v
```

Expected: failures for missing `run_backup` behavior.

**Step 3: Implement `run_backup`**

Order is fixed:

1. acquire lock;
2. load/validate config and secrets;
3. validate hostname and paths;
4. backend ownership/availability preflight;
5. local Galera health and free-space preflight;
6. record local `WSREP_FLOW_CONTROL_PAUSED_NS`;
7. create a unique `0700` work directory with `tempfile.mkdtemp(dir=staging_root)`;
8. run backup and prepare;
9. create a compressed tar, calculate plaintext SHA-256, encrypt with AES-256-CBC/PBKDF2 using 200,000 iterations and SHA-256, calculate encrypted SHA-256, then write checksum and metadata containing those exact cryptographic parameters;
10. read final flow-control counter and fail before publication when delta exceeds the configured threshold;
11. publish, read back, and verify;
12. prune only after verified publication;
13. write success state/events/metrics;
14. clean work in `finally`.

Do not use `shell=True`. Every external command is an argv list passed through the secret-aware `CommandRunner`.

**Step 4: Verify green**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_workflow -v
```

Expected: all sequencing, cryptographic, flow-control, and cleanup tests `ok`.

**Step 5: Commit**

```bash
git add roles/galera_backup/files/galera-backup tests/unit/test_galera_backup_workflow.py
git commit -m "feat(backup): run one encrypted Galera physical backup"
```

---

### Task 8: Implement confirmation-gated restore through the same backends

**Files:**
- Modify: `roles/galera_backup/files/galera-backup`
- Create: `tests/unit/test_galera_backup_restore.py`

**Step 1: Write failing restore tests**

Cover:

- missing `--confirm` returns `E_RESTORE_CONFIRM` before any datadir operation;
- config role must be `restore`, actual hostname must match the rendered restore hostname, and it must differ from scheduler hostname;
- latest selection ignores foreign, partial, malformed, and wrong-format metadata;
- encrypted checksum is verified before decrypt;
- plaintext checksum is verified before extraction;
- backup MariaDB major/minor may be equal to or older than the restore host, never newer;
- tar traversal (`../`), absolute paths, symlinks, devices, and FIFOs are rejected;
- datadir guard allows only the configured exact restore datadir and empties contents without deleting the mount point;
- `mariadb-backup --copy-back` precedes standalone startup;
- every user database is passed to `mariadb-check`, and at least one user table with at least one row is required;
- standalone MariaDB stops and work/plaintext disappear on success, checksum failure, startup timeout, and integrity failure;
- restore success state contains artifact, row count, checksum, and timestamp but no secret.

**Step 2: Prove red**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_restore -v
```

Expected: failures for missing `run_restore`.

**Step 3: Implement safe restore**

Expose CLI only as:

```text
galera-backup backup <cluster-name>
galera-backup restore <cluster-name> --confirm
```

Implement a tar-member validator before extraction. Do not trust encryption as authentication; every archive member must remain below the extraction root and must be a regular file or directory.

Start standalone MariaDB with `subprocess.Popen(..., start_new_session=True)` and a dedicated log file. Wait for the socket with a bounded monotonic deadline. In `finally`, attempt `mariadb-admin ... shutdown`, then terminate/kill only the recorded child PID if still alive; never use a broad process kill as the primary mechanism.

**Step 4: Verify green**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_restore -v
```

Expected: all confirmation, safety, version, integrity, and cleanup tests `ok`.

**Step 5: Commit**

```bash
git add roles/galera_backup/files/galera-backup tests/unit/test_galera_backup_restore.py
git commit -m "feat(backup): restore and verify every backend safely"
```

---

### Task 9: Deploy the runner, scoped MinIO identity, config, secrets, and single cron

**Files:**
- Create: `roles/galera_backup/tasks/main.yml`
- Create: `roles/galera_backup/templates/config.json.j2`
- Create: `roles/galera_backup/templates/secrets.env.j2`
- Create: `roles/galera_backup/templates/cron.j2`
- Create: `roles/galera_backup/templates/minio-policy.json.j2`
- Modify: `playbooks/f10_backup.yml`
- Modify: `.github/workflows/ci.yml`

**Step 1: Add failing template/role contract tests**

Extend `test_galera_backup_core.py` to render templates with Jinja and assert:

- config contains no secret values;
- secrets file contains exactly the backend-required `GALERA_BACKUP_*` keys;
- cron contains `CRON_TZ=UTC`, explicit PATH, user `root`, one absolute runner invocation, no `systemd-cat` dependency, and no secrets;
- policy grants `GetBucketLocation` and full key-name `ListBucket` on exactly the configured bucket so legacy foreign objects cannot be hidden by a prefix filter;
- policy grants get/put/delete/multipart object actions only for `galera-<cluster>-*` and `galera-backup-owner.json`;
- policy contains no wildcard bucket resource and no access to a second bucket.

**Step 2: Prove red**

Run:

```bash
python3 -m unittest tests.unit.test_galera_backup_core.TemplateContractTests -v
```

Expected: template-not-found failures.

**Step 3: Implement role installation**

Role behavior:

- load tool names and MinIO SDK/client image from the selected lockfile;
- install common packages from `lock.backup_tools`; install CIFS userspace only for managed SMB; install/start cron only for `scheduler.mode=cron`;
- install MinIO SDK at exactly `lock.minio.sdk_version`;
- deploy `/opt/galera-backup/galera-backup` and `/opt/galera-backup/clusters/<cluster>/config.json` on the scheduler and every restore host;
- create the same backend `secrets.env` on scheduler/restore hosts using `no_log: true`, root ownership, and mode `0600`;
- publish the resolved non-logging `galera_backup_shared_secrets` Ansible fact from the scheduler play so the following restore-host play receives identical scoped credentials without re-running MinIO provisioning;
- preserve an existing valid restore-host secrets file when a later disaster-recovery invocation cannot reach the scheduler;
- remove `/usr/local/bin/s3_object.py` and this cluster's legacy cron file;
- install `/etc/cron.d/galera-backup-<sanitized-cluster>` only on the selected scheduler; manual mode removes it;
- report missing `cifs` helper/module with running and newest installed kernel, but never reboot.

**Step 4: Provision managed MinIO credentials without a secret argv**

When the S3 endpoint host matches the inventory infra host:

1. render a bucket/prefix-scoped inline policy to a root-only temporary directory on infra;
2. run pinned `minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727` with MinIO root credentials supplied through a root-only Docker `--env-file`, never argv;
3. create the bucket idempotently;
4. if scheduler `secrets.env` already has a scoped access key, use `mc admin accesskey edit --policy <root-only-policy-file>` to converge its policy;
5. otherwise remove any stale key whose MinIO name is exactly `galera-backup-<cluster>`, execute `mc admin accesskey create <alias>/ --json --name galera-backup-<cluster> --policy <root-only-policy-file>`, and let MinIO generate both keys;
6. parse the one-time JSON under `no_log`, write only the scoped key pair to scheduler and restore `secrets.env`, then remove temporary root env/policy files;
7. prove the scoped key can access its bucket and cannot access a root-created decoy bucket during live verification.

For an external S3 endpoint, require `GALERA_BACKUP_S3_ACCESS_KEY` and `GALERA_BACKUP_S3_SECRET_KEY` from the controller environment and deploy only those values.

**Step 5: Make `f10_backup.yml` a thin configure/run entrypoint**

Use three plays:

```yaml
- name: Remove legacy and stale scheduler cron from Galera nodes
  hosts: galera
  tasks:
    - name: Remove legacy controller-era cron
      ansible.builtin.file:
        path: "/etc/cron.d/mariadb-backup-{{ cluster.name }}"
        state: absent
      when: galera_backup_action == 'configure'

    - name: Remove current cron from every non-selected node
      ansible.builtin.file:
        path: "/etc/cron.d/galera-backup-{{ cluster.name }}"
        state: absent
      when:
        - galera_backup_action == 'configure'
        - inventory_hostname != backup.scheduler.host

- name: Configure or run Galera backup on the selected scheduler
  hosts: "{{ backup.scheduler.host }}"
  roles:
    - role: galera_backup
      vars:
        galera_backup_local_role: scheduler
      when: galera_backup_action == 'configure'
  tasks:
    - ansible.builtin.command:
        argv: [/opt/galera-backup/galera-backup, backup, "{{ cluster.name }}"]
      when: galera_backup_action == 'run'

- name: Pre-position restore executable and scoped secrets
  hosts: restore
  roles:
    - role: galera_backup
      vars:
        galera_backup_local_role: restore
        galera_backup_install_cron: false
        galera_backup_provision_s3: false
        galera_backup_shared_secrets: >-
          {{ hostvars[backup.scheduler.host].galera_backup_shared_secrets }}
      when: galera_backup_action == 'configure'
```

Add assertions for the action enum and scheduler membership. `configure` installs on scheduler/restore and makes the restore host independent of live Galera availability; `run` invokes the already-installed scheduler executable and therefore needs no controller secrets.

**Step 6: Add all unit tests to CI**

```yaml
- name: Galera backup unit tests
  run: python3 -m unittest discover -s tests/unit -p 'test_*.py' -v

- name: Galera backup executable compile
  run: python3 -m py_compile roles/galera_backup/files/galera-backup
```

**Step 7: Verify role syntax and unit contracts**

Run:

```bash
python3 -m unittest discover -s tests/unit -p 'test_*.py' -v
ansible-playbook playbooks/f10_backup.yml -i clusters/example-cluster/inventory.yml -e @clusters/example-cluster/cluster.yml -e galera_backup_action=configure --syntax-check
```

Expected: all tests `ok`; syntax check reports the playbook.

**Step 8: Commit**

```bash
git add roles/galera_backup playbooks/f10_backup.yml .github/workflows/ci.yml tests/unit
git commit -m "feat(backup): deploy scoped runner and per-cluster cron"
```

---

### Task 10: Cut manual backup/restore and monitoring over; delete legacy code

**Files:**
- Modify: `playbooks/f10_restore.yml`
- Modify: `playbooks/f11_node_exporter.yml`
- Modify: `playbooks/f11_freshness.yml`
- Modify: `playbooks/f15_alerts.yml`
- Modify: `tests/lab/probe-backup.py`
- Modify: `tests/lab/probe-restore.py`
- Modify: `tests/lab/probe-pmm-native.py`
- Modify: `tests/validation/probe-no-secrets-leak.sh`
- Modify: `Makefile`
- Delete: `roles/backup/files/s3_object.py`
- Delete: `roles/backup/templates/mariadb-backup.cron.j2`
- Delete: `tests/lab/backup-run.sh`

**Step 1: Write failing cutover assertions**

Add unit/static assertions that:

- restore invokes the installed runner and no longer performs S3/encryption/copy-back logic itself;
- no caller references `backup-run.sh`, `s3_object.py`, `/var/lib/mariadb-backup-state`, or `isa_backup_last_success_unixtime`;
- PMM probe expects the five `galera_backup_*` metrics;
- alert definitions include both `Backup run failed` and `Backup freshness stale`.

Do not test arbitrary source strings as the only proof; these assertions supplement, not replace, the live playbook and PMM checks.

**Step 2: Prove red**

Run:

```bash
python3 -m unittest discover -s tests/unit -p 'test_*.py' -v
```

Expected: cutover assertions fail while legacy callers remain.

**Step 3: Thin `f10_restore.yml`**

Keep only:

- confirmation and inventory disjointness assertions;
- lockfile-driven MariaDB repository and exact package installation on `restore`;
- `galera_backup` role deployment with cron disabled and local role `restore`; it preserves pre-positioned scoped secrets, can create a replacement managed-MinIO access key through the infra host when that file is missing, and requires controller-supplied backend credentials for a missing external-S3/SMB file;
- command argv `/opt/galera-backup/galera-backup restore {{ cluster.name }} --confirm`.

The runner, not YAML shell blocks, owns download, checksum, decrypt, safe extraction, copy-back, standalone process lifecycle, integrity checks, state, and cleanup.

**Step 4: Update Make targets**

Add `cluster-backup-configure` and change existing targets:

```make
cluster-backup-configure:
	$(cluster_guard)
	ansible-playbook playbooks/f10_backup.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e galera_backup_action=configure $(ANSIBLE_OPTS)

cluster-backup:
	$(cluster_guard)
	ansible-playbook playbooks/f10_backup.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e galera_backup_action=run $(ANSIBLE_OPTS)

cluster-restore-drill:
	$(cluster_guard)
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (drill kasuje datadir hosta grupy restore)"; exit 1)
	ansible-playbook playbooks/f10_restore.yml -i clusters/$(CLUSTER)/inventory.yml -e @clusters/$(CLUSTER)/cluster.yml -e restore_confirm=yes $(ANSIBLE_OPTS)
```

No target invokes a script from `tests/`.

**Step 5: Cut monitoring over without duplicate Prometheus series**

- `f11_node_exporter.yml`: remove the baseline `isa_backup_last_success_unixtime` series; retain restore/TLS baseline.
- `f11_freshness.yml`: stop scanning all Galera nodes for old backup JSON. Continue publishing restore/TLS only; read restore success from `/opt/galera-backup/clusters/<cluster>/state.json` on `restore[0]`.
- `f15_alerts.yml`: stale query uses `max(galera_backup_last_success_unixtime{cluster=\"{{ cluster_label }}\"})`; add immediate failure query on `galera_backup_last_run_success` with `noDataState: Alerting`.
- `probe-pmm-native.py`: require fresh runner metrics from the scheduler node and verify both alert rules.

**Step 6: Update probes to scoped credentials and new metadata**

`probe-backup.py` uses `GALERA_BACKUP_S3_ACCESS_KEY/SECRET_KEY`, requires the exact owner marker, filters exact cluster prefix, checks both encrypted and plaintext SHA fields, format version, backend, size, UUID/seqno, and metadata-last completeness.

`probe-restore.py` reads the new restore `state.json` and requires `last_run.command=restore`, success, positive row count, and exact cluster/artifact.

Extend the no-secret probe with behavioral subprocess tests from the unit suite and source scanning for accidental literal secret assignment; do not print matched secret values.

**Step 7: Delete legacy code only after grep shows no callers**

Run a repository search for the three legacy paths. When only the files themselves remain, delete them and their now-empty `roles/backup` directory.

**Step 8: Verify cutover**

Run:

```bash
python3 -m unittest discover -s tests/unit -p 'test_*.py' -v
python3 -m py_compile roles/galera_backup/files/galera-backup tests/lab/probe-backup.py tests/lab/probe-restore.py
ansible-playbook playbooks/f10_backup.yml -i clusters/example-cluster/inventory.yml -e @clusters/example-cluster/cluster.yml -e galera_backup_action=configure --syntax-check
ansible-playbook playbooks/f10_restore.yml -i clusters/example-cluster/inventory.yml -e @clusters/example-cluster/cluster.yml -e restore_confirm=yes --syntax-check
make verify-zero-hardcode
bash tests/validation/probe-no-secrets-leak.sh
```

Expected: every command exits zero; zero-hardcode reports `0 hardcoded cluster data`; secret probe reports PASS.

**Step 9: Commit**

```bash
git add -A Makefile playbooks roles tests
git commit -m "refactor(backup): cut all callers over to galera-backup"
```

---

### Task 11: Smoke-test real S3 backup and restore on Rocky 10

**Files:** no planned source changes; fix root causes and add regression tests if any step exposes a defect.

**Step 1: Establish live preconditions**

Load the existing out-of-repository environment and confirm cluster health:

```bash
set -a; . /tmp/isa-claude-r10b.env; set +a
make cluster-health CLUSTER=claude-r10b
```

Expected: 3/3 Primary/Synced/Ready and exact expected cluster size.

**Step 2: Configure managed MinIO and scheduler**

Root MinIO credentials remain controller/infra-only:

```bash
make cluster-backup-configure CLUSTER=claude-r10b
```

Expected: runner/config/secrets on `gnode4`; one cron file on `gnode4`; no same-cluster cron on `gnode5`/`gnode6`; scoped credentials in `secrets.env`; no MinIO root values on any Galera host.

**Step 3: Prove bucket-level least privilege**

Using MinIO root only on infra, create an empty decoy bucket. Using the generated scoped credentials, prove:

- list/get/put/delete succeeds in `r10b-galera-backups` only under current owner/prefix;
- list or put to the decoy bucket returns AccessDenied;
- owner marker says `claude-r10b` and format 1.

Remove the decoy bucket with root credentials after the test.

**Step 4: Run the installed manual path**

```bash
make cluster-backup CLUSTER=claude-r10b
make lab-backup-verify CLUSTER=claude-r10b
```

Expected: a new `galera-claude-r10b-*` complete artifact; encryption/checksums/metadata/UUID/seqno/owner all pass.

**Step 5: Run confirmation-gated restore**

```bash
make cluster-restore-drill CLUSTER=claude-r10b CONFIRM=yes
make lab-restore-verify CLUSTER=claude-r10b
```

Expected: standalone restore on `rnode1`, all user tables `mariadb-check` OK, positive row count, work directory absent afterward.

**Step 6: Inspect operational evidence**

On `gnode4`, verify mode/ownership of config and secrets, success state, one-line JSON events, metrics, empty staging, and no plaintext. On `rnode1`, verify standalone MariaDB is stopped and restore work is absent.

**Step 7: Commit only defect-driven changes**

If smoke testing required a fix, add a regression test first, apply the minimal fix, rerun the failed scenario, then commit one focused change. Otherwise create no evidence-only commit.

---

### Task 12: Prove actual cron, concurrency, failure state, and PMM alerts

**Files:** no planned source changes; defect fixes require tests.

**Step 1: Test the real cron daemon, not a manual imitation**

Create a temporary copy of `claude-r10b` config outside the repository whose schedule is two minutes ahead in UTC. Deploy it with the same cluster name, record the current latest metadata key, then wait up to five minutes.

Expected: `crond` launches `/opt/galera-backup/galera-backup backup claude-r10b`; a newer metadata key appears; state/events/metrics timestamps match the cron window.

Immediately reconverge the repository config so the installed schedule returns to `0 2 * * *`.

**Step 2: Test lock contention without starting a second physical backup**

Hold `/run/lock/galera-backup-claude-r10b.lock` from one SSH session, invoke `make cluster-backup`, and release the lock.

Expected: non-zero exit with `E_LOCKED`; no `mariadb-backup` child; state `locked`; failure timestamp and `last_run_success=0`; prior last-success timestamp preserved.

Run one successful backup afterward so the latest-run metric returns to 1.

**Step 3: Inject wrong scoped S3 credentials**

Temporarily replace only the scheduler's scoped secret with a wrong value under mode `0600`, run backup, and restore the original secret in an unconditional controller-side cleanup step.

Expected: backend preflight fails before `mariadb-backup`; no metadata; no plaintext/staging; persisted `E_STORAGE_AUTH`; secret absent from argv/events/state/Ansible output.

Run a successful backup afterward.

**Step 4: Verify PMM ingestion and alerts**

Query PMM/Prometheus for all five `galera_backup_*` metrics on the scheduler node. Verify Grafana has both managed alert rules, `noDataState: Alerting`, and correct cluster matcher. Trigger a failure long enough to observe the immediate-failure rule pending/firing, then recover it with a successful run.

**Step 5: Re-run focused probes**

```bash
make lab-monitoring-verify CLUSTER=claude-r10b
make lab-backup-verify CLUSTER=claude-r10b
```

Expected: both PASS with fresh metrics and managed rules.

---

### Task 13: Prove managed SMB and pre-mounted share on Rocky 10 without rebooting Galera

**Files:**
- Create: `tests/live/probe-galera-backup-backends.py`
- Defect fixes require matching unit regression tests.

**Step 1: Add the live backend probe before touching hosts**

The probe imports the installed runner on `rnode1`, takes an already-verified real encrypted S3 artifact, and exercises the exact `SMBBackend`/`FilesystemBackend` `preflight`, owner, `publish`, read-back, retention, and `fetch_latest` methods. It must never synthesize an empty or fake backup payload.

**Step 2: Prove current-kernel failure is diagnostic and non-destructive**

Before reboot, run managed-SMB preflight on `rnode1`.

Expected on the currently observed old kernel: `E_CIFS_MODULE`, running and installed kernel versions in the public diagnostic, no mount attempt, no reboot.

**Step 3: Explicit execution-time checkpoint, then reboot only `rnode1`**

Confirm `rnode1` is still exclusively in inventory group `restore` and no standalone restore process runs. Reboot it into the installed kernel containing `cifs`; wait for SSH and verify `modprobe -n -v cifs` succeeds.

Do not reboot `gnode4`, `gnode5`, or `gnode6` for this proof.

**Step 4: Start an ephemeral Samba endpoint**

Run a pinned Samba test container on the infra node with:

- SMB 3.1.1 only;
- server-side encryption required;
- one random test user/password supplied outside argv/logs;
- one temporary share/dataset;
- ingress restricted to the restore node during the test.

Record container/image digest and remove the container, credentials, firewall rule, and data in unconditional cleanup.

**Step 5: Exercise managed SMB with a real backup artifact**

The live probe publishes the real S3 artifact set into the SMB share through `SMBBackend` and verifies:

- observed `cifs` source and `vers=3.1.1,seal,nosuid,nodev,noexec`;
- owner marker and cluster directory;
- atomic final directory with metadata last;
- both checksums and encrypted size;
- credentials file absent and mount unmounted afterward.

Then deploy restore config for SMB on `rnode1`, run the normal confirmation-gated runner restore, and require `mariadb-check` plus positive user row count.

**Step 6: Exercise already-mounted filesystem mode on the same real share**

Mount the test share explicitly outside the runner, set `destination=filesystem` and `expected_fstype=cifs` in a temporary out-of-repository config, and publish the same real artifact through `FilesystemBackend`.

Expected: the runner neither mounts nor unmounts; ownership/atomicity/checksums pass; normal restore passes. Unmount the share externally.

**Step 7: Inject mount-loss and foreign-owner failures**

- No mount at configured filesystem path: fail before write; underlying local directory remains empty.
- Foreign owner marker in a separate temporary share root: fail before artifact copy.
- Wrong SMB password: fail, credentials file absent, mount absent, metadata absent.

**Step 8: Cleanup and restore production S3 configuration**

Remove all temporary test config, Samba container, share data, credentials, firewall changes, and mounts. Re-run `cluster-backup-configure` from the repository S3 config and one successful S3 backup/restore so final state reflects the production-configured backend.

**Step 9: Commit the reusable live probe**

```bash
git add tests/live/probe-galera-backup-backends.py
git commit -m "test(backup): verify SMB and mounted-share backends live"
```

---

### Task 14: Prove EL9 portability, finish documentation, and run final regression

**Files:**
- Modify: `README.md`
- Modify: `docs/runbooks/backup.md`
- Modify: `ISA.md` only for now-obsolete backup decisions
- Modify any source/tests only for defects found by verification

**Step 1: Run the EL9 S3 path in the Rocky 9 lab**

Bring up the existing `tests/lab/Dockerfile`-based Rocky 9 lab, configure backup in manual mode first, execute one S3 backup and restore, then install a next-minute temporary cron schedule and observe a second artifact.

Expected: same runner and role, different lockfile; no OS-major branch; S3 backup/restore and real cron pass. SMB live proof remains EL10 because OrbStack's host kernel does not provide CIFS.

**Step 2: Update operator documentation only after both live platforms work**

Document:

- `make cluster-backup-configure`, manual backup, cron behavior, and restore confirmation;
- exact S3, SMB, and filesystem configuration examples;
- managed MinIO credential generation and rotation/reconfigure procedure;
- `secrets.env`, config, state, events, metrics, cron, lock, and staging paths;
- failure codes and first diagnostic commands;
- missing CIFS module/kernel mismatch procedure with no automatic reboot;
- per-cluster bucket/directory ownership and migration of an existing bucket;
- honest limitation: repository MinIO is off-cluster from Galera but is not immutable/off-site disaster recovery unless its underlying storage is independently protected.

Remove text claiming backup always chooses a non-writer, supports S3 only, uses old `BACKUP_*` variables, or schedules restore drills automatically.

**Step 3: Run the complete static suite**

```bash
python3 -m unittest discover -s tests/unit -p 'test_*.py' -v
python3 tests/validation/validate-backup-config.py clusters
python3 tests/validation/validate-lockfile.py versions/versions.lock.yml versions/versions-el10.lock.yml
python3 -m compileall -q tests
python3 -m py_compile roles/galera_backup/files/galera-backup
make verify-zero-hardcode
bash tests/validation/probe-no-secrets-leak.sh
```

Then run schema validation for every cluster and `ansible-playbook --syntax-check` for every real playbook exactly as CI does.

Expected: all zero.

**Step 4: Run the live `claude-r10b` regression suite**

At minimum:

```bash
make cluster-health CLUSTER=claude-r10b
make lab-galera-verify CLUSTER=claude-r10b
make lab-proxysql-verify CLUSTER=claude-r10b
make lab-endpoint-verify CLUSTER=claude-r10b
make lab-backup-verify CLUSTER=claude-r10b
make lab-restore-verify CLUSTER=claude-r10b
make lab-monitoring-verify CLUSTER=claude-r10b
```

Run every remaining existing cluster probe used in the prior 14/14 suite, including firewall, hardening, rolling restart state, upgrade plan, patch state, drift, cache, and zero-hardcode. Expected: no regression and all relevant probes PASS.

**Step 5: Re-run the thing, not only tests**

Finish with one repository-configured manual S3 backup and one confirmation-gated restore on `claude-r10b`. Verify the final scheduler cron is exactly `0 2 * * *`, only on `gnode4`, and the latest PMM metrics/state are successful.

**Step 6: Commit cleanup/documentation**

```bash
git add README.md docs/runbooks/backup.md ISA.md
# Add any defect fixes and their tests only if they were necessary.
git commit -m "docs(backup): document scheduled multi-backend operations"
```

**Step 7: Final acceptance evidence**

Report exact command outputs for:

- unit/static/CI-equivalent checks;
- EL9 S3 manual + cron + restore;
- EL10 S3 manual + real cron + restore;
- EL10 managed SMB publication + restore;
- EL10 pre-mounted filesystem publication + restore;
- cross-bucket AccessDenied;
- lock, wrong credentials, foreign owner, missing mount, and missing-module failures;
- PMM freshness and immediate-failure metrics/rules;
- final complete live regression count.

Do not claim immutable/off-site protection beyond what the observed backend actually provides.
