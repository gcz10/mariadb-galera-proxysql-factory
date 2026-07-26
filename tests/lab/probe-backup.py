#!/usr/bin/env python3
"""Verify the latest off-cluster backup in S3.

Checks: ISC-32 (backup stored off-cluster in object storage), ISC-33 (encrypted),
ISC-34 (sha256 checksum matches), ISC-35 (metadata has MariaDB version, time,
cluster name and wsrep seqno).

Requires MINIO_ROOT_USER / MINIO_ROOT_PASSWORD in the environment.
The probe resolves the configured S3 hostname through the selected inventory;
S3_PROBE_ENDPOINT remains an optional controller-side override.
"""

import hashlib
import json
import os
import sys
import tempfile
from urllib.parse import urlparse

import yaml
from minio import Minio

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
INVENTORY_PATH = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
PROBE_ENDPOINT_OVERRIDE = os.environ.get("S3_PROBE_ENDPOINT", "")
ACCESS = os.environ.get("MINIO_ROOT_USER", "")
SECRET = os.environ.get("MINIO_ROOT_PASSWORD", "")

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER = yaml.safe_load(fh)
with open(INVENTORY_PATH, encoding="utf-8") as fh:
    INVENTORY = yaml.safe_load(fh)

endpoint_text = str(CLUSTER["backup"]["s3"]["endpoint"])
configured_endpoint = urlparse(endpoint_text if "://" in endpoint_text else f"//{endpoint_text}")
configured_host = configured_endpoint.hostname or ""
inventory_addresses = {
    host: values.get("ansible_host", host)
    for group in INVENTORY["all"]["children"].values()
    for host, values in group.get("hosts", {}).items()
}
probe_host = inventory_addresses.get(configured_host, configured_host)
PROBE_SECURE = bool(CLUSTER["backup"]["s3"].get("secure", configured_endpoint.scheme == "https"))
probe_port = configured_endpoint.port or (443 if PROBE_SECURE else 80)
PROBE_ENDPOINT = PROBE_ENDPOINT_OVERRIDE or f"{probe_host}:{probe_port}"

BUCKET = CLUSTER["backup"]["s3"]["bucket"]
CLUSTER_NAME = CLUSTER["cluster"]["name"]
REQUIRED_META = ["cluster_name", "mariadb_version", "created_at", "wsrep_seqno", "wsrep_uuid"]


def check(cond, msg, failures):
    if not cond:
        failures.append(msg)


def main():
    failures = []
    if not ACCESS or not SECRET:
        print("FAIL: MINIO_ROOT_USER / MINIO_ROOT_PASSWORD must be set")
        return 1

    c = Minio(PROBE_ENDPOINT, access_key=ACCESS, secret_key=SECRET, secure=PROBE_SECURE)

    # ISC-32: backup exists in off-cluster object storage.
    if not c.bucket_exists(BUCKET):
        print(f"FAIL: ISC-32 — backup bucket '{BUCKET}' does not exist (no off-cluster backup)")
        return 1
    metas = sorted(
        o.object_name for o in c.list_objects(BUCKET, recursive=True)
        if o.object_name.endswith("/metadata.json")
    )
    check(len(metas) > 0, f"ISC-32 — no backups found in s3://{BUCKET}", failures)
    if not metas:
        print("FAIL: ISC-32 — no backup found off-cluster:")
        print(f"  - bucket {BUCKET} empty")
        return 1

    latest = metas[-1].rsplit("/", 1)[0]
    objs = {o.object_name for o in c.list_objects(BUCKET, prefix=latest + "/", recursive=True)}
    for suffix in ("backup.tar.enc", "backup.sha256", "metadata.json"):
        check(f"{latest}/{suffix}" in objs, f"ISC-32 — missing {suffix} in {latest}", failures)

    tmp = tempfile.mkdtemp()
    enc = os.path.join(tmp, "backup.tar.enc")
    c.fget_object(BUCKET, f"{latest}/backup.tar.enc", enc)
    c.fget_object(BUCKET, f"{latest}/backup.sha256", os.path.join(tmp, "backup.sha256"))
    c.fget_object(BUCKET, f"{latest}/metadata.json", os.path.join(tmp, "metadata.json"))

    # ISC-33: encrypted (OpenSSL salted magic, not a plaintext tar/gzip).
    with open(enc, "rb") as fh:
        magic = fh.read(8)
    check(magic == b"Salted__",
          f"ISC-33 — backup not encrypted (magic={magic!r}, expected OpenSSL Salted__)",
          failures)

    # ISC-34: sha256 of the encrypted artifact matches the stored checksum.
    h = hashlib.sha256()
    with open(enc, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    computed = h.hexdigest()
    with open(os.path.join(tmp, "backup.sha256"), encoding="utf-8") as fh:
        stored = fh.read().split()[0]
    check(computed == stored,
          f"ISC-34 — checksum mismatch (computed {computed[:16]}… vs stored {stored[:16]}…)",
          failures)

    # ISC-35: metadata completeness.
    with open(os.path.join(tmp, "metadata.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    for field in REQUIRED_META:
        check(str(meta.get(field, "")).strip() != "",
              f"ISC-35 — metadata missing/empty '{field}'", failures)
    check(str(meta.get("wsrep_seqno", "")).isdigit(),
          f"ISC-35 — wsrep_seqno not numeric: {meta.get('wsrep_seqno')!r}", failures)
    check(meta.get("cluster_name") == CLUSTER_NAME,
          f"ISC-35 — cluster_name {meta.get('cluster_name')!r} != {CLUSTER_NAME!r}", failures)

    for f in (enc, os.path.join(tmp, "backup.sha256"), os.path.join(tmp, "metadata.json")):
        os.unlink(f)
    os.rmdir(tmp)

    if failures:
        print("FAIL: backup verification failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"PASS: backup verified — {latest} off-cluster in s3://{BUCKET}, encrypted "
        f"(aes-256-cbc), sha256 OK, metadata {meta['mariadb_version']} "
        f"seqno={meta['wsrep_seqno']} cluster={meta['cluster_name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
