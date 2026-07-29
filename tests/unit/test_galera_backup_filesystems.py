import os
import json
import unittest
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from tests.unit.galera_backup_testlib import load_galera_backup_module


class GaleraBackupFilesystemsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.mod = load_galera_backup_module()
        except Exception:
            cls.mod = None

    def setUp(self):
        if self.mod is None:
            self.skipTest("galera-backup executable not implemented yet")

    def make_fake_findmnt_info(self, target: str, source: str = "/dev/sdb1", fstype: str = "nfs4", majmin: str = "8:17"):
        return {
            "target": target,
            "source": source,
            "fstype": fstype,
            "majmin": majmin,
            "fsroot": "/",
            "options": "rw,relatime",
        }

    def test_rejects_non_mount_point(self):
        with tempfile.TemporaryDirectory() as td:
            mount_path = Path(td) / "not_a_mount"
            mount_path.mkdir()

            backend = self.mod.FilesystemBackend(
                mount_point=mount_path,
                expected_fstype="nfs4",
                cluster_name="claude-r10b",
            )

            # Mock check_mount to simulate not a mount point
            with patch.object(backend, "_get_mount_info", side_effect=self.mod.BackupError("E_STORAGE", "Not a mount point")):
                with self.assertRaises(self.mod.BackupError) as ctx:
                    backend.preflight()
                self.assertEqual(ctx.exception.code, "E_STORAGE")

    def test_rejects_root_filesystem_or_wrong_fstype(self):
        with tempfile.TemporaryDirectory() as td:
            mount_path = Path(td)
            backend = self.mod.FilesystemBackend(
                mount_point=mount_path,
                expected_fstype="nfs4",
                cluster_name="claude-r10b",
            )

            # Case 1: Root filesystem /
            fake_root = self.make_fake_findmnt_info("/", "/dev/sda1", "ext4")
            with patch.object(backend, "_get_mount_info", return_value=fake_root):
                with self.assertRaises(self.mod.BackupError) as ctx:
                    backend.preflight()
                self.assertEqual(ctx.exception.code, "E_STORAGE")

            # Case 2: Wrong fstype
            fake_ext4 = self.make_fake_findmnt_info(str(mount_path), "/dev/sdb1", "ext4")
            with patch.object(backend, "_get_mount_info", return_value=fake_ext4):
                with self.assertRaises(self.mod.BackupError) as ctx:
                    backend.preflight()
                self.assertEqual(ctx.exception.code, "E_STORAGE")

    def test_owner_marker_claim_and_foreign_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            mount_path = Path(td)
            backend = self.mod.FilesystemBackend(
                mount_point=mount_path,
                expected_fstype="nfs4",
                cluster_name="claude-r10b",
            )
            fake_mount = self.make_fake_findmnt_info(str(mount_path))

            with patch.object(backend, "_get_mount_info", return_value=fake_mount):
                # 1. Empty mount gets owner marker
                backend.preflight()
                owner_file = mount_path / "claude-r10b" / "galera-backup-owner.json"
                self.assertTrue(owner_file.exists())
                owner_data = json.loads(owner_file.read_text())
                self.assertEqual(owner_data["cluster_name"], "claude-r10b")

                # 2. Foreign owner marker fails
                owner_file.write_text(json.dumps({"format_version": 1, "cluster_name": "foreign-cluster"}))
                with self.assertRaises(self.mod.BackupError) as ctx:
                    backend.preflight()
                self.assertEqual(ctx.exception.code, "E_OWNER_CONFLICT")

    def test_atomic_publication_and_partial_cleanup(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as work_td:
            mount_path = Path(td)
            work_path = Path(work_td)

            backend = self.mod.FilesystemBackend(
                mount_point=mount_path,
                expected_fstype="nfs4",
                cluster_name="claude-r10b",
            )
            fake_mount = self.make_fake_findmnt_info(str(mount_path))

            payload_file = work_path / "backup.tar.enc"
            checksum_file = work_path / "backup.sha256"
            metadata_file = work_path / "metadata.json"

            payload_data = b"payload-bytes-999"
            payload_file.write_bytes(payload_data)

            import hashlib
            sha = hashlib.sha256(payload_data).hexdigest()
            checksum_file.write_text(f"{sha}  backup.tar.enc\n")

            meta = {
                "format_version": 1,
                "cluster_name": "claude-r10b",
                "backup_name": "galera-claude-r10b-20260729-120000",
                "created_unixtime": 1785240000,
                "encrypted_sha256": sha,
                "encrypted_size_bytes": len(payload_data),
            }
            metadata_file.write_text(json.dumps(meta))

            art = self.mod.ArtifactSet(
                backup_name="galera-claude-r10b-20260729-120000",
                payload_path=payload_file,
                checksum_path=checksum_file,
                metadata_path=metadata_file,
            )

            with patch.object(backend, "_get_mount_info", return_value=fake_mount):
                backend.preflight()
                pub = backend.publish(art)

                final_dir = mount_path / "claude-r10b" / "galera-claude-r10b-20260729-120000"
                self.assertTrue(final_dir.exists())
                self.assertTrue((final_dir / "metadata.json").exists())

                # Partial dir shouldn't exist
                partial_dir = mount_path / "claude-r10b" / ".partial-galera-claude-r10b-20260729-120000"
                self.assertFalse(partial_dir.exists())

    def test_retention_scoped_to_cluster(self):
        with tempfile.TemporaryDirectory() as td:
            mount_path = Path(td)
            backend = self.mod.FilesystemBackend(
                mount_point=mount_path,
                expected_fstype="nfs4",
                cluster_name="claude-r10b",
            )
            fake_mount = self.make_fake_findmnt_info(str(mount_path))

            cluster_dir = mount_path / "claude-r10b"
            cluster_dir.mkdir(parents=True)

            # Old backup
            old_dir = cluster_dir / "galera-claude-r10b-20260701-120000"
            old_dir.mkdir()
            old_meta = {
                "format_version": 1,
                "cluster_name": "claude-r10b",
                "backup_name": "galera-claude-r10b-20260701-120000",
                "created_unixtime": 1000,
            }
            (old_dir / "metadata.json").write_text(json.dumps(old_meta))

            # New backup
            new_dir = cluster_dir / "galera-claude-r10b-20260729-120000"
            new_dir.mkdir()
            new_meta = {
                "format_version": 1,
                "cluster_name": "claude-r10b",
                "backup_name": "galera-claude-r10b-20260729-120000",
                "created_unixtime": 1785240000,
            }
            (new_dir / "metadata.json").write_text(json.dumps(new_meta))

            with patch.object(backend, "_get_mount_info", return_value=fake_mount):
                now = datetime.fromtimestamp(1785240000, tz=timezone.utc)
                count = backend.prune(now, retention_days=14)
                self.assertEqual(count, 1)
                self.assertFalse(old_dir.exists())
                self.assertTrue(new_dir.exists())


if __name__ == "__main__":
    unittest.main()
