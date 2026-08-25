#!/usr/bin/env python3
"""Verify the latest off-cluster backup in S3.

Checks: ISC-32 (backup stored off-cluster in object storage), ISC-33 (encrypted),
ISC-34 (sha256 checksum matches), ISC-35 (metadata has MariaDB version, time,
cluster name and wsrep seqno).

Uses GALERA_BACKUP_S3_ACCESS_KEY / SECRET_KEY (falling back to MINIO_ROOT_USER / PASSWORD).
Enforces bucket ownership marker, prefix filtering, format version, and metadata completeness.
S3_PROBE_ENDPOINT is a test-only override for negative protocol tests; normal runs
always derive the endpoint and secure flag from cluster.yml.
"""

import hashlib
import json
import os
import sys
import tempfile
from urllib.parse import urlparse

import urllib3
from minio import Minio
from urllib3.util import Timeout

from _probe_common import ProbeContext, finish

CTX = ProbeContext()
CLUSTER = CTX.config
PROBE_ENDPOINT_OVERRIDE = os.environ.get("S3_PROBE_ENDPOINT", "")
ACCESS = CTX.env_secret("GALERA_BACKUP_S3_ACCESS_KEY") or CTX.env_secret(
    "MINIO_ROOT_USER"
)
SECRET = CTX.env_secret("GALERA_BACKUP_S3_SECRET_KEY") or CTX.env_secret(
    "MINIO_ROOT_PASSWORD"
)

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


def check(cond, msg, failures):
    if not cond:
        failures.append(msg)


def main():
    failures = []
    undetermined = []

    # Klaster moze swiadomie nie miec kopii — magazyn bywa usluga zewnetrzna,
    # ktorej lab nie stawia. Bez tego sonda probowala rozwiazac placeholderowy
    # endpoint i konczyla bramke jako UNDETERMINED, wiec taki klaster NIGDY nie
    # mogl przejsc. Odmowa pomiaru jest tu poprawna odpowiedzia, ale musi byc
    # jawna: nie twierdzimy, ze kopia istnieje.
    backup = CTX.config.get("backup") or {}
    if not backup.get("enabled", True):
        print(
            "SKIP: backup wylaczony w cluster.yml (backup.enabled=false) — "
            "brak kopii do zweryfikowania"
        )
        return 0

    # To jest sonda S3, nie uniwersalna sonda kazdego magazynu. Decyzja o
    # backendzie MUSI zapasc przed odczytem `backup.s3`: legalna konfiguracja
    # SMB/filesystem nie ma tego bloku wcale. Wlaczony backup bez sondy nie jest
    # jednak zielony — to jawny brak pomiaru.
    destination = str(backup.get("destination", "s3"))
    if destination != "s3":
        undetermined.append(f"brak sondy dla destination={destination}")
        return finish(failures, undetermined, "")

    s3 = backup.get("s3") or {}
    endpoint_text = str(s3.get("endpoint", ""))
    bucket = str(s3.get("bucket", ""))
    if not endpoint_text or not bucket:
        failures.append(
            "backup.destination=s3 wymaga backup.s3.endpoint i backup.s3.bucket"
        )
        return finish(failures, undetermined, "")

    configured_endpoint = urlparse(
        endpoint_text if "://" in endpoint_text else f"//{endpoint_text}"
    )
    configured_host = configured_endpoint.hostname or ""
    probe_host = inventory_addresses.get(configured_host, configured_host)
    probe_secure = bool(
        s3.get("secure", configured_endpoint.scheme == "https")
    )
    probe_port = configured_endpoint.port or (443 if probe_secure else 80)
    probe_endpoint = PROBE_ENDPOINT_OVERRIDE or f"{probe_host}:{probe_port}"
    if not ACCESS or not SECRET:
        failures.append(
            "S3 credentials must be set in environment "
            "(GALERA_BACKUP_S3_ACCESS_KEY/SECRET_KEY or MINIO_ROOT_USER/PASSWORD)"
        )
        return finish(failures, undetermined, "")

    client = Minio(
        probe_endpoint,
        access_key=ACCESS,
        secret_key=SECRET,
        secure=probe_secure,
        http_client=urllib3.PoolManager(
            timeout=Timeout(connect=5, read=30),
            retries=False,
        ),
    )

    # ISC-32: backup exists in off-cluster object storage.
    try:
        bucket_exists = client.bucket_exists(bucket)
    except Exception as exc:
        undetermined.append(
            f"S3 {probe_endpoint} nie odpowiada: {type(exc).__name__}: {str(exc)[:160]}"
        )
        return finish(failures, undetermined, "")
    if not bucket_exists:
        failures.append(
            f"ISC-32 — backup bucket '{bucket}' does not exist (no off-cluster backup)"
        )
        return finish(failures, undetermined, "")

    # Check owner marker. Object absence/invalidity is a measured failure;
    # connectivity failure at the initial bucket check above is undetermined.
    try:
        data = (
            client.get_object(bucket, "galera-backup-owner.json")
            .read()
            .decode("utf-8")
        )
        owner_info = json.loads(data)
        check(
            owner_info.get("cluster_name") == CLUSTER_NAME,
            f"Owner marker cluster_name '{owner_info.get('cluster_name')}' != "
            f"'{CLUSTER_NAME}'",
            failures,
        )
        check(
            owner_info.get("format_version") == 1,
            f"Owner marker format_version {owner_info.get('format_version')} != 1",
            failures,
        )
    except Exception as exc:
        check(
            False,
            f"galera-backup-owner.json missing or invalid: {exc}",
            failures,
        )

    prefix = f"galera-{CLUSTER_NAME}-"
    try:
        metas = sorted(
            o.object_name
            for o in client.list_objects(bucket, prefix=prefix, recursive=True)
            if o.object_name.endswith("/metadata.json")
        )
    except Exception as exc:
        undetermined.append(
            f"S3 {probe_endpoint} przerwalo odczyt obiektow: "
            f"{type(exc).__name__}: {str(exc)[:160]}"
        )
        return finish(failures, undetermined, "")

    check(
        len(metas) > 0,
        f"ISC-32 — no backups found under prefix s3://{bucket}/{prefix}",
        failures,
    )
    if not metas:
        return finish(failures, undetermined, "")

    latest = metas[-1].rsplit("/", 1)[0]
    try:
        objs = {
            o.object_name
            for o in client.list_objects(
                bucket, prefix=latest + "/", recursive=True
            )
        }
    except Exception as exc:
        undetermined.append(
            f"S3 {probe_endpoint} przerwalo odczyt artefaktu: "
            f"{type(exc).__name__}: {str(exc)[:160]}"
        )
        return finish(failures, undetermined, "")
    for suffix in ("backup.tar.enc", "backup.sha256", "metadata.json"):
        check(
            f"{latest}/{suffix}" in objs,
            f"ISC-32 — missing {suffix} in {latest}",
            failures,
        )
    if failures:
        return finish(failures, undetermined, "")

    with tempfile.TemporaryDirectory() as tmp:
        enc = os.path.join(tmp, "backup.tar.enc")
        checksum_path = os.path.join(tmp, "backup.sha256")
        metadata_path = os.path.join(tmp, "metadata.json")
        try:
            client.fget_object(bucket, f"{latest}/backup.tar.enc", enc)
            client.fget_object(bucket, f"{latest}/backup.sha256", checksum_path)
            client.fget_object(bucket, f"{latest}/metadata.json", metadata_path)
        except Exception as exc:
            undetermined.append(
                f"S3 {probe_endpoint} nie dostarczyl artefaktu: "
                f"{type(exc).__name__}: {str(exc)[:160]}"
            )
            return finish(failures, undetermined, "")

        # ISC-33: encrypted (OpenSSL salted magic, not a plaintext tar/gzip).
        with open(enc, "rb") as fh:
            magic = fh.read(8)
        check(
            magic == b"Salted__",
            f"ISC-33 — backup not encrypted (magic={magic!r}, "
            f"expected OpenSSL Salted__)",
            failures,
        )

        # ISC-34: sha256 of the encrypted artifact matches the stored checksum.
        h = hashlib.sha256()
        with open(enc, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        computed = h.hexdigest()
        try:
            with open(checksum_path, encoding="utf-8") as fh:
                checksum_parts = fh.read().split()
        except OSError as exc:
            failures.append(f"ISC-34 — backup.sha256 invalid: {exc}")
            checksum_parts = []
        stored = checksum_parts[0] if checksum_parts else ""
        check(
            computed == stored,
            f"ISC-34 — checksum mismatch (computed {computed[:16]}… vs "
            f"stored {stored[:16]}…)",
            failures,
        )

        # ISC-35: metadata completeness.
        try:
            with open(metadata_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"ISC-35 — metadata.json invalid: {exc}")
            return finish(failures, undetermined, "")
        if not isinstance(meta, dict):
            failures.append("ISC-35 — metadata.json top-level value is not an object")
            return finish(failures, undetermined, "")
        for field in REQUIRED_META:
            check(
                str(meta.get(field, "")).strip() != "",
                f"ISC-35 — metadata missing/empty '{field}'",
                failures,
            )
        check(
            str(meta.get("wsrep_seqno", "")).isdigit(),
            f"ISC-35 — wsrep_seqno not numeric: {meta.get('wsrep_seqno')!r}",
            failures,
        )
        check(
            meta.get("cluster_name") == CLUSTER_NAME,
            f"ISC-35 — cluster_name {meta.get('cluster_name')!r} != "
            f"{CLUSTER_NAME!r}",
            failures,
        )
        check(
            meta.get("format_version") == 1,
            f"ISC-35 — format_version {meta.get('format_version')!r} != 1",
            failures,
        )
        check(
            meta.get("sha256_encrypted") == computed,
            "ISC-35 — sha256_encrypted in metadata mismatch with computed",
            failures,
        )

        return finish(
            failures,
            undetermined,
            f"backup verified — {latest} off-cluster in s3://{bucket}, encrypted "
            f"({meta.get('encryption', 'aes-256-cbc')}), sha256 OK, metadata "
            f"{meta.get('mariadb_version', 'unknown')} "
            f"seqno={meta.get('wsrep_seqno', 'unknown')} "
            f"cluster={meta.get('cluster_name', 'unknown')}",
        )


if __name__ == "__main__":
    sys.exit(main())
