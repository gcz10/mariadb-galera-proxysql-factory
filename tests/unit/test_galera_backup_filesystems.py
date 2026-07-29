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


class ManagedSMBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.mod = load_galera_backup_module()
        except Exception:
            cls.mod = None

    def setUp(self):
        if self.mod is None:
            self.skipTest("galera-backup executable not implemented yet")

    def test_missing_cifs_module_diagnostic_without_mount(self):
        smb = self.mod.SMBBackend(
            source="//nas/backups",
            mount_point="/mnt/smb",
            options=["vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
            username="smbuser",
            password="smbpassword",
            domain="DOMAIN",
            cluster_name="claude-r10b",
        )

        with patch.object(smb, "_check_cifs_available", return_value=(False, "6.12.0-211.16.1.el10_2", "6.12.0-211.39.1.el10_2")):
            with patch.object(smb, "_exec_mount") as mock_mount:
                with self.assertRaises(self.mod.BackupError) as ctx:
                    smb.preflight()
                self.assertEqual(ctx.exception.code, "E_CIFS_MODULE")
                self.assertIn("6.12.0-211.16.1.el10_2", ctx.exception.public_message)
                self.assertIn("6.12.0-211.39.1.el10_2", ctx.exception.public_message)
                self.assertEqual(mock_mount.call_count, 0)

    def test_credentials_file_and_mount_argv_safety(self):
        with tempfile.TemporaryDirectory() as td:
            mount_path = Path(td)
            smb = self.mod.SMBBackend(
                source="//nas/backups",
                mount_point=mount_path,
                options=["vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
                username="smbuser",
                password="smbpassword",
                domain="MYDOMAIN",
                cluster_name="claude-r10b",
            )

            mounted_cmds = []

            def fake_exec_mount(cmd, cred_path, cred_content):
                mounted_cmds.append(cmd)
                self.assertIn("username=smbuser\n", cred_content)
                self.assertIn("password=smbpassword\n", cred_content)
                self.assertIn("domain=MYDOMAIN\n", cred_content)
                st = os.stat(cred_path)
                self.assertEqual(st.st_mode & 0o777, 0o600)
                return (0, "", "")

            fake_mount_info = {
                "target": str(mount_path),
                "source": "//nas/backups",
                "fstype": "cifs",
                "options": "rw,vers=3.1.1,seal,nosuid,nodev,noexec",
                "majmin": "0:42",
                "fsroot": "/",
            }

            with patch.object(smb, "_check_cifs_available", return_value=(True, "6.12", "6.12")):
                with patch.object(smb, "_check_target_not_mounted"):
                    with patch.object(smb, "_exec_mount", side_effect=fake_exec_mount):
                        with patch.object(smb.fs_backend, "_get_mount_info", return_value=fake_mount_info):
                            with patch.object(smb, "_exec_umount", return_value=(0, "", "")):
                                with smb:
                                    smb.preflight()

            self.assertEqual(len(mounted_cmds), 1)
            cmd_str = " ".join(mounted_cmds[0])
            self.assertNotIn("smbpassword", cmd_str)
            self.assertIn("credentials=", cmd_str)
    def test_cleanup_credentials_and_umount_on_failure(self):
        smb = self.mod.SMBBackend(
            source="//nas/backups",
            mount_point="/mnt/smb",
            options=["vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
            username="smbuser",
            password="smbpassword",
            domain=None,
            cluster_name="claude-r10b",
        )

        cred_file_created = []
        cred_file_removed = []

        def fake_exec_mount(cmd, cred_path, cred_content):
            cred_file_created.append(cred_path)
            return (1, "", "Mount failed")

        with patch.object(smb, "_check_cifs_available", return_value=(True, "6.12", "6.12")):
            with patch.object(smb, "_check_target_not_mounted"):
                with patch.object(smb, "_exec_mount", side_effect=fake_exec_mount):
                    with self.assertRaises(self.mod.BackupError) as ctx:
                        with smb:
                            smb.preflight()
                    self.assertEqual(ctx.exception.code, "E_STORAGE")

        # Credential file should be cleaned up
        for cp in cred_file_created:
            self.assertFalse(os.path.exists(cp))
if __name__ == "__main__":
    unittest.main()
