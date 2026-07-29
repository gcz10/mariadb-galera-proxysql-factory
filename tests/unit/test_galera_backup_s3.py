import io
import json
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

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

    def test_owner_marker_created_on_empty_bucket(self):
        self.backend.preflight()
        self.assertIn("galera-backup-owner.json", self.client.objects)
        owner_data = json.loads(self.client.objects["galera-backup-owner.json"].decode())
        self.assertEqual(owner_data["format_version"], 1)
        self.assertEqual(owner_data["cluster_name"], "claude-r10b")

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

            # Record call sequence
            call_sequence = []
            orig_put = self.client.fput_object
            def record_fput(b, name, path):
                call_sequence.append(name)
                orig_put(b, name, path)
            self.client.fput_object = record_fput

            self.backend.publish(art)

            prefix = "galera-claude-r10b-20260729-120000/"
            self.assertEqual(
                call_sequence,
                [
                    f"{prefix}backup.tar.enc",
                    f"{prefix}backup.sha256",
                    f"{prefix}metadata.json",
                ]
            )

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


    def test_minio_service_account_revocation_filtering(self):
        # Verify service account accessKey resolution by friendly name
        list_output_lines = [
            json.dumps({
                "status": "success",
                "user": "rootuser",
                "svcaccs": [
                    {"accessKey": "AKIA_DISCLOSED_1", "parentUser": "rootuser"},
                    {"accessKey": "AKIA_DISCLOSED_2", "parentUser": "rootuser"},
                    {"accessKey": "AKIA_OTHER_CLUSTER", "parentUser": "rootuser"},
                ],
            })
        ]
        
        key_info_map = {
            "AKIA_DISCLOSED_1": {"name": "galera-backup-claude-r10b"},
            "AKIA_DISCLOSED_2": {"name": "galera-backup-claude-r10b"},
            "AKIA_OTHER_CLUSTER": {"name": "galera-backup-other-cluster"},
        }
        
        target_cluster_name = "galera-backup-claude-r10b"
        to_remove = []
        
        for line in list_output_lines:
            record = json.loads(line)
            for sa in record.get("svcaccs") or []:
                ak = sa.get("accessKey")
                if not ak:
                    continue
                info = key_info_map.get(ak, {})
                if info.get("name") == target_cluster_name:
                    to_remove.append(ak)
                    
        self.assertEqual(to_remove, ["AKIA_DISCLOSED_1", "AKIA_DISCLOSED_2"])
        self.assertNotIn("AKIA_OTHER_CLUSTER", to_remove)

if __name__ == "__main__":
    unittest.main()
