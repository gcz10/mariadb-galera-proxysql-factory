#!/usr/bin/env python3
"""Verify the latest off-cluster backup (S3 or SMB) without touching the storage.

Checks: ISC-32 (backup stored off-cluster, complete artifact, owned by this
cluster), ISC-33 (encrypted), ISC-34 (sha256 checksum matches), ISC-35
(metadata has MariaDB version, time, cluster name and wsrep seqno, and its
`created_unixtime` is within `backup.freshness_sla_hours`).

Nothing is written, mounted, copied to disk or restored:
  * S3: the bucket is listed and the newest artifact is streamed into a SHA-256.
  * SMB: `_backup_artifact_reader.py` runs read-only ON the backup scheduler
    host through the ansible ad-hoc channel, using the installed runner config
    (/opt/galera-backup/clusters/<cluster>/config.json). The share is read
    only if it is already mounted as the runner requires, or through
    `smbclient`. Otherwise the result is UNDETERMINED, never PASS.

Credentials:
  * environment=production: S3 keys come ONLY from the process environment
    (GALERA_BACKUP_S3_ACCESS_KEY / GALERA_BACKUP_S3_SECRET_KEY). tests/lab/.env
    and MINIO_ROOT_* are never consulted, and the lab-only S3_PROBE_ENDPOINT
    override is refused.
  * otherwise (lab): the same variables, falling back to tests/lab/.env and
    MINIO_ROOT_USER / MINIO_ROOT_PASSWORD, exactly as before.
SMB credentials never reach the controller; the host-side reader takes them
from the installed secrets.env and never prints them.
"""

import base64
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import urllib3
from minio import Minio
from minio.error import S3Error
from urllib3.util import Timeout

import _backup_artifact_reader as artifact_reader
from _probe_common import ProbeContext, finish, require_hosts, run_ansible

CTX = ProbeContext()
CLUSTER = CTX.config
PRODUCTION = (CLUSTER.get("cluster") or {}).get("environment") == "production"
PROBE_ENDPOINT_OVERRIDE = os.environ.get("S3_PROBE_ENDPOINT", "")

inventory_addresses = {}
for group in CTX.inventory.get("all", {}).get("children", {}).values():
    for host, values in (group.get("hosts", {}) or {}).items():
        host_vars = values or {}
        if host_vars.get("ansible_host"):
            inventory_addresses[host] = host_vars["ansible_host"]
        else:
            inventory_addresses.setdefault(host, host)
CLUSTER_NAME = CLUSTER["cluster"]["name"]
REQUIRED_META = [
    "cluster_name",
    "mariadb_version",
    "created_at",
    "wsrep_seqno",
    "wsrep_uuid",
    "sha256_encrypted",
    "sha256_plaintext",
    "size_bytes",
    "format_version",
    "backend",
]
ARTIFACT_FILES = artifact_reader.ARTIFACT_FILES
OWNER_KEY = artifact_reader.OWNER_MARKER
READER_PATH = Path(artifact_reader.__file__)
BACKUP_INSTALL_ROOT = "/opt/galera-backup"
SMB_READ_TIMEOUT_SECONDS = 3300
# Slack for clock differences between the backup host and this controller.
MAX_FUTURE_SKEW_SECONDS = 300
MAX_TEXT_BYTES = 1 << 20
CHUNK_BYTES = 1 << 20


@dataclass
class Artifact:
    """What was read from one backup directory/prefix; the judge sees only this."""

    name: str
    files: dict
    metadata_text: Optional[str] = None
    checksum_text: Optional[str] = None
    oversize: list = field(default_factory=list)
    payload_magic: bytes = b""
    payload_sha256: str = ""
    payload_size: int = 0


def check(cond, msg, failures):
    if not cond:
        failures.append(msg)


# === judging (shared by S3 and SMB) =========================================


def freshness_sla(backup):
    value = backup.get("freshness_sla_hours")
    if type(value) is int and value >= 1:
        return value
    return None


def freshness_failure(meta, sla_hours, now):
    created = meta.get("created_unixtime")
    if type(created) is not int or created < 0:
        return (
            "ISC-35 — metadata created_unixtime missing or not a non-negative "
            f"integer: {created!r}"
        )
    age = now - created
    if age < -MAX_FUTURE_SKEW_SECONDS:
        return f"ISC-35 — metadata created_unixtime {created} is {-age:.0f}s in the future"
    if age > sla_hours * 3600:
        return (
            f"ISC-35 — latest backup is stale: {age / 3600:.1f}h old > "
            f"freshness SLA {sla_hours}h (backup.freshness_sla_hours)"
        )
    return None


def judge_owner_marker(text, oversize, cluster_name, failures):
    if oversize:
        failures.append(f"{OWNER_KEY} missing or invalid: larger than {MAX_TEXT_BYTES} bytes")
        return
    if text is None:
        failures.append(f"{OWNER_KEY} missing or invalid: not found")
        return
    try:
        owner_info = json.loads(text)
    except ValueError as exc:
        failures.append(f"{OWNER_KEY} missing or invalid: {exc}")
        return
    if not isinstance(owner_info, dict):
        failures.append(f"{OWNER_KEY} missing or invalid: not a JSON object")
        return
    check(
        owner_info.get("cluster_name") == cluster_name,
        f"Owner marker cluster_name '{owner_info.get('cluster_name')}' != "
        f"'{cluster_name}'",
        failures,
    )
    check(
        owner_info.get("format_version") == 1,
        f"Owner marker format_version {owner_info.get('format_version')} != 1",
        failures,
    )


def missing_file_failures(files, name, failures):
    for filename in ARTIFACT_FILES:
        check(
            bool(files.get(filename)),
            f"ISC-32 — missing {filename} in {name}",
            failures,
        )


def judge_artifact(artifact, cluster_name, sla_hours, now, failures):
    """ISC-32..35 plus freshness for one artifact; returns its metadata or None."""
    before = len(failures)
    missing_file_failures(artifact.files, artifact.name, failures)
    if len(failures) != before:
        return None

    # ISC-33: AES-GCM v3/v2 or legacy v1 (OpenSSL salted magic). Plaintext
    # tar/gzip is not accepted in any supported format.
    magic = artifact.payload_magic
    check(
        magic[:4] in (b"GB3G", b"GB2G") or magic == b"Salted__",
        f"ISC-33 — backup not encrypted (magic={magic!r}, "
        f"expected GB3G/GB2G (AES-GCM) or OpenSSL Salted__)",
        failures,
    )

    # ISC-34: sha256 of the encrypted artifact matches the stored checksum.
    computed = artifact.payload_sha256
    if "backup.sha256" in artifact.oversize or artifact.checksum_text is None:
        failures.append("ISC-34 — backup.sha256 invalid: unreadable or oversized")
        checksum_parts = []
    else:
        checksum_parts = artifact.checksum_text.split()
    stored = checksum_parts[0] if checksum_parts else ""
    check(
        computed == stored,
        f"ISC-34 — checksum mismatch (computed {computed[:16]}… vs "
        f"stored {stored[:16]}…)",
        failures,
    )

    # ISC-35: metadata completeness, identity and freshness.
    if "metadata.json" in artifact.oversize or artifact.metadata_text is None:
        failures.append("ISC-35 — metadata.json invalid: unreadable or oversized")
        return None
    try:
        meta = json.loads(artifact.metadata_text)
    except ValueError as exc:
        failures.append(f"ISC-35 — metadata.json invalid: {exc}")
        return None
    if not isinstance(meta, dict):
        failures.append("ISC-35 — metadata.json top-level value is not an object")
        return None
    for field_name in REQUIRED_META:
        check(
            str(meta.get(field_name, "")).strip() != "",
            f"ISC-35 — metadata missing/empty '{field_name}'",
            failures,
        )
    check(
        str(meta.get("wsrep_seqno", "")).isdigit(),
        f"ISC-35 — wsrep_seqno not numeric: {meta.get('wsrep_seqno')!r}",
        failures,
    )
    check(
        meta.get("cluster_name") == cluster_name,
        f"ISC-35 — cluster_name {meta.get('cluster_name')!r} != {cluster_name!r}",
        failures,
    )
    check(
        meta.get("format_version") in (1, 2, 3),
        f"ISC-35 — unsupported format_version {meta.get('format_version')!r}",
        failures,
    )
    check(
        meta.get("sha256_encrypted") == computed,
        "ISC-35 — sha256_encrypted in metadata mismatch with computed",
        failures,
    )
    # The restore path trusts `encrypted_sha256`, not `sha256_encrypted`.
    if "encrypted_sha256" in meta:
        check(
            meta.get("encrypted_sha256") == computed,
            "ISC-35 — encrypted_sha256 in metadata mismatch with computed",
            failures,
        )
    stale = freshness_failure(meta, sla_hours, now)
    if stale:
        failures.append(stale)
    return meta


def summary_line(artifact, where, meta, sla_hours, now):
    age_hours = (now - meta["created_unixtime"]) / 3600
    return (
        f"backup verified — {artifact.name} off-cluster in {where}, encrypted "
        f"({meta.get('encryption_method', 'aes-256-gcm-stream-pbkdf2-sha256')}), "
        f"sha256 OK, metadata {meta.get('mariadb_version', 'unknown')} "
        f"seqno={meta.get('wsrep_seqno', 'unknown')} "
        f"cluster={meta.get('cluster_name', 'unknown')}, "
        f"age {age_hours:.1f}h <= SLA {sla_hours}h"
    )


# === S3 =====================================================================


def s3_credentials():
    """Production reads only the explicit process environment."""
    if PRODUCTION:
        return (
            os.environ.get("GALERA_BACKUP_S3_ACCESS_KEY", ""),
            os.environ.get("GALERA_BACKUP_S3_SECRET_KEY", ""),
        )
    access = CTX.env_secret("GALERA_BACKUP_S3_ACCESS_KEY") or CTX.env_secret(
        "MINIO_ROOT_USER"
    )
    secret = CTX.env_secret("GALERA_BACKUP_S3_SECRET_KEY") or CTX.env_secret(
        "MINIO_ROOT_PASSWORD"
    )
    return access, secret


def _close_response(response):
    for method_name in ("close", "release_conn"):
        method = getattr(response, method_name, None)
        if callable(method):
            method()


def _read_limited(response, limit):
    chunks = []
    total = 0
    while total <= limit:
        chunk = response.read(min(CHUNK_BYTES, limit + 1 - total))
        if not chunk:
            return b"".join(chunks), True
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)[:limit], False


def read_s3_text(client, bucket, key):
    """(text, oversize); raises whatever the backend raised."""
    response = client.get_object(bucket, key)
    try:
        data, complete = _read_limited(response, MAX_TEXT_BYTES)
    finally:
        _close_response(response)
    if not complete:
        return None, True
    return data.decode("utf-8", "replace"), False


def stream_s3_payload(client, bucket, key):
    """(leading bytes, sha256 hex, size) of an object, never stored on disk."""
    response = client.get_object(bucket, key)
    digest = hashlib.sha256()
    size = 0
    magic = b""
    try:
        while True:
            chunk = response.read(CHUNK_BYTES)
            if not chunk:
                break
            if len(magic) < 8:
                magic += chunk[: 8 - len(magic)]
            digest.update(chunk)
            size += len(chunk)
    finally:
        _close_response(response)
    return magic, digest.hexdigest(), size


def verify_s3(client, endpoint, bucket, cluster_name, sla_hours, now, failures, undetermined):
    """Judge the newest artifact in an S3 bucket; returns the OK summary or None."""

    def unreadable(what, exc):
        undetermined.append(
            f"S3 {endpoint} {what}: {type(exc).__name__}: {str(exc)[:160]}"
        )

    # ISC-32: backup exists in off-cluster object storage.
    try:
        bucket_exists = client.bucket_exists(bucket)
    except Exception as exc:
        unreadable("nie odpowiada", exc)
        return None
    if not bucket_exists:
        failures.append(
            f"ISC-32 — backup bucket '{bucket}' does not exist (no off-cluster backup)"
        )
        return None

    # Owner marker: absence/invalidity is a measured failure; any other
    # backend error means the state could not be read.
    owner_text, owner_oversize = None, False
    try:
        owner_text, owner_oversize = read_s3_text(client, bucket, OWNER_KEY)
    except S3Error as exc:
        if exc.code != "NoSuchKey":
            unreadable("nie odczytal znacznika wlasciciela", exc)
            return None
    except Exception as exc:
        unreadable("nie odczytal znacznika wlasciciela", exc)
        return None
    judge_owner_marker(owner_text, owner_oversize, cluster_name, failures)

    prefix = f"galera-{cluster_name}-"
    metadata_key = re.compile(
        rf"^(galera-{re.escape(cluster_name)}-[0-9]{{8}}-[0-9]{{6}})/metadata\.json$"
    )
    try:
        names = []
        for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
            match = metadata_key.match(obj.object_name)
            if match:
                names.append(match.group(1))
    except Exception as exc:
        unreadable("przerwalo odczyt obiektow", exc)
        return None
    if not names:
        failures.append(f"ISC-32 — no backups found under prefix s3://{bucket}/{prefix}")
        return None
    # metadata.json is uploaded last: it marks a finished publication.
    latest = max(names)

    try:
        keys = {
            obj.object_name
            for obj in client.list_objects(bucket, prefix=latest + "/", recursive=True)
        }
    except Exception as exc:
        unreadable("przerwalo odczyt artefaktu", exc)
        return None
    files = {name: f"{latest}/{name}" in keys for name in ARTIFACT_FILES}
    missing_file_failures(files, latest, failures)
    if failures:
        return None

    artifact = Artifact(name=latest, files=files)
    try:
        artifact.metadata_text, big = read_s3_text(client, bucket, f"{latest}/metadata.json")
        if big:
            artifact.oversize.append("metadata.json")
        artifact.checksum_text, big = read_s3_text(client, bucket, f"{latest}/backup.sha256")
        if big:
            artifact.oversize.append("backup.sha256")
        artifact.payload_magic, artifact.payload_sha256, artifact.payload_size = (
            stream_s3_payload(client, bucket, f"{latest}/backup.tar.enc")
        )
    except Exception as exc:
        unreadable("nie dostarczyl artefaktu", exc)
        return None

    meta = judge_artifact(artifact, cluster_name, sla_hours, now, failures)
    if failures or meta is None:
        return None
    return summary_line(artifact, f"s3://{bucket}", meta, sla_hours, now)


def probe_s3(backup, sla_hours, now, failures, undetermined):
    s3 = backup.get("s3") or {}
    endpoint_text = str(s3.get("endpoint", ""))
    bucket = str(s3.get("bucket", ""))
    if not endpoint_text or not bucket:
        failures.append(
            "backup.destination=s3 wymaga backup.s3.endpoint i backup.s3.bucket"
        )
        return None

    configured_endpoint = urlparse(
        endpoint_text if "://" in endpoint_text else f"//{endpoint_text}"
    )
    configured_host = configured_endpoint.hostname or ""
    probe_host = inventory_addresses.get(configured_host, configured_host)
    probe_secure = bool(s3.get("secure", configured_endpoint.scheme == "https"))
    probe_port = configured_endpoint.port or (443 if probe_secure else 80)
    if PRODUCTION and PROBE_ENDPOINT_OVERRIDE:
        failures.append(
            "S3_PROBE_ENDPOINT is a lab-only override; production verifies only "
            "the declared backup.s3.endpoint"
        )
        return None
    if PRODUCTION and not probe_secure:
        failures.append(
            "production requires backup.s3.secure=true; the probe will not "
            "talk to S3 over plaintext"
        )
        return None
    probe_endpoint = PROBE_ENDPOINT_OVERRIDE or f"{probe_host}:{probe_port}"

    access, secret = s3_credentials()
    if not access or not secret:
        if PRODUCTION:
            undetermined.append(
                "S3 credentials missing: production reads only "
                "GALERA_BACKUP_S3_ACCESS_KEY and GALERA_BACKUP_S3_SECRET_KEY "
                "from the process environment"
            )
        else:
            undetermined.append(
                "S3 credentials must be set in environment "
                "(GALERA_BACKUP_S3_ACCESS_KEY/SECRET_KEY or MINIO_ROOT_USER/PASSWORD)"
            )
        return None

    client = Minio(
        probe_endpoint,
        access_key=access,
        secret_key=secret,
        secure=probe_secure,
        http_client=urllib3.PoolManager(
            timeout=Timeout(connect=5, read=30),
            retries=False,
            cert_reqs="CERT_REQUIRED",
        ),
    )
    return verify_s3(
        client, probe_endpoint, bucket, CLUSTER_NAME, sla_hours, now, failures, undetermined
    )


# === SMB ====================================================================


def inventory_host_names():
    all_group = CTX.inventory.get("all") or {}
    names = set((all_group.get("hosts") or {}).keys())
    for group in (all_group.get("children") or {}).values():
        names.update(((group or {}).get("hosts") or {}).keys())
    return names


def reader_command(params):
    """One shell command that runs the reader from a base64 argument: no file
    is written on the host, and the payload has no Jinja-significant text."""
    source = base64.b64encode(READER_PATH.read_bytes()).decode("ascii")
    argument = base64.b64encode(
        json.dumps(params, sort_keys=True).encode("utf-8")
    ).decode("ascii")
    return (
        "/usr/bin/python3 -c 'import base64,sys;"
        'exec(compile(base64.b64decode(sys.argv[1]),"backup_artifact_reader","exec"),'
        '{"__name__":"__main__"})\' ' + source + " " + argument
    )


def parse_evidence(body):
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(artifact_reader.RESULT_PREFIX):
            try:
                data = json.loads(line[len(artifact_reader.RESULT_PREFIX):])
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


def artifact_from_record(record, cluster_name):
    name = record["name"]
    if not isinstance(name, str) or not re.fullmatch(
        rf"galera-{re.escape(cluster_name)}-[0-9]{{8}}-[0-9]{{6}}", name
    ):
        raise ValueError("name")
    raw_files = record["files"]
    if not isinstance(raw_files, dict) or set(raw_files) != set(ARTIFACT_FILES):
        raise ValueError("files")
    files = {filename: raw_files[filename] is True for filename in ARTIFACT_FILES}
    for key in ("metadata", "checksum"):
        if record.get(key) is not None and not isinstance(record[key], str):
            raise ValueError(key)
    artifact = Artifact(
        name=name,
        files=files,
        metadata_text=record.get("metadata"),
        checksum_text=record.get("checksum"),
        oversize=[str(item) for item in (record.get("oversize") or [])],
    )
    if all(files.values()):
        payload = record["payload"]
        if not isinstance(payload, dict):
            raise ValueError("payload")
        artifact.payload_magic = bytes.fromhex(payload["magic_hex"])
        artifact.payload_sha256 = str(payload["sha256"])
        artifact.payload_size = int(payload["size"])
    return artifact


def judge_smb_evidence(evidence, source, host, cluster_name, sla_hours, now, failures, undetermined):
    """Turn the host reader's evidence into failures/undetermined/OK summary."""
    status = evidence.get("status")
    if status == "config_mismatch":
        problems = evidence.get("problems")
        if not isinstance(problems, list) or not problems:
            problems = ["installed backup config differs from cluster.yml"]
        for problem in problems:
            failures.append(
                f"SMB installed backup config on {host} drifted from cluster.yml: "
                f"{str(problem)[:300]}"
            )
        return None
    if status != "ok":
        undetermined.append(
            f"SMB {source}: {str(evidence.get('reason') or 'no reason given')[:600]}"
        )
        return None
    access = evidence.get("access")
    if access not in ("mount", "smbclient"):
        undetermined.append(f"SMB {source}: unknown read route {str(access)[:40]!r}")
        return None

    owner = evidence.get("owner")
    owner = owner if isinstance(owner, dict) else {}
    owner_text = owner.get("text")
    if owner_text is not None and not isinstance(owner_text, str):
        undetermined.append(f"SMB {source}: malformed reader evidence (owner)")
        return None
    judge_owner_marker(owner_text, bool(owner.get("oversize")), cluster_name, failures)

    record = evidence.get("backup")
    if not record:
        failures.append(
            f"ISC-32 — no backups found in smb {source} under {cluster_name}/"
        )
        return None
    try:
        artifact = artifact_from_record(record, cluster_name)
    except (KeyError, TypeError, ValueError) as exc:
        undetermined.append(
            f"SMB {source}: malformed reader evidence ({type(exc).__name__}: {str(exc)[:80]})"
        )
        return None

    meta = judge_artifact(artifact, cluster_name, sla_hours, now, failures)
    if failures or meta is None:
        return None
    return summary_line(artifact, f"smb {source} (read via {access})", meta, sla_hours, now)


def probe_smb(backup, sla_hours, now, failures, undetermined):
    smb = backup.get("smb") or {}
    source = str(smb.get("source", ""))
    mount_point = str(smb.get("mount_point", ""))
    if not source or not mount_point:
        failures.append(
            "backup.destination=smb wymaga backup.smb.source i backup.smb.mount_point"
        )
        return None
    host = str((backup.get("scheduler") or {}).get("host", ""))
    if not host:
        failures.append(
            "backup.scheduler.host is required: the installed backup config lives there"
        )
        return None
    if host not in inventory_host_names():
        failures.append(f"backup.scheduler.host '{host}' is not defined in the inventory")
        return None

    params = {
        "cluster": CLUSTER_NAME,
        "install_root": BACKUP_INSTALL_ROOT,
        "declared_source": source,
        "declared_mount_point": mount_point,
        "timeout_seconds": SMB_READ_TIMEOUT_SECONDS,
    }
    result = run_ansible(
        CTX, host, reader_command(params), timeout=SMB_READ_TIMEOUT_SECONDS + 300
    )
    require_hosts(result, [host], "SMB artifact read", failures, undetermined)
    if host not in result.bodies:
        return None
    evidence = parse_evidence(result.bodies[host])
    if evidence is None:
        undetermined.append(
            f"SMB artifact read on {host}: the reader printed no evidence line"
        )
        return None
    return judge_smb_evidence(
        evidence, source, host, CLUSTER_NAME, sla_hours, now, failures, undetermined
    )


# === entry point ============================================================


def main():
    failures = []
    undetermined = []

    # Klaster moze swiadomie nie miec kopii — magazyn bywa usluga zewnetrzna,
    # ktorej lab nie stawia. Odmowa pomiaru jest tu poprawna odpowiedzia, ale
    # musi byc jawna: nie twierdzimy, ze kopia istnieje. Produkcja nie ma tej
    # furtki: wylaczona kopia produkcyjna to naruszenie, nie SKIP.
    backup = CLUSTER.get("backup") or {}
    if not backup.get("enabled", True):
        if PRODUCTION:
            failures.append(
                "production requires backup.enabled=true; a disabled backup is "
                "never reported as verified"
            )
            return finish(failures, undetermined, "")
        print(
            "SKIP: backup wylaczony w cluster.yml (backup.enabled=false) — "
            "brak kopii do zweryfikowania"
        )
        return 0

    # Decyzja o backendzie MUSI zapasc przed odczytem `backup.s3`/`backup.smb`:
    # legalna konfiguracja ma tylko blok swojego backendu. Wlaczony backup bez
    # sondy nie jest zielony — to jawny brak pomiaru.
    destination = str(backup.get("destination", "s3"))
    if destination not in ("s3", "smb"):
        undetermined.append(f"brak sondy dla destination={destination}")
        return finish(failures, undetermined, "")

    sla_hours = freshness_sla(backup)
    if sla_hours is None:
        failures.append(
            "backup.freshness_sla_hours must be a positive integer to judge "
            f"freshness, got {backup.get('freshness_sla_hours')!r}"
        )
        return finish(failures, undetermined, "")

    now = time.time()
    if destination == "s3":
        summary = probe_s3(backup, sla_hours, now, failures, undetermined)
    else:
        summary = probe_smb(backup, sla_hours, now, failures, undetermined)
    if summary is None and not failures and not undetermined:
        undetermined.append("sonda nie zwrocila zadnego pomiaru")
    return finish(failures, undetermined, summary or "")


if __name__ == "__main__":
    sys.exit(main())
