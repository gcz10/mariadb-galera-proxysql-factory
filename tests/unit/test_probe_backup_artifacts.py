"""Behaviour of the backup probe: freshness, identity, integrity, backend errors.

The probe judges the newest published artifact of an S3 or SMB backup without
writing to the storage. These tests drive that judgement with real artifact
bytes (in-memory object store for S3, a real directory tree for SMB) and check
the verdict class (PASS summary / FAIL / UNDETERMINED), not how the probe
forwards calls. No network, no live storage, no host is contacted.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import secrets
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO = Path(__file__).resolve().parents[2]
LAB = REPO / "tests" / "lab"
PACKAGE_ROOT = REPO / "roles" / "galera_backup" / "files"
for _path in (str(LAB), str(PACKAGE_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import _backup_artifact_reader as reader  # noqa: E402
import _probe_common  # noqa: E402
from minio.error import S3Error  # noqa: E402

CLUSTER = "c1"
SOURCE = "//nas.example/backups"
NOW = 1_800_000_000
HOUR = 3600
DROP = object()


def load_probe(cluster, inventory=None):
    inventory = inventory or {
        "all": {"children": {"galera": {"hosts": {"db1": {"ansible_host": "10.0.0.11"}}}}}
    }
    with tempfile.TemporaryDirectory() as td:
        config = Path(td) / "cluster.yml"
        inv = Path(td) / "inventory.yml"
        config.write_text(yaml.safe_dump(cluster), encoding="utf-8")
        inv.write_text(yaml.safe_dump(inventory), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            "probe_backup_under_test", LAB / "probe-backup.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        env = {
            "CLUSTER": cluster["cluster"]["name"],
            "CLUSTER_CONFIG": str(config),
            "CLUSTER_INVENTORY": str(inv),
            "S3_PROBE_ENDPOINT": "",
        }
        try:
            with mock.patch.dict(os.environ, env):
                spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
    return module


def lab_cluster():
    return {
        "cluster": {"name": CLUSTER, "environment": "laboratory"},
        "backup": {"enabled": True, "destination": "s3", "freshness_sla_hours": 26},
    }


def production_cluster(**backup):
    config = {
        "cluster": {"name": CLUSTER, "environment": "production"},
        "backup": {
            "enabled": True,
            "destination": "s3",
            "freshness_sla_hours": 26,
            "s3": {"endpoint": "s3.example.test:443", "bucket": "prod-backups", "secure": True},
        },
    }
    config["backup"].update(backup)
    return config


def build_artifact(
    cluster=CLUSTER,
    stamp="20260930-020000",
    created=NOW - HOUR,
    magic=b"GB3G",
    meta=None,
    raw_metadata=None,
    tamper=False,
):
    """Files of one published backup, consistent unless a defect is requested."""
    payload = magic + b"\x01" * 24 + b"encrypted-body"
    digest = hashlib.sha256(payload).hexdigest()
    metadata = {
        "format_version": 3,
        "cluster_name": cluster,
        "backup_name": f"galera-{cluster}-{stamp}",
        "created_at": "2026-09-30T02:00:00+00:00",
        "created_unixtime": created,
        "mariadb_version": "11.8.9",
        "wsrep_uuid": "5b7c0e1a-0000-0000-0000-000000000001",
        "wsrep_seqno": "42",
        "sha256_plaintext": "a" * 64,
        "sha256_encrypted": digest,
        "encrypted_sha256": digest,
        "size_bytes": len(payload),
        "encrypted_size_bytes": len(payload),
        "encryption_method": "aes-256-gcm-stream-pbkdf2-sha256",
        "backend": "s3",
    }
    for key, value in (meta or {}).items():
        if value is DROP:
            metadata.pop(key, None)
        else:
            metadata[key] = value
    if tamper:
        payload = payload[:-1] + bytes([payload[-1] ^ 0xFF])
    return {
        "backup.tar.enc": payload,
        "backup.sha256": digest.encode() + b"  backup.tar.enc\n",
        "metadata.json": raw_metadata if raw_metadata is not None else json.dumps(metadata).encode(),
    }


def owner_marker(cluster=CLUSTER, version=1):
    return json.dumps({"cluster_name": cluster, "format_version": version}).encode()


# === S3 =====================================================================


class FakeResponse:
    def __init__(self, data, error=None):
        self._data = data
        self._offset = 0
        self._error = error

    def read(self, amount=None):
        if self._error:
            raise self._error
        if amount is None:
            amount = len(self._data) - self._offset
        chunk = self._data[self._offset:self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self):
        pass

    def release_conn(self):
        pass


class FakeS3:
    """In-memory bucket with the slice of the Minio client API the probe reads."""

    def __init__(self, objects=None, exists=True, fail=None):
        self.objects = dict(objects or {})
        self.exists = exists
        self.fail = fail or {}

    def put(self, cluster, stamp, files):
        for name, data in files.items():
            self.objects[f"galera-{cluster}-{stamp}/{name}"] = data

    def bucket_exists(self, bucket):
        if ("bucket", None) in self.fail:
            raise self.fail[("bucket", None)]
        return self.exists

    def list_objects(self, bucket, prefix="", recursive=False):
        if ("list", prefix) in self.fail:
            raise self.fail[("list", prefix)]
        return [
            types.SimpleNamespace(object_name=key)
            for key in sorted(self.objects)
            if key.startswith(prefix)
        ]

    def get_object(self, bucket, key):
        if ("get", key) in self.fail:
            raise self.fail[("get", key)]
        if key not in self.objects:
            raise S3Error(response=None, code="NoSuchKey", message="not found",
                          resource=key, request_id="req", host_id="host")
        return FakeResponse(self.objects[key], self.fail.get(("read", key)))


def bucket_with(*artifacts, owner=True, cluster=CLUSTER):
    store = FakeS3()
    if owner:
        store.objects["galera-backup-owner.json"] = owner_marker(cluster)
    for stamp, files in artifacts:
        store.put(cluster, stamp, files)
    return store


class S3ArtifactVerdictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = load_probe(lab_cluster())

    def verdict(self, store, sla=26, now=NOW):
        failures, undetermined = [], []
        summary = self.probe.verify_s3(
            store, "s3.example.test:443", "b", CLUSTER, sla, now, failures, undetermined
        )
        return summary, failures, undetermined

    def assertFails(self, store, fragment, **kw):
        summary, failures, undetermined = self.verdict(store, **kw)
        self.assertIsNone(summary)
        self.assertTrue(
            any(fragment in item for item in failures), f"{fragment!r} not in {failures}"
        )
        self.assertEqual(undetermined, [])

    def assertUndetermined(self, store, fragment):
        summary, failures, undetermined = self.verdict(store)
        self.assertIsNone(summary)
        self.assertEqual(failures, [])
        self.assertTrue(
            any(fragment in item for item in undetermined),
            f"{fragment!r} not in {undetermined}",
        )

    # --- fresh, complete, own ------------------------------------------------

    def test_fresh_complete_artifact_validates(self):
        summary, failures, undetermined = self.verdict(
            bucket_with(("20260930-020000", build_artifact()))
        )
        self.assertEqual((failures, undetermined), ([], []))
        self.assertIn("galera-c1-20260930-020000", summary)
        self.assertIn("age 1.0h <= SLA 26h", summary)

    def test_freshness_sla_boundary_is_inclusive(self):
        at_limit = build_artifact(created=NOW - 26 * HOUR)
        summary, failures, _ = self.verdict(bucket_with(("20260930-020000", at_limit)))
        self.assertEqual(failures, [])
        self.assertIsNotNone(summary)
        past_limit = build_artifact(created=NOW - 26 * HOUR - 1)
        self.assertFails(bucket_with(("20260930-020000", past_limit)), "stale")

    def test_clock_skew_tolerance_is_bounded(self):
        slightly_ahead = build_artifact(created=NOW + 299)
        summary, failures, _ = self.verdict(bucket_with(("20260930-020000", slightly_ahead)))
        self.assertEqual(failures, [])
        self.assertIsNotNone(summary)
        far_ahead = build_artifact(created=NOW + 301)
        self.assertFails(bucket_with(("20260930-020000", far_ahead)), "in the future")

    def test_unusable_created_unixtime_fails(self):
        for value in (DROP, None, "1799996400", True, -5, 1799996400.5):
            with self.subTest(value=value):
                artifact = build_artifact(meta={"created_unixtime": value})
                self.assertFails(
                    bucket_with(("20260930-020000", artifact)), "created_unixtime"
                )

    def test_freshness_sla_comes_from_the_declaration(self):
        artifact = build_artifact(created=NOW - 10 * HOUR)
        self.assertFails(bucket_with(("20260930-020000", artifact)), "stale", sla=8)
        summary, failures, _ = self.verdict(bucket_with(("20260930-020000", artifact)), sla=12)
        self.assertEqual(failures, [])
        self.assertIn("SLA 12h", summary)

    # --- which artifact is judged ---------------------------------------------

    def test_newest_artifact_decides_and_older_stale_ones_do_not_matter(self):
        old = build_artifact(stamp="20260928-020000", created=NOW - 50 * HOUR)
        new = build_artifact(stamp="20260930-020000", created=NOW - HOUR)
        summary, failures, _ = self.verdict(
            bucket_with(("20260928-020000", old), ("20260930-020000", new))
        )
        self.assertEqual(failures, [])
        self.assertIn("galera-c1-20260930-020000", summary)

    def test_broken_newest_artifact_is_not_masked_by_an_older_good_one(self):
        old = build_artifact(stamp="20260929-020000", created=NOW - 2 * HOUR)
        new = build_artifact(stamp="20260930-020000", created=NOW - HOUR)
        del new["backup.tar.enc"]
        self.assertFails(
            bucket_with(("20260929-020000", old), ("20260930-020000", new)),
            "missing backup.tar.enc",
        )

    def test_unfinished_upload_without_metadata_is_ignored(self):
        old = build_artifact(stamp="20260929-020000", created=NOW - 2 * HOUR)
        in_flight = build_artifact(stamp="20260930-020000")
        del in_flight["metadata.json"]
        summary, failures, _ = self.verdict(
            bucket_with(("20260929-020000", old), ("20260930-020000", in_flight))
        )
        self.assertEqual(failures, [])
        self.assertIn("galera-c1-20260929-020000", summary)

    def test_artifact_of_a_cluster_with_an_overlapping_name_is_not_ours(self):
        store = bucket_with(("20260929-020000", build_artifact(stamp="20260929-020000")))
        store.put("c1-x", "20260930-030000", build_artifact(cluster="c1-x", stamp="20260930-030000"))
        summary, failures, _ = self.verdict(store)
        self.assertEqual(failures, [])
        self.assertIn("galera-c1-20260929-020000", summary)

    def test_no_backups_fails(self):
        self.assertFails(bucket_with(), "no backups found")

    def test_missing_bucket_fails(self):
        store = bucket_with()
        store.exists = False
        self.assertFails(store, "does not exist")

    # --- identity ---------------------------------------------------------------

    def test_foreign_cluster_metadata_fails(self):
        artifact = build_artifact(meta={"cluster_name": "other-cluster"})
        self.assertFails(bucket_with(("20260930-020000", artifact)), "cluster_name")

    def test_foreign_owner_marker_fails(self):
        store = bucket_with(("20260930-020000", build_artifact()))
        store.objects["galera-backup-owner.json"] = owner_marker("other-cluster")
        self.assertFails(store, "Owner marker cluster_name")

    def test_missing_or_invalid_owner_marker_fails(self):
        for marker in (None, b"{not json", b"[]", owner_marker(version=2)):
            with self.subTest(marker=marker):
                store = bucket_with(("20260930-020000", build_artifact()), owner=False)
                if marker is not None:
                    store.objects["galera-backup-owner.json"] = marker
                summary, failures, undetermined = self.verdict(store)
                self.assertIsNone(summary)
                self.assertTrue(failures)
                self.assertEqual(undetermined, [])

    # --- completeness and integrity ---------------------------------------------

    def test_incomplete_artifact_fails(self):
        for missing in ("backup.tar.enc", "backup.sha256"):
            with self.subTest(missing=missing):
                artifact = build_artifact()
                del artifact[missing]
                self.assertFails(
                    bucket_with(("20260930-020000", artifact)), f"missing {missing}"
                )

    def test_tampered_payload_fails(self):
        artifact = build_artifact(tamper=True)
        summary, failures, _ = self.verdict(bucket_with(("20260930-020000", artifact)))
        self.assertIsNone(summary)
        self.assertTrue(any("checksum mismatch" in item for item in failures))
        self.assertTrue(any("sha256_encrypted" in item for item in failures))

    def test_unencrypted_payload_fails(self):
        artifact = build_artifact(magic=b"\x1f\x8b\x08\x00")
        self.assertFails(bucket_with(("20260930-020000", artifact)), "not encrypted")

    def test_metadata_digest_the_restore_path_trusts_must_match(self):
        artifact = build_artifact(meta={"encrypted_sha256": "0" * 64})
        self.assertFails(bucket_with(("20260930-020000", artifact)), "encrypted_sha256")

    def test_invalid_metadata_fails(self):
        cases = {
            "not json": {"raw_metadata": b"{not json"},
            "not an object": {"raw_metadata": b"[]"},
            "missing field": {"meta": {"wsrep_uuid": DROP}},
            "bad format": {"meta": {"format_version": 9}},
            "bad seqno": {"meta": {"wsrep_seqno": "abc"}},
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                artifact = build_artifact(**kwargs)
                summary, failures, undetermined = self.verdict(
                    bucket_with(("20260930-020000", artifact))
                )
                self.assertIsNone(summary)
                self.assertTrue(failures)
                self.assertEqual(undetermined, [])

    # --- unreadable backend state is not a verdict -------------------------------

    def test_unreachable_endpoint_is_undetermined(self):
        store = bucket_with(("20260930-020000", build_artifact()))
        store.fail[("bucket", None)] = ConnectionError("connection refused")
        self.assertUndetermined(store, "nie odpowiada")

    def test_unreadable_owner_marker_is_undetermined_not_missing(self):
        store = bucket_with(("20260930-020000", build_artifact()))
        store.fail[("get", "galera-backup-owner.json")] = S3Error(
            response=None, code="AccessDenied", message="denied",
            resource="r", request_id="req", host_id="host",
        )
        self.assertUndetermined(store, "znacznika wlasciciela")

    def test_listing_errors_are_undetermined(self):
        for prefix, fragment in (
            ("galera-c1-", "przerwalo odczyt obiektow"),
            ("galera-c1-20260930-020000/", "przerwalo odczyt artefaktu"),
        ):
            with self.subTest(prefix=prefix):
                store = bucket_with(("20260930-020000", build_artifact()))
                store.fail[("list", prefix)] = TimeoutError("timed out")
                self.assertUndetermined(store, fragment)

    def test_payload_transfer_errors_are_undetermined(self):
        key = "galera-c1-20260930-020000/backup.tar.enc"
        for kind in ("get", "read"):
            with self.subTest(kind=kind):
                store = bucket_with(("20260930-020000", build_artifact()))
                store.fail[(kind, key)] = ConnectionError("reset by peer")
                self.assertUndetermined(store, "nie dostarczyl artefaktu")


# === production credentials and refusals ====================================


class ProductionCredentialTests(unittest.TestCase):
    def credentials(self, probe, env, env_file_text):
        with tempfile.TemporaryDirectory() as td:
            lab_dir = Path(td) / "tests" / "lab"
            lab_dir.mkdir(parents=True)
            (lab_dir / ".env").write_text(env_file_text, encoding="utf-8")
            with mock.patch.object(_probe_common, "REPO_ROOT", Path(td)), \
                    mock.patch.dict(os.environ, env, clear=True):
                return probe.s3_credentials()

    LAB_ENV_FILE = (
        "GALERA_BACKUP_S3_ACCESS_KEY=file-access\n"
        "GALERA_BACKUP_S3_SECRET_KEY=file-secret\n"
        "MINIO_ROOT_USER=file-root\n"
        "MINIO_ROOT_PASSWORD=file-root-pw\n"
    )

    def test_production_uses_only_the_explicit_process_environment(self):
        probe = load_probe(production_cluster())
        root_only = {"MINIO_ROOT_USER": "root", "MINIO_ROOT_PASSWORD": "root-pw"}
        self.assertEqual(self.credentials(probe, root_only, self.LAB_ENV_FILE), ("", ""))
        explicit = {
            "GALERA_BACKUP_S3_ACCESS_KEY": "explicit-access",
            "GALERA_BACKUP_S3_SECRET_KEY": "explicit-secret",
            **root_only,
        }
        self.assertEqual(
            self.credentials(probe, explicit, self.LAB_ENV_FILE),
            ("explicit-access", "explicit-secret"),
        )

    def test_lab_keeps_the_env_file_and_minio_root_fallbacks(self):
        probe = load_probe(lab_cluster())
        self.assertEqual(
            self.credentials(probe, {}, self.LAB_ENV_FILE), ("file-access", "file-secret")
        )
        root_only = {"MINIO_ROOT_USER": "root", "MINIO_ROOT_PASSWORD": "root-pw"}
        self.assertEqual(self.credentials(probe, root_only, ""), ("root", "root-pw"))


class ProductionProbeProcessTests(unittest.TestCase):
    """Whole-probe runs that stop before any network access."""

    @staticmethod
    def run_probe(cluster, env_extra=None):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "cluster.yml"
            inventory = Path(td) / "inventory.yml"
            config.write_text(yaml.safe_dump(cluster), encoding="utf-8")
            inventory.write_text(yaml.safe_dump({"all": {"children": {}}}), encoding="utf-8")
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GALERA_BACKUP_S3_")
                and key not in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "S3_PROBE_ENDPOINT")
            }
            env.update(
                {
                    "CLUSTER": cluster["cluster"]["name"],
                    "CLUSTER_CONFIG": str(config),
                    "CLUSTER_INVENTORY": str(inventory),
                }
            )
            env.update(env_extra or {})
            result = subprocess.run(
                [sys.executable, str(LAB / "probe-backup.py")],
                cwd=REPO,
                capture_output=True,
                text=True,
                env=env,
            )
        return result, result.stdout + result.stderr

    def test_lab_root_keys_do_not_stand_in_for_production_credentials(self):
        result, output = self.run_probe(
            production_cluster(),
            {"MINIO_ROOT_USER": "root", "MINIO_ROOT_PASSWORD": "root-password"},
        )
        self.assertEqual(result.returncode, 2, output)
        self.assertIn("UNDETERMINED", output)
        self.assertIn("GALERA_BACKUP_S3_ACCESS_KEY", output)
        self.assertNotIn("root-password", output)
        self.assertNotIn("Traceback", output)

    def test_production_refuses_the_lab_endpoint_override(self):
        result, output = self.run_probe(
            production_cluster(),
            {
                "S3_PROBE_ENDPOINT": "127.0.0.1:9",
                "GALERA_BACKUP_S3_ACCESS_KEY": "access",
                "GALERA_BACKUP_S3_SECRET_KEY": "secret-value",
            },
        )
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("lab-only override", output)
        self.assertNotIn("secret-value", output)

    def test_production_does_not_talk_to_plaintext_s3(self):
        cluster = production_cluster()
        cluster["backup"]["s3"]["secure"] = False
        result, output = self.run_probe(
            cluster,
            {
                "GALERA_BACKUP_S3_ACCESS_KEY": "access",
                "GALERA_BACKUP_S3_SECRET_KEY": "secret-value",
            },
        )
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("secure=true", output)

    def test_production_never_skips_a_disabled_backup(self):
        result, output = self.run_probe(production_cluster(enabled=False))
        self.assertEqual(result.returncode, 1, output)
        self.assertNotIn("SKIP", output)
        self.assertIn("backup.enabled=true", output)

    def test_invalid_freshness_declaration_is_a_failure(self):
        cluster = lab_cluster()
        cluster["backup"]["freshness_sla_hours"] = 0
        result, output = self.run_probe(cluster)
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("freshness_sla_hours", output)


# === SMB ====================================================================


def write_artifact(directory, files):
    directory.mkdir(parents=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)


SMBCLIENT_SHIM = r'''#!@PYTHON@
import fnmatch, json, os, sys

here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "shim.json")))
argv = sys.argv[1:]
if any(cfg["password"] in arg for arg in argv):
    sys.stderr.write("shim: password found in argv\n")
    sys.exit(97)
need = ["--client-protection=encrypt", "--option=client min protocol=SMB3_11"]
if any(item not in argv for item in need) or argv[0] != cfg["source"]:
    sys.stderr.write("shim: unexpected arguments\n")
    sys.exit(98)
user = argv[argv.index("-U") + 1]
command = argv[argv.index("-c") + 1]
if cfg.get("domain") and argv[argv.index("-W") + 1] != cfg["domain"]:
    sys.stderr.write("shim: wrong domain\n")
    sys.exit(98)
if os.environ.get("PASSWD") != cfg["password"] or user != cfg["user"]:
    sys.stderr.write("session setup failed: NT_STATUS_LOGON_FAILURE user=%s password=%s\n"
                     % (user, os.environ.get("PASSWD")))
    sys.exit(1)
if cfg.get("refuse"):
    sys.stderr.write("Connection failed: NT_STATUS_CONNECTION_REFUSED\n")
    sys.exit(1)
root = cfg["root"]
verb, _, path = command.partition(" ")
if verb == "ls":
    listing_stream = sys.stderr if "-E" in argv else sys.stdout
    directory, _, mask = path.rpartition("/")
    target = os.path.join(root, directory)
    names = sorted(fnmatch.filter(os.listdir(target), mask)) if os.path.isdir(target) else []
    if not names:
        sys.stderr.write("NT_STATUS_NO_SUCH_FILE listing %s\n" % path)
        sys.exit(1)
    if mask == "*":
        names = [".", ".."] + names
    for name in names:
        full = os.path.join(target, name)
        is_dir = os.path.isdir(full)
        size = 0 if is_dir else os.path.getsize(full)
        listing_stream.write("  %-30s%7s %8d  %s\n" % (name, "D" if is_dir else "N", size,
                                                      "Wed Sep 30 02:00:12 2026"))
    listing_stream.write("\n\t\t1000 blocks of size 1024. 500 blocks available\n")
elif verb == "get" and path.endswith(" -"):
    full = os.path.join(root, path[:-2])
    if not os.path.isfile(full):
        sys.stderr.write("NT_STATUS_OBJECT_NAME_NOT_FOUND opening remote file %s\n" % path)
        sys.exit(1)
    with open(full, "rb") as handle:
        sys.stdout.buffer.write(handle.read())
    if "-E" not in argv:
        sys.stdout.buffer.write(b"getting remote file\n")
else:
    sys.stderr.write("shim: unsupported command %r\n" % command)
    sys.exit(2)
'''


class SmbFixture(unittest.TestCase):
    PASSWORD = secrets.token_urlsafe(24)
    USERNAME = "svc-backup"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.install_root = root / "opt-galera-backup"
        self.mount_point = root / "mnt"
        self.share_root = root / "share"
        (self.mount_point / CLUSTER).mkdir(parents=True)
        (self.share_root / CLUSTER).mkdir(parents=True)
        cluster_dir = self.install_root / "clusters" / CLUSTER
        cluster_dir.mkdir(parents=True)
        self.write_config()
        self.mountinfo = root / "mountinfo"
        self.write_mountinfo()
        self.probe = load_probe(
            {
                "cluster": {"name": CLUSTER, "environment": "production"},
                "backup": {"enabled": True, "destination": "smb", "freshness_sla_hours": 26},
            }
        )
        self.old_path = list(sys.path)
        self.addCleanup(self.restore_path)

    def restore_path(self):
        sys.path[:] = self.old_path

    def write_config(self, source=SOURCE):
        (self.install_root / "clusters" / CLUSTER / "config.json").write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "cluster_name": CLUSTER,
                    "backend": {
                        "type": "smb",
                        "source": source,
                        "mount_point": str(self.mount_point),
                        "options": ["vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
                    },
                }
            ),
            encoding="utf-8",
        )

    def write_mountinfo(self, fstype="cifs", source=SOURCE, super_options=None, mount_options=None):
        real = os.path.realpath(self.mount_point)
        super_options = super_options or "rw,vers=3.1.1,seal,cache=strict,username=svc"
        mount_options = mount_options or "rw,nosuid,nodev,noexec,relatime"
        self.mountinfo.write_text(
            f"36 35 0:45 / {real} {mount_options} shared:1 - {fstype} {source} {super_options}\n",
            encoding="utf-8",
        )

    def params(self):
        return {
            "cluster": CLUSTER,
            "install_root": str(self.install_root),
            "declared_source": SOURCE,
            "declared_mount_point": str(self.mount_point),
            "timeout_seconds": 60,
        }

    def judge(self, evidence, sla=26, now=NOW):
        failures, undetermined = [], []
        summary = self.probe.judge_smb_evidence(
            json.loads(json.dumps(evidence)), SOURCE, "db1", CLUSTER, sla, now,
            failures, undetermined,
        )
        return summary, failures, undetermined

    def collect(self):
        try:
            return reader.collect(self.params(), mountinfo_path=str(self.mountinfo))
        except reader.Unavailable as exc:
            return {"status": "unavailable", "reason": str(exc)}

    def verdict(self, sla=26, now=NOW):
        return self.judge(self.collect(), sla=sla, now=now)

    def assertFails(self, fragment, **kw):
        summary, failures, undetermined = self.verdict(**kw)
        self.assertIsNone(summary)
        self.assertTrue(
            any(fragment in item for item in failures), f"{fragment!r} not in {failures}"
        )
        self.assertEqual(undetermined, [])

    def assertUndetermined(self, fragment, evidence=None):
        summary, failures, undetermined = (
            self.verdict() if evidence is None else self.judge(evidence)
        )
        self.assertIsNone(summary)
        self.assertEqual(failures, [])
        self.assertTrue(
            any(fragment in item for item in undetermined),
            f"{fragment!r} not in {undetermined}",
        )


class SmbMountedShareTests(SmbFixture):
    """Route 1: the share is already mounted as the runner requires."""

    def publish(self, stamp="20260930-020000", files=None, owner=True, **kw):
        cluster_dir = self.mount_point / CLUSTER
        if owner and not (cluster_dir / "galera-backup-owner.json").exists():
            (cluster_dir / "galera-backup-owner.json").write_bytes(owner_marker())
        write_artifact(
            cluster_dir / f"galera-{CLUSTER}-{stamp}",
            files if files is not None else build_artifact(stamp=stamp, **kw),
        )

    def test_fresh_complete_artifact_validates_without_writing(self):
        self.publish()
        before = sorted(str(p) for p in self.mount_point.rglob("*"))
        summary, failures, undetermined = self.verdict()
        self.assertEqual((failures, undetermined), ([], []))
        self.assertIn("galera-c1-20260930-020000", summary)
        self.assertIn("read via mount", summary)
        self.assertEqual(before, sorted(str(p) for p in self.mount_point.rglob("*")))

    def test_stale_artifact_fails(self):
        self.publish(created=NOW - 27 * HOUR)
        self.assertFails("stale")

    def test_foreign_cluster_artifact_or_owner_fails(self):
        self.publish(meta={"cluster_name": "other-cluster"})
        self.assertFails("cluster_name")

    def test_foreign_owner_marker_fails(self):
        self.publish()
        (self.mount_point / CLUSTER / "galera-backup-owner.json").write_bytes(
            owner_marker("other-cluster")
        )
        self.assertFails("Owner marker cluster_name")

    def test_incomplete_artifact_fails(self):
        files = build_artifact()
        del files["backup.sha256"]
        self.publish(files=files)
        self.assertFails("missing backup.sha256")

    def test_symlinked_payload_counts_as_missing(self):
        self.publish()
        payload = self.mount_point / CLUSTER / "galera-c1-20260930-020000" / "backup.tar.enc"
        target = Path(self.tmp.name) / "elsewhere.bin"
        target.write_bytes(payload.read_bytes())
        payload.unlink()
        payload.symlink_to(target)
        self.assertFails("missing backup.tar.enc")

    def test_tampered_payload_fails(self):
        self.publish(tamper=True)
        self.assertFails("checksum mismatch")

    def test_unencrypted_payload_fails(self):
        self.publish(magic=b"\x1f\x8b\x08\x00")
        self.assertFails("not encrypted")

    def test_oversized_metadata_fails_instead_of_being_read_whole(self):
        self.publish(raw_metadata=b"{" + b" " * (reader.MAX_TEXT_BYTES + 10) + b"}")
        self.assertFails("metadata.json invalid")

    def test_no_backups_on_a_mounted_share_fails(self):
        (self.mount_point / CLUSTER / "galera-backup-owner.json").write_bytes(owner_marker())
        self.assertFails("no backups found")

    def test_names_that_are_not_backups_and_unfinished_uploads_are_ignored(self):
        self.publish(stamp="20260929-020000", created=NOW - 2 * HOUR)
        unfinished = build_artifact(stamp="20260930-020000")
        del unfinished["metadata.json"]
        self.publish(stamp="20260930-020000", files=unfinished)
        (self.mount_point / CLUSTER / "galera-c1-notes").mkdir()
        (self.mount_point / CLUSTER / "galera-c1-x-20260930-030000").mkdir()
        summary, failures, _ = self.verdict()
        self.assertEqual(failures, [])
        self.assertIn("galera-c1-20260929-020000", summary)

    def test_installed_config_drift_fails(self):
        self.publish()
        self.write_config(source="//other-server/backups")
        self.assertFails("drifted from cluster.yml")

    def test_missing_installed_config_is_undetermined(self):
        (self.install_root / "clusters" / CLUSTER / "config.json").unlink()
        self.assertUndetermined("unreadable")

    def test_share_that_is_not_mounted_is_never_read_as_empty(self):
        self.publish()
        self.mountinfo.write_text("", encoding="utf-8")
        self.assertUndetermined("the share is not mounted")

    def test_mount_that_does_not_match_the_declaration_is_not_trusted(self):
        self.publish()
        cases = {
            "the wrong filesystem": ({"fstype": "ext4"}, "mounted as ext4, not cifs"),
            "a different source": (
                {"source": "//intruder.example/backups"},
                "mounted from a different source",
            ),
            "options the runner requires": (
                {"super_options": "rw,vers=3.0,cache=strict"},
                "lacks required mount options",
            ),
        }
        for label, (kwargs, fragment) in cases.items():
            with self.subTest(case=label):
                self.write_mountinfo(**kwargs)
                self.assertUndetermined(fragment)

    def test_mount_source_comparison_matches_the_runner(self):
        from galera_backup.textutil import normalize_smb_source, validate_smb_options

        for sample in ("//NAS.example/Backups", "//nas.example/backups/", "\\\\nas\\share"):
            self.assertEqual(
                reader.normalize_smb_source(sample), normalize_smb_source(sample), sample
            )
        self.assertEqual(validate_smb_options(sorted(reader.REQUIRED_MOUNT_OPTIONS)), [])


class SmbClientRouteTests(SmbFixture):
    """Route 2: no mount at all; smbclient streams the files."""

    def setUp(self):
        super().setUp()
        self.mountinfo.write_text("", encoding="utf-8")
        (self.install_root / "clusters" / CLUSTER / "secrets.env").write_text(
            'GALERA_BACKUP_ENCRYPTION_KEY="enc-key"\n'
            f'GALERA_BACKUP_SMB_USERNAME="{self.USERNAME}"\n'
            f'GALERA_BACKUP_SMB_PASSWORD="{self.PASSWORD}"\n'
            'GALERA_BACKUP_SMB_DOMAIN="CORP"\n',
            encoding="utf-8",
        )
        os.chmod(self.install_root / "clusters" / CLUSTER / "secrets.env", 0o600)
        self.bin_dir = Path(self.tmp.name) / "bin"
        self.bin_dir.mkdir()
        shim = self.bin_dir / "smbclient"
        shim.write_text(SMBCLIENT_SHIM.replace("@PYTHON@", sys.executable), encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        self.shim_config = {
            "root": str(self.share_root),
            "source": SOURCE,
            "user": self.USERNAME,
            "password": self.PASSWORD,
            "domain": "CORP",
        }
        self.write_shim_config()
        patcher = mock.patch.object(reader, "SAFE_PATH", f"{self.bin_dir}:/usr/bin:/bin")
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_shim_config(self):
        (self.bin_dir / "shim.json").write_text(json.dumps(self.shim_config), encoding="utf-8")

    def publish(self, stamp="20260930-020000", files=None, **kw):
        cluster_dir = self.share_root / CLUSTER
        if not (cluster_dir / "galera-backup-owner.json").exists():
            (cluster_dir / "galera-backup-owner.json").write_bytes(owner_marker())
        write_artifact(
            cluster_dir / f"galera-{CLUSTER}-{stamp}",
            files if files is not None else build_artifact(stamp=stamp, **kw),
        )

    def test_fresh_complete_artifact_validates_over_smb_without_a_mount(self):
        self.publish()
        evidence = self.collect()
        self.assertEqual(evidence.get("access"), "smbclient", evidence)
        summary, failures, undetermined = self.judge(evidence)
        self.assertEqual((failures, undetermined), ([], []))
        self.assertIn("read via smbclient", summary)

    def test_stale_and_tampered_artifacts_fail_over_smb(self):
        self.publish(created=NOW - 30 * HOUR)
        self.assertFails("stale")
        shutil.rmtree(self.share_root / CLUSTER / "galera-c1-20260930-020000")
        self.publish(tamper=True)
        self.assertFails("checksum mismatch")

    def test_incomplete_artifact_fails_over_smb(self):
        files = build_artifact()
        del files["backup.tar.enc"]
        self.publish(files=files)
        self.assertFails("missing backup.tar.enc")

    def test_absent_cluster_directory_is_a_measured_missing_backup(self):
        shutil.rmtree(self.share_root / CLUSTER)
        summary, failures, undetermined = self.verdict()
        self.assertIsNone(summary)
        self.assertTrue(any("no backups found" in item for item in failures), failures)
        self.assertTrue(any("galera-backup-owner.json" in item for item in failures), failures)
        self.assertEqual(undetermined, [])

    def test_rejected_login_is_undetermined_and_never_echoes_the_credentials(self):
        self.publish()
        self.shim_config["password"] = "some-other-password"
        self.write_shim_config()
        evidence = self.collect()
        self.assertEqual(evidence["status"], "unavailable")
        self.assertIn("NT_STATUS_LOGON_FAILURE", evidence["reason"])
        for secret in (self.PASSWORD, self.USERNAME, "some-other-password"):
            self.assertNotIn(secret, json.dumps(evidence))
        self.assertUndetermined("NT_STATUS_LOGON_FAILURE", evidence=evidence)

    def test_unreachable_server_is_undetermined(self):
        self.publish()
        self.shim_config["refuse"] = True
        self.write_shim_config()
        self.assertUndetermined("NT_STATUS_CONNECTION_REFUSED")

    def test_unusable_installed_credentials_are_undetermined(self):
        self.publish()
        (self.install_root / "clusters" / CLUSTER / "secrets.env").unlink()
        self.assertUndetermined("credentials are unusable")

    def test_listing_format_is_parsed_and_partial_output_is_not_trusted(self):
        sample = (
            "  .                                   D        0  Tue Jun 10 12:03:54 2014\n"
            "  ..                                  D        0  Tue Jun 10 12:03:54 2014\n"
            "  galera-c1-20260930-020000           D        0  Wed Sep 30 02:00:12 2026\n"
            "  backup.tar.enc                      N  1234567  Wed Sep 30 02:00:12 2026\n"
            "\n\t\t56789 blocks of size 1024. 12345 blocks available\n"
        )
        self.assertEqual(
            reader.parse_smbclient_listing(sample),
            [
                ("galera-c1-20260930-020000", "D", 0),
                ("backup.tar.enc", "N", 1234567),
            ],
        )
        self.assertIsNone(reader.parse_smbclient_listing(sample.split("\n\n")[0]))


# === controller <-> host transport ===========================================


class SmbTransportTests(SmbFixture):
    """Runs the exact command the probe ships, locally instead of over SSH."""

    def local_ansible(self, ctx, pattern, script, timeout=120):
        result = _probe_common.AnsibleResult()
        proc = subprocess.run(
            script, shell=True, capture_output=True, text=True, timeout=timeout
        )
        if proc.returncode != 0:
            result.errors[pattern] = (proc.stderr or "failed")[:200]
        else:
            result.bodies[pattern] = proc.stdout.strip()
        return result

    def backup(self):
        return {
            "destination": "smb",
            "freshness_sla_hours": 26,
            "scheduler": {"host": "db1"},
            "smb": {"source": SOURCE, "mount_point": str(self.mount_point)},
        }

    def run_probe_smb(self, backup=None, runner=None):
        failures, undetermined = [], []
        with mock.patch.object(self.probe, "BACKUP_INSTALL_ROOT", str(self.install_root)), \
                mock.patch.object(self.probe, "run_ansible", runner or self.local_ansible):
            summary = self.probe.probe_smb(
                backup or self.backup(), 26, NOW, failures, undetermined
            )
        return summary, failures, undetermined

    def test_command_survives_ansible_templating_and_argument_limits(self):
        command = self.probe.reader_command(self.params())
        for token in ("{{", "{%", "{#"):
            self.assertNotIn(token, command)
        self.assertLess(len(command), 100_000)

    def test_unreadable_installed_config_reaches_the_verdict_as_undetermined(self):
        (self.install_root / "clusters" / CLUSTER / "config.json").unlink()
        summary, failures, undetermined = self.run_probe_smb()
        self.assertIsNone(summary)
        self.assertEqual(failures, [])
        self.assertTrue(any("unreadable" in item for item in undetermined), undetermined)

    def test_installed_config_drift_reaches_the_verdict_as_failure(self):
        self.write_config(source="//other-server/backups")
        summary, failures, undetermined = self.run_probe_smb()
        self.assertIsNone(summary)
        self.assertTrue(any("drifted from cluster.yml" in item for item in failures), failures)
        self.assertEqual(undetermined, [])

    def test_unreachable_host_is_undetermined(self):
        def unreachable(ctx, pattern, script, timeout=120):
            result = _probe_common.AnsibleResult()
            result.errors[pattern] = "Failed to connect to the host via ssh"
            return result

        summary, failures, undetermined = self.run_probe_smb(runner=unreachable)
        self.assertIsNone(summary)
        self.assertEqual(failures, [])
        self.assertTrue(any("nie odpowiedzial" in item for item in undetermined), undetermined)

    def test_reader_output_without_evidence_is_undetermined(self):
        def silent(ctx, pattern, script, timeout=120):
            result = _probe_common.AnsibleResult()
            result.bodies[pattern] = "nothing useful"
            return result

        summary, failures, undetermined = self.run_probe_smb(runner=silent)
        self.assertIsNone(summary)
        self.assertEqual(failures, [])
        self.assertTrue(any("no evidence line" in item for item in undetermined), undetermined)

    def test_scheduler_host_must_exist_in_the_inventory(self):
        backup = self.backup()
        backup["scheduler"] = {"host": "ghost"}

        def must_not_run(ctx, pattern, script, timeout=120):
            raise AssertionError("no host should be contacted")

        summary, failures, undetermined = self.run_probe_smb(backup, must_not_run)
        self.assertIsNone(summary)
        self.assertTrue(any("not defined in the inventory" in item for item in failures))


if __name__ == "__main__":
    unittest.main()
