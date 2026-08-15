import importlib.util
import io
import json
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock

from tests.unit.galera_backup_testlib import load_galera_backup_module


class FakeMinioClient:
    def __init__(self, bucket_exists=True):
        self._bucket_exists = bucket_exists
        self.objects: dict[str, bytes] = {}

    def bucket_exists(self, bucket_name: str) -> bool:
        return self._bucket_exists

    def list_objects(self, bucket_name: str, prefix: str = "", recursive: bool = True):
        results = []
        for key in sorted(self.objects.keys()):
            if not prefix or key.startswith(prefix):
                obj = MagicMock()
                obj.object_name = key
                obj.size = len(self.objects[key])
                obj.last_modified = datetime.now(timezone.utc)
                results.append(obj)
        return results

    def get_object(self, bucket_name: str, object_name: str):
        if object_name not in self.objects:
            raise Exception("NoSuchKey")
        data = self.objects[object_name]
        return io.BytesIO(data)

    def fget_object(self, bucket_name: str, object_name: str, file_path: str):
        if object_name not in self.objects:
            raise Exception("NoSuchKey")
        data = self.objects[object_name]
        with open(file_path, "wb") as f:
            f.write(data)

    def put_object(self, bucket_name: str, object_name: str, data, length: int, content_type: str = ""):
        buf = data.read(length)
        self.objects[object_name] = buf

    def fput_object(self, bucket_name: str, object_name: str, file_path: str):
        with open(file_path, "rb") as f:
            self.objects[object_name] = f.read()

    def remove_object(self, bucket_name: str, object_name: str):
        if object_name in self.objects:
            del self.objects[object_name]

    def remove_objects(self, bucket_name: str, delete_object_list):
        for item in delete_object_list:
            name = item.name if hasattr(item, "name") else item
            if name in self.objects:
                del self.objects[name]


class GaleraBackupS3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.mod = load_galera_backup_module()
        except Exception:
            cls.mod = None

    def setUp(self):
        if self.mod is None:
            self.skipTest("galera-backup executable not implemented yet")

        self.client = FakeMinioClient()
        self.backend = self.mod.S3Backend(
            endpoint="192.168.1.47:9000",
            bucket="r10b-galera-backups",
            secure=False,
            access_key="access",
            secret_key="secret",
            cluster_name="claude-r10b",
            client=self.client,
        )

    def test_missing_owner_marker_fails_closed(self):
        with self.assertRaises(self.mod.BackupError) as ctx:
            self.backend.preflight()
        self.assertEqual(ctx.exception.code, "E_OWNER_CONFLICT")
        self.assertIn("storage administrator", ctx.exception.public_message)

    def test_owner_marker_matching_is_idempotent(self):
        self.client.objects["galera-backup-owner.json"] = json.dumps(
            {"format_version": 1, "cluster_name": "claude-r10b"}
        ).encode()
        self.backend.preflight()
        self.assertIn("galera-backup-owner.json", self.client.objects)

    def test_owner_marker_foreign_fails(self):
        self.client.objects["galera-backup-owner.json"] = json.dumps(
            {"format_version": 1, "cluster_name": "other-cluster"}
        ).encode()
        with self.assertRaises(self.mod.BackupError) as ctx:
            self.backend.preflight()
        self.assertEqual(ctx.exception.code, "E_OWNER_CONFLICT")

    def test_publication_order_and_verification(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            payload_file = td_path / "backup.tar.enc"
            checksum_file = td_path / "backup.sha256"
            metadata_file = td_path / "metadata.json"

            payload_content = b"encrypted-data-payload-12345"
            payload_file.write_bytes(payload_content)

            import hashlib
            sha = hashlib.sha256(payload_content).hexdigest()
            checksum_file.write_text(f"{sha}  backup.tar.enc\n")

            meta = {
                "format_version": 1,
                "cluster_name": "claude-r10b",
                "backup_name": "galera-claude-r10b-20260729-120000",
                "created_at_utc": "2026-07-29T12:00:00Z",
                "created_unixtime": 1785240000,
                "encrypted_sha256": sha,
                "plaintext_sha256": "plain-sha",
                "encrypted_size_bytes": len(payload_content),
                "wsrep_uuid": "uuid-123",
                "wsrep_seqno": "456",
            }
            metadata_file.write_text(json.dumps(meta))

            art = self.mod.ArtifactSet(
                backup_name="galera-claude-r10b-20260729-120000",
                payload_path=payload_file,
                checksum_path=checksum_file,
                metadata_path=metadata_file,
            )

            call_sequence = []
            original_put = self.client.fput_object
            original_get = self.client.fget_object

            def record_fput(bucket, name, path):
                call_sequence.append(("put", name))
                original_put(bucket, name, path)

            def record_fget(bucket, name, path):
                call_sequence.append(("get", name))
                original_get(bucket, name, path)

            self.client.fput_object = record_fput
            self.client.fget_object = record_fget

            self.backend.publish(art)

            prefix = "galera-claude-r10b-20260729-120000/"
            self.assertEqual(
                call_sequence,
                [
                    ("put", f"{prefix}backup.tar.enc"),
                    ("put", f"{prefix}backup.sha256"),
                    ("get", f"{prefix}backup.tar.enc"),
                    ("put", f"{prefix}metadata.json"),
                ],
            )

            original_remote_payload = self.client.objects[
                f"{prefix}backup.tar.enc"
            ]
            payload_file.write_bytes(b"different encrypted payload")
            with self.assertRaises(self.mod.BackupError) as duplicate_ctx:
                self.backend.publish(art)
            self.assertEqual(duplicate_ctx.exception.code, "E_STORAGE")
            self.assertEqual(
                self.client.objects[f"{prefix}backup.tar.enc"],
                original_remote_payload,
            )

    def test_failed_publication_removes_incomplete_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = root / "backup.tar.enc"
            checksum = root / "backup.sha256"
            metadata = root / "metadata.json"
            payload_content = b"encrypted-payload"
            payload.write_bytes(payload_content)

            import hashlib

            digest = hashlib.sha256(payload_content).hexdigest()
            checksum.write_text(
                f"{digest}  backup.tar.enc\n",
                encoding="utf-8",
            )
            backup_name = "galera-claude-r10b-20260729-120001"
            metadata.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "cluster_name": "claude-r10b",
                        "backup_name": backup_name,
                        "created_unixtime": 1785240001,
                        "encrypted_sha256": digest,
                        "encrypted_size_bytes": len(payload_content),
                    }
                ),
                encoding="utf-8",
            )
            artifact = self.mod.ArtifactSet(
                backup_name=backup_name,
                payload_path=payload,
                checksum_path=checksum,
                metadata_path=metadata,
            )

            original_put = self.client.fput_object

            def fail_checksum(bucket, name, path):
                original_put(bucket, name, path)
                if name.endswith("/backup.sha256"):
                    raise OSError("checksum upload interrupted after write")

            self.client.fput_object = fail_checksum
            with self.assertRaises(self.mod.BackupError) as ctx:
                self.backend.publish(artifact)

            self.assertEqual(ctx.exception.code, "E_STORAGE")
            prefix = f"{backup_name}/"
            self.assertEqual(
                [
                    key
                    for key in self.client.objects
                    if key.startswith(prefix)
                ],
                [],
            )

            def refuse_cleanup(bucket, name):
                raise OSError("cleanup denied")

            self.client.remove_object = refuse_cleanup
            with self.assertRaises(self.mod.BackupError) as cleanup_ctx:
                self.backend.publish(artifact)

            self.assertEqual(cleanup_ctx.exception.code, "E_STORAGE")
            self.assertIn(
                "checksum upload interrupted after write",
                cleanup_ctx.exception.public_message,
            )
            self.assertIn("cleanup denied", cleanup_ctx.exception.public_message)

    def test_retention_prunes_only_own_expired_backups(self):
        # Create expired backup metadata and payload
        prefix_old = "galera-claude-r10b-20260701-120000/"
        meta_old = {
            "format_version": 1,
            "cluster_name": "claude-r10b",
            "backup_name": "galera-claude-r10b-20260701-120000",
            "created_unixtime": 1000,
        }
        self.client.objects[f"{prefix_old}metadata.json"] = json.dumps(meta_old).encode()
        self.client.objects[f"{prefix_old}backup.tar.enc"] = b"old-data"

        # Create fresh backup metadata
        prefix_new = "galera-claude-r10b-20260729-120000/"
        meta_new = {
            "format_version": 1,
            "cluster_name": "claude-r10b",
            "backup_name": "galera-claude-r10b-20260729-120000",
            "created_unixtime": 1785240000,
        }
        self.client.objects[f"{prefix_new}metadata.json"] = json.dumps(meta_new).encode()
        self.client.objects[f"{prefix_new}backup.tar.enc"] = b"new-data"

        # Foreign prefix
        self.client.objects["galera-other-cluster-20260701-120000/metadata.json"] = b"{}"

        now = datetime.fromtimestamp(1785240000, tz=timezone.utc)
        deleted_count = self.backend.prune(now, retention_days=14)

        self.assertEqual(deleted_count, 1)
        self.assertNotIn(f"{prefix_old}metadata.json", self.client.objects)
        self.assertIn(f"{prefix_new}metadata.json", self.client.objects)
        self.assertIn("galera-other-cluster-20260701-120000/metadata.json", self.client.objects)


    def test_retention_rejects_non_integer_metadata_timestamp(self):
        prefix = "galera-claude-r10b-20260701-120000/"
        self.client.objects[f"{prefix}metadata.json"] = json.dumps(
            {
                "format_version": 1,
                "cluster_name": "claude-r10b",
                "created_unixtime": "1000",
            }
        ).encode()
        self.client.objects[f"{prefix}backup.tar.enc"] = b"old-data"

        with self.assertRaises(self.mod.BackupError) as ctx:
            self.backend.prune(
                datetime.fromtimestamp(1785240000, tz=timezone.utc),
                retention_days=14,
            )

        self.assertEqual(ctx.exception.code, "E_STORAGE")
        self.assertIn(f"{prefix}metadata.json", self.client.objects)
    def test_fetch_latest_rejects_non_integer_metadata_timestamp(self):
        prefix = "galera-claude-r10b-20260701-120000/"
        self.client.objects[f"{prefix}metadata.json"] = json.dumps(
            {
                "format_version": 1,
                "cluster_name": "claude-r10b",
                "created_unixtime": "1000",
            }
        ).encode()
        self.client.objects[f"{prefix}backup.tar.enc"] = b"old-data"
        self.client.objects[f"{prefix}backup.sha256"] = b"sha256  backup.tar.enc\n"

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(self.mod.BackupError) as ctx:
                self.backend.fetch_latest(Path(td))

        self.assertEqual(ctx.exception.code, "E_STORAGE")

    def load_minio_access_key_filters(self):
        plugin_path = (
            Path(__file__).resolve().parents[2]
            / "roles"
            / "galera_backup"
            / "filter_plugins"
            / "minio_access_keys.py"
        )
        self.assertTrue(plugin_path.is_file(), "MinIO access-key filter plugin is missing")
        spec = importlib.util.spec_from_file_location("minio_access_keys", plugin_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.minio_service_account_keys, module.minio_access_keys_named

    def test_minio_service_account_list_extracts_actual_keys(self):
        list_keys, _ = self.load_minio_access_key_filters()
        output = json.dumps(
            {
                "status": "success",
                "user": "rootuser",
                "svcaccs": [
                    {"accessKey": "matching-key-1", "parentUser": "rootuser"},
                    {"accessKey": "other-key", "parentUser": "rootuser"},
                    {"accessKey": "matching-key-1", "parentUser": "rootuser"},
                ],
            }
        )

        self.assertEqual(list_keys(output), ["matching-key-1", "other-key"])

    def test_minio_service_account_list_accepts_null_svcaccs_on_fresh_server(self):
        # Regresja: swieze MinIO zwraca dokladnie ta linie dla principala bez
        # kont serwisowych. Filtr traktowal `null` jak uszkodzone wyjscie i
        # wywalal `cluster-backup-configure` na kazdej nowej instancji.
        list_keys, _ = self.load_minio_access_key_filters()
        output = '{"status":"success","user":"4aqtYG964aX9","stsKeys":null,"svcaccs":null}'

        self.assertEqual(list_keys(output), [])

    def test_minio_service_account_list_rejects_wrong_typed_svcaccs(self):
        # `null` jest legalnym "zero kont", ale wartosc OBECNA i zlego typu
        # oznacza zmiane kontraktu `mc` — cicha akceptacja gubilaby klucze.
        list_keys, _ = self.load_minio_access_key_filters()
        output = json.dumps({"status": "success", "user": "root", "svcaccs": "not-a-list"})

        with self.assertRaises(ValueError):
            list_keys(output)

    def test_minio_service_account_info_selects_keys_by_exact_name(self):
        _, select_keys = self.load_minio_access_key_filters()
        info_outputs = [
            json.dumps(
                {
                    "status": "success",
                    "accessKey": "matching-key-1",
                    "name": "galera-backup-claude-r10b",
                }
            ),
            json.dumps(
                {
                    "status": "success",
                    "accessKey": "other-key",
                    "name": "galera-backup-other",
                }
            ),
            json.dumps(
                {
                    "status": "success",
                    "accessKey": "matching-key-2",
                    "name": "galera-backup-claude-r10b",
                }
            ),
        ]

        self.assertEqual(
            select_keys(info_outputs, "galera-backup-claude-r10b"),
            ["matching-key-1", "matching-key-2"],
        )

    def test_minio_service_account_filter_rejects_malformed_list_output(self):
        list_keys, _ = self.load_minio_access_key_filters()

        with self.assertRaisesRegex(ValueError, "line 1 is not valid JSON"):
            list_keys("not-json")

    def test_minio_service_account_filter_rejects_matching_info_without_key(self):
        _, select_keys = self.load_minio_access_key_filters()
        info_outputs = [
            json.dumps(
                {
                    "status": "success",
                    "name": "galera-backup-claude-r10b",
                }
            )
        ]

        with self.assertRaisesRegex(ValueError, "matching service account has no accessKey"):
            select_keys(info_outputs, "galera-backup-claude-r10b")

if __name__ == "__main__":
    unittest.main()
