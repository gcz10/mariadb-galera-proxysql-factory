import os
import json
import hashlib
import sys
import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "roles" / "galera_backup" / "files"))

from galera_backup import pipeline  # noqa: E402


class GaleraBackupFilesystemsTests(unittest.TestCase):
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

            backend = pipeline.FilesystemBackend(
                mount_point=mount_path,
                expected_fstype="nfs4",
                cluster_name="claude-r10b",
            )

            # Mock check_mount to simulate not a mount point
            with patch.object(backend, "_get_mount_info", side_effect=pipeline.BackupError("E_STORAGE", "Not a mount point")):
                with self.assertRaises(pipeline.BackupError) as ctx:
                    backend.preflight()
                self.assertEqual(ctx.exception.code, "E_STORAGE")

    def test_rejects_root_filesystem_or_wrong_fstype(self):
        with tempfile.TemporaryDirectory() as td:
            mount_path = Path(td)
            backend = pipeline.FilesystemBackend(
                mount_point=mount_path,
                expected_fstype="nfs4",
                cluster_name="claude-r10b",
            )

            # Case 1: Root filesystem /
            fake_root = self.make_fake_findmnt_info("/", "/dev/sda1", "ext4")
            with patch.object(backend, "_get_mount_info", return_value=fake_root):
                with self.assertRaises(pipeline.BackupError) as ctx:
                    backend.preflight()
                self.assertEqual(ctx.exception.code, "E_STORAGE")

            # Case 2: Wrong fstype
            fake_ext4 = self.make_fake_findmnt_info(str(mount_path), "/dev/sdb1", "ext4")
            with patch.object(backend, "_get_mount_info", return_value=fake_ext4):
                with self.assertRaises(pipeline.BackupError) as ctx:
                    backend.preflight()
                self.assertEqual(ctx.exception.code, "E_STORAGE")

    def test_owner_marker_claim_and_foreign_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            mount_path = Path(td)
            backend = pipeline.FilesystemBackend(
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
                with self.assertRaises(pipeline.BackupError) as ctx:
                    backend.preflight()
                self.assertEqual(ctx.exception.code, "E_OWNER_CONFLICT")

                # 3. Wersja artefaktu nie jest wersja markera ownership.
                owner_file.write_text(json.dumps({"format_version": 2, "cluster_name": "claude-r10b"}))
                with self.assertRaises(pipeline.BackupError) as ctx:
                    backend.preflight()
                self.assertEqual(ctx.exception.code, "E_OWNER_CONFLICT")

    def test_fetch_latest_accepts_v2_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mount_path = root / "mount"
            mount_path.mkdir()
            backend = pipeline.FilesystemBackend(
                mount_point=mount_path,
                expected_fstype="nfs4",
                cluster_name="claude-r10b",
            )
            fake_mount = self.make_fake_findmnt_info(str(mount_path))
            backup_name = "galera-claude-r10b-20260729-120000"

            with patch.object(backend, "_get_mount_info", return_value=fake_mount):
                backend.preflight()
                backup_dir = mount_path / "claude-r10b" / backup_name
                backup_dir.mkdir()
                (backup_dir / "backup.tar.enc").write_bytes(b"v2-payload")
                (backup_dir / "backup.sha256").write_text("fixture\n", encoding="utf-8")
                (backup_dir / "metadata.json").write_text(
                    json.dumps(
                        {
                            "format_version": 2,
                            "cluster_name": "claude-r10b",
                            "created_unixtime": 1785240000,
                        }
                    ),
                    encoding="utf-8",
                )

                artifact = backend.fetch_latest(root / "fetched")

            self.assertEqual(artifact.backup_name, backup_name)
            self.assertEqual(
                json.loads(artifact.metadata_path.read_text(encoding="utf-8"))["format_version"],
                2,
            )

    def test_atomic_publication_and_partial_cleanup(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as work_td:
            mount_path = Path(td)
            work_path = Path(work_td)

            backend = pipeline.FilesystemBackend(
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

            art = pipeline.ArtifactSet(
                backup_name="galera-claude-r10b-20260729-120000",
                payload_path=payload_file,
                checksum_path=checksum_file,
                metadata_path=metadata_file,
            )

            with patch.object(backend, "_get_mount_info", return_value=fake_mount):
                backend.preflight()
                backend.publish(art)

                final_dir = mount_path / "claude-r10b" / "galera-claude-r10b-20260729-120000"
                self.assertTrue(final_dir.exists())
                self.assertTrue((final_dir / "metadata.json").exists())

                # Partial dir shouldn't exist
                partial_dir = mount_path / "claude-r10b" / ".partial-galera-claude-r10b-20260729-120000"
                self.assertFalse(partial_dir.exists())

                with self.assertRaises(pipeline.BackupError) as duplicate_ctx:
                    backend.publish(art)
                self.assertEqual(duplicate_ctx.exception.code, "E_STORAGE")
                self.assertTrue(final_dir.exists())
                self.assertEqual(
                    (final_dir / "backup.tar.enc").read_bytes(),
                    payload_data,
                )

    def test_retention_scoped_to_cluster(self):
        with tempfile.TemporaryDirectory() as td:
            mount_path = Path(td)
            backend = pipeline.FilesystemBackend(
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

    def test_retention_delete_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as td:
            mount_path = Path(td)
            backend = pipeline.FilesystemBackend(
                mount_point=mount_path,
                expected_fstype="nfs4",
                cluster_name="claude-r10b",
            )
            fake_mount = self.make_fake_findmnt_info(str(mount_path))
            old_dir = (
                mount_path
                / "claude-r10b"
                / "galera-claude-r10b-20260701-120000"
            )
            old_dir.mkdir(parents=True)
            (old_dir / "metadata.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "cluster_name": "claude-r10b",
                        "created_unixtime": 1000,
                    }
                ),
                encoding="utf-8",
            )

            now = datetime.fromtimestamp(1785240000, tz=timezone.utc)
            with patch.object(backend, "_get_mount_info", return_value=fake_mount):
                with patch.object(
                    pipeline.shutil,
                    "rmtree",
                    side_effect=PermissionError("retention deletion denied"),
                ):
                    with self.assertRaises(pipeline.BackupError) as ctx:
                        backend.prune(now, retention_days=14)

            self.assertEqual(ctx.exception.code, "E_STORAGE")
            self.assertTrue(old_dir.exists())


    def test_retention_rejects_non_integer_metadata_timestamp(self):
        for invalid_timestamp in ("1000", True):
            with self.subTest(invalid_timestamp=invalid_timestamp):
                with tempfile.TemporaryDirectory() as td:
                    mount_path = Path(td)
                    backend = pipeline.FilesystemBackend(
                        mount_point=mount_path,
                        expected_fstype="nfs4",
                        cluster_name="claude-r10b",
                    )
                    fake_mount = self.make_fake_findmnt_info(str(mount_path))
                    old_dir = (
                        mount_path
                        / "claude-r10b"
                        / "galera-claude-r10b-20260701-120000"
                    )
                    old_dir.mkdir(parents=True)
                    (old_dir / "metadata.json").write_text(
                        json.dumps(
                            {
                                "format_version": 1,
                                "cluster_name": "claude-r10b",
                                "created_unixtime": invalid_timestamp,
                            }
                        ),
                        encoding="utf-8",
                    )

                    with patch.object(backend, "_get_mount_info", return_value=fake_mount):
                        with self.assertRaises(pipeline.BackupError) as ctx:
                            backend.prune(
                                datetime.fromtimestamp(1785240000, tz=timezone.utc),
                                retention_days=14,
                            )

                    self.assertEqual(ctx.exception.code, "E_STORAGE")
                    self.assertTrue(old_dir.exists())
    def test_fetch_latest_rejects_non_integer_metadata_timestamp(self):
        for invalid_timestamp in ("1000", True):
            with self.subTest(invalid_timestamp=invalid_timestamp):
                with tempfile.TemporaryDirectory() as td:
                    mount_path = Path(td)
                    backend = pipeline.FilesystemBackend(
                        mount_point=mount_path,
                        expected_fstype="nfs4",
                        cluster_name="claude-r10b",
                    )
                    backup_dir = (
                        mount_path
                        / "claude-r10b"
                        / "galera-claude-r10b-20260701-120000"
                    )
                    backup_dir.mkdir(parents=True)
                    (backup_dir / "backup.tar.enc").write_bytes(b"old-data")
                    (backup_dir / "backup.sha256").write_text(
                        "sha256  backup.tar.enc\n",
                        encoding="utf-8",
                    )
                    (backup_dir / "metadata.json").write_text(
                        json.dumps(
                            {
                                "format_version": 1,
                                "cluster_name": "claude-r10b",
                                "created_unixtime": invalid_timestamp,
                            }
                        ),
                        encoding="utf-8",
                    )

                    with self.assertRaises(pipeline.BackupError) as ctx:
                        backend.fetch_latest(Path(td) / "work")

                    self.assertEqual(ctx.exception.code, "E_STORAGE")

class ManagedSMBTests(unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.runner = pipeline.CommandRunner(set())

    def _make_smb(self, **kwargs):
        defaults = {
            "source": "//nas/backups",
            "mount_point": "/mnt/smb",
            "options": ["vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
            "username": "smbuser",
            "password": "smbpassword",
            "domain": None,
            "cluster_name": "claude-r10b",
            "runner": self.runner,
        }
        defaults.update(kwargs)
        return pipeline.SMBBackend(**defaults)

    def test_missing_cifs_module_diagnostic_without_mount(self):
        smb = self._make_smb(domain="DOMAIN")

        with patch.object(smb, "_check_cifs_available", return_value=(False, "6.12.0-211.16.1.el10_2", "6.12.0-211.39.1.el10_2")):
            with patch.object(smb, "_exec_mount") as mock_mount:
                with self.assertRaises(pipeline.BackupError) as ctx:
                    smb.preflight()
                self.assertEqual(ctx.exception.code, "E_CIFS_MODULE")
                self.assertIn("6.12.0-211.16.1.el10_2", ctx.exception.public_message)
                self.assertIn("6.12.0-211.39.1.el10_2", ctx.exception.public_message)
                self.assertEqual(mock_mount.call_count, 0)

    def test_runtime_rejects_noncanonical_smb_option_case(self):
        for options in (
            ["vers=3.1.1", "Seal", "nosuid", "nodev", "noexec"],
            ["VERS=3.1.1", "seal", "nosuid", "nodev", "noexec"],
        ):
            with self.subTest(options=options):
                errors = pipeline.validate_smb_options(options)
                self.assertTrue(errors)
                self.assertIn("lowercase", " ".join(errors))

    def test_runtime_rejects_noncanonical_smb_source_before_mount(self):
        for source in ("//nas/share/", "//nas/share/nested", "//nas", "nas/share"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as td:
                smb = self._make_smb(source=source, mount_point=Path(td) / "managed")
                with patch.object(smb, "_check_cifs_available", return_value=(True, "6.12", "6.12")):
                    with patch.object(smb, "_exec_mount") as mount:
                        with self.assertRaises(pipeline.BackupError) as raised:
                            smb.preflight()
                self.assertIn("exactly one UNC share", raised.exception.public_message)
                mount.assert_not_called()

    def test_credentials_file_and_mount_argv_safety(self):
        with tempfile.TemporaryDirectory() as td:
            mount_path = Path(td)
            smb = self._make_smb(mount_point=mount_path, domain="MYDOMAIN")

            mounted_cmds = []

            def fake_exec_mount(cmd):
                mounted_cmds.append(cmd)
                mount_options = cmd[cmd.index("-o") + 1].split(",")
                credential_option = next(option for option in mount_options if option.startswith("credentials="))
                credential_path = Path(credential_option.split("=", 1)[1])
                credential_content = credential_path.read_text(encoding="utf-8")
                self.assertIn("username=smbuser\n", credential_content)
                self.assertIn("password=smbpassword\n", credential_content)
                self.assertIn("domain=MYDOMAIN\n", credential_content)
                self.assertEqual(credential_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(credential_path.parent.stat().st_mode & 0o777, 0o700)
                self.assertNotEqual(
                    credential_path.name,
                    f"smb-credentials-claude-r10b-{os.getpid()}.key",
                )
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
    def test_managed_smb_creates_missing_mount_point_before_mount(self):
        with tempfile.TemporaryDirectory() as td:
            mount_path = Path(td) / "managed"
            smb = self._make_smb(mount_point=mount_path)

            mount_observations = []
            def fake_exec_mount(cmd):
                mount_observations.append(
                    (mount_path.is_dir(), mount_path.stat().st_mode & 0o777)
                )
                return (0, "", "")

            mount_info = {
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
                        with patch.object(smb.fs_backend, "_get_mount_info", return_value=mount_info):
                            with patch.object(smb, "_exec_umount", return_value=(0, "", "")):
                                smb.preflight()
                                smb.close()

            self.assertEqual(mount_observations, [(True, 0o750)])

    def test_cleanup_credentials_and_umount_on_failure(self):
        smb = self._make_smb()

        cred_file_created = []

        def fake_exec_mount(cmd):
            mount_options = cmd[cmd.index("-o") + 1].split(",")
            credential_option = next(option for option in mount_options if option.startswith("credentials="))
            cred_file_created.append(Path(credential_option.split("=", 1)[1]))
            return (1, "", "Mount failed")

        with patch.object(smb, "_check_cifs_available", return_value=(True, "6.12", "6.12")):
            with patch.object(smb, "_check_target_not_mounted"):
                with patch.object(smb, "_exec_mount", side_effect=fake_exec_mount):
                    with self.assertRaises(pipeline.BackupError) as ctx:
                        with smb:
                            smb.preflight()
                    self.assertEqual(ctx.exception.code, "E_STORAGE")

        # Credential file should be cleaned up
        for cp in cred_file_created:
            self.assertFalse(os.path.exists(cp))
    def test_missing_mount_cifs_is_reported_as_cifs_unavailable(self):
        smb = self._make_smb()
        probe_result = MagicMock(returncode=0, stdout="6.12.0-test\n", stderr="")

        with patch("shutil.which", return_value=None):
            with patch("subprocess.run", return_value=probe_result):
                available, running_kernel, _ = smb._check_cifs_available()

        self.assertFalse(available)
        self.assertEqual(running_kernel, "6.12.0-test")

    def test_missing_mount_cifs_preflight_names_userspace_remediation(self):
        smb = self._make_smb()
        probe_result = MagicMock(returncode=0, stdout="6.12.0-test\n", stderr="")

        with patch("shutil.which", return_value=None):
            with patch("subprocess.run", return_value=probe_result):
                with self.assertRaises(pipeline.BackupError) as ctx:
                    smb.preflight()

        self.assertEqual(ctx.exception.code, "E_CIFS_MODULE")
        self.assertIn("mount.cifs", ctx.exception.public_message)
        self.assertIn("cifs-utils", ctx.exception.public_message)
        self.assertNotIn("reboot required", ctx.exception.public_message)

    def test_preflight_rejects_wrong_observed_smb_source(self):
        with tempfile.TemporaryDirectory() as td:
            smb = self._make_smb(mount_point=td)
            observed = {
                "target": str(Path(td).resolve()),
                "source": "//other/share",
                "fstype": "cifs",
                "options": "rw,vers=3.1.1,seal,nosuid,nodev,noexec",
                "majmin": "0:42",
                "fsroot": "/",
            }

            with patch.object(smb, "_check_cifs_available", return_value=(True, "6.12", "6.12")):
                with patch.object(smb, "_check_target_not_mounted"):
                    with patch.object(smb, "_exec_mount", return_value=(0, "", "")):
                        with patch.object(smb, "_exec_umount", return_value=(0, "", "")):
                            with patch.object(smb.fs_backend, "_get_mount_info", return_value=observed):
                                with self.assertRaises(pipeline.BackupError) as raised:
                                    with smb:
                                        smb.preflight()

            self.assertEqual(raised.exception.code, "E_STORAGE")
            self.assertIn("source", raised.exception.public_message)
            self.assertFalse((Path(td) / "claude-r10b").exists())

    def test_preflight_normalizes_equivalent_smb_sources(self):
        with tempfile.TemporaryDirectory() as td:
            smb = self._make_smb(source="//NAS/backups", mount_point=td)
            observed = {
                "target": str(Path(td).resolve()),
                "source": "//nas/backups",
                "fstype": "cifs",
                "options": "rw,vers=3.1.1,seal,nosuid,nodev,noexec",
                "majmin": "0:42",
                "fsroot": "/",
            }
            with patch.object(smb, "_check_cifs_available", return_value=(True, "6.12", "6.12")):
                with patch.object(smb, "_check_target_not_mounted"):
                    with patch.object(smb, "_exec_mount", return_value=(0, "", "")):
                        with patch.object(smb, "_exec_umount", return_value=(0, "", "")):
                            with patch.object(smb.fs_backend, "_get_mount_info", return_value=observed):
                                smb.preflight()
                                smb.close()

            self.assertTrue((Path(td) / "claude-r10b" / "galera-backup-owner.json").is_file())


    def test_preflight_rejects_missing_observed_smb_security_option(self):
        with tempfile.TemporaryDirectory() as td:
            smb = self._make_smb(mount_point=td)
            observed = {
                "target": str(Path(td).resolve()),
                "source": "//nas/backups",
                "fstype": "cifs",
                "options": "rw,vers=3.1.1,nosuid,nodev,noexec",
                "majmin": "0:42",
                "fsroot": "/",
            }

            with patch.object(smb, "_check_cifs_available", return_value=(True, "6.12", "6.12")):
                with patch.object(smb, "_check_target_not_mounted"):
                    with patch.object(smb, "_exec_mount", return_value=(0, "", "")):
                        with patch.object(smb, "_exec_umount", return_value=(0, "", "")):
                            with patch.object(smb.fs_backend, "_get_mount_info", return_value=observed):
                                with self.assertRaises(pipeline.BackupError) as raised:
                                    with smb:
                                        smb.preflight()

            self.assertEqual(raised.exception.code, "E_STORAGE")
            self.assertIn("observed mount options", raised.exception.public_message)
            self.assertFalse((Path(td) / "claude-r10b").exists())

    def test_preflight_preserves_primary_failure_when_unmount_fails(self):
        with tempfile.TemporaryDirectory() as td:
            mount_path = Path(td)
            cluster_dir = mount_path / "claude-r10b"
            cluster_dir.mkdir()
            (cluster_dir / "galera-backup-owner.json").write_text(
                json.dumps({"format_version": 1, "cluster_name": "another-cluster"}),
                encoding="utf-8",
            )
            smb = self._make_smb(mount_point=mount_path)
            observed = {
                "target": str(mount_path.resolve()),
                "source": "//nas/backups",
                "fstype": "cifs",
                "options": "rw,vers=3.1.1,seal,nosuid,nodev,noexec",
                "majmin": "0:42",
                "fsroot": "/",
            }

            with patch.object(smb, "_check_cifs_available", return_value=(True, "6.12", "6.12")):
                with patch.object(smb, "_check_target_not_mounted"):
                    with patch.object(smb, "_exec_mount", return_value=(0, "", "")):
                        with patch.object(smb, "_exec_umount", return_value=(1, "", "target is busy")):
                            with patch.object(smb.fs_backend, "_get_mount_info", return_value=observed):
                                with self.assertRaises(pipeline.BackupError) as raised:
                                    smb.preflight()

            self.assertEqual(raised.exception.code, "E_OWNER_CONFLICT")
            self.assertIn("another cluster", raised.exception.public_message)
            self.assertIn("unmount failed", raised.exception.public_message)
            self.assertIsNone(smb._credentials_file)


    def test_unmount_failure_removes_credentials_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as td:
            smb = self._make_smb(mount_point=td)
            credentials_file = smb._create_credentials_file()
            smb._credentials_file = credentials_file
            smb._is_mounted = True

            with patch.object(smb, "_exec_umount", return_value=(1, "", "target is busy")):
                with self.assertRaises(pipeline.BackupError) as raised:
                    smb.close()

            self.assertEqual(raised.exception.code, "E_STORAGE")
            self.assertIn("unmount", raised.exception.public_message.lower())
            self.assertFalse(credentials_file.exists())
            self.assertIsNone(smb._credentials_file)

    def test_live_probe_round_trip_exercises_backend_contract(self):
        import importlib.util

        probe_path = Path(__file__).resolve().parents[1] / "live" / "probe-galera-backup-backends.py"
        spec = importlib.util.spec_from_file_location("probe_galera_backup_backends", str(probe_path))
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = root / "backup.tar.enc"
            checksum = root / "backup.sha256"
            metadata = root / "metadata.json"
            payload.write_bytes(b"non-empty-encrypted-payload")
            payload_sha = hashlib.sha256(payload.read_bytes()).hexdigest()
            checksum.write_text(f"{payload_sha}  backup.tar.enc\n", encoding="utf-8")
            metadata.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "cluster_name": "claude-r10b",
                        "created_unixtime": 1785360000,
                        "encrypted_sha256": payload_sha,
                        "plaintext_sha256": "0" * 64,
                        "encrypted_size_bytes": payload.stat().st_size,
                    }
                ),
                encoding="utf-8",
            )
            artifact = pipeline.ArtifactSet(
                backup_name="galera-claude-r10b-20260729-220000",
                payload_path=payload,
                checksum_path=checksum,
                metadata_path=metadata,
            )
            backend = MagicMock()
            backend.publish.return_value = pipeline.PublishedArtifact(
                backup_name=artifact.backup_name,
                prefix="published",
                encrypted_sha256=payload_sha,
                encrypted_size=payload.stat().st_size,
                unixtime=1785360000,
            )
            backend.fetch_latest.return_value = artifact
            backend.prune.return_value = 0

            result = probe.verify_backend_round_trip(
                backend,
                artifact,
                root / "fetched",
                retention_days=14,
            )

            self.assertEqual(result["backup_name"], artifact.backup_name)
            backend.preflight.assert_called_once_with()
            backend.publish.assert_called_once_with(artifact)
            backend.fetch_latest.assert_called_once_with(root / "fetched")
            backend.prune.assert_called_once()
            prune_args = backend.prune.call_args.args
            self.assertEqual(prune_args[1], 36500)

            tampered_payload = root / "tampered.tar.enc"
            tampered_bytes = bytearray(payload.read_bytes())
            tampered_bytes[0] ^= 1
            tampered_payload.write_bytes(bytes(tampered_bytes))
            backend.fetch_latest.return_value = pipeline.ArtifactSet(
                backup_name=artifact.backup_name,
                payload_path=tampered_payload,
                checksum_path=checksum,
                metadata_path=metadata,
            )
            with self.assertRaisesRegex(RuntimeError, "Fetched encrypted SHA-256"):
                probe.verify_backend_round_trip(
                    backend,
                    artifact,
                    root / "fetched-tampered",
                    retention_days=14,
                )

    def test_live_probe_resolves_restore_connection_from_inventory(self):
        import importlib.util

        probe_path = Path(__file__).resolve().parents[1] / "live" / "probe-galera-backup-backends.py"
        spec = importlib.util.spec_from_file_location("probe_galera_backup_inventory", str(probe_path))
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)

        with tempfile.TemporaryDirectory() as td:
            inventory_path = Path(td) / "inventory.yml"
            inventory_path.write_text(
                "all:\n"
                "  vars:\n"
                "    ansible_user: operator\n"
                "    ansible_ssh_private_key_file: keys/cluster\n"
                "  children:\n"
                "    restore:\n"
                "      hosts:\n"
                "        restore-a:\n"
                "          ansible_host: 192.0.2.55\n"
                "          ansible_port: 2222\n",
                encoding="utf-8",
            )

            connection = probe.resolve_remote_connection(
                "dynamic-cluster",
                str(inventory_path),
            )

        self.assertEqual(connection["host"], "192.0.2.55")
        self.assertEqual(connection["port"], 2222)
        self.assertEqual(connection["user"], "operator")
        self.assertEqual(connection["ssh_key"], "keys/cluster")
        self.assertEqual(connection["known_hosts"], str(inventory_path.parent / "known_hosts"))

    def test_probe_cifs_diagnostic_parsing_and_no_mount_verification(self):
        import importlib.util
        probe_path = Path(__file__).resolve().parents[1] / "live" / "probe-galera-backup-backends.py"
        spec = importlib.util.spec_from_file_location("probe_galera_backup_backends", str(probe_path))
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        msg = (
            "CIFS kernel module ('cifs.ko') is unavailable for running kernel "
            "6.12.0-211.16.1.el10_2.0.1.x86_64. Installed kernel with CIFS: "
            "6.12.0-211.39.1.el10_2.x86_64. Database host reboot required to boot matching kernel."
        )

        parsed = probe.parse_cifs_diagnostic(msg)
        self.assertEqual(parsed.get("running_kernel"), "6.12.0-211.16.1.el10_2.0.1.x86_64")
        self.assertEqual(parsed.get("installed_kernel"), "6.12.0-211.39.1.el10_2.x86_64")

        mock_backend = MagicMock()
        mock_backend._credentials_file = None
        mock_backend._is_mounted = False
        self.assertTrue(probe.verify_no_mount_performed(mock_backend))

        mock_backend._is_mounted = True
        self.assertFalse(probe.verify_no_mount_performed(mock_backend))

    def test_probe_preflight_exercises_production_error_path(self):
        import importlib.util

        probe_path = Path(__file__).resolve().parents[1] / "live" / "probe-galera-backup-backends.py"
        spec = importlib.util.spec_from_file_location("probe_galera_backup_preflight", str(probe_path))
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)

        backend = MagicMock()
        backend._credentials_file = None
        backend._is_mounted = False
        backend._check_cifs_available.return_value = (
            False,
            "6.12.0-running",
            "6.12.0-installed",
        )
        backend.preflight.side_effect = pipeline.BackupError(
            "E_CIFS_MODULE",
            "CIFS kernel module ('cifs.ko') is unavailable for running kernel "
            "6.12.0-running. Installed kernel with CIFS: 6.12.0-installed.",
        )
        fake_mod = MagicMock()
        fake_mod.SMBBackend.return_value = backend
        fake_mod.BackupError = pipeline.BackupError

        with patch.object(probe, "load_installed_runner", return_value=fake_mod):
            ok, details = probe.run_on_node_preflight("claude-r10b")

        self.assertTrue(ok)
        self.assertEqual(details["error_code"], "E_CIFS_MODULE")
        self.assertTrue(details["no_mount_performed"])
        backend.preflight.assert_called_once_with()

    def test_wrong_password_probe_always_closes_backend(self):
        import importlib.util

        probe_path = Path(__file__).resolve().parents[1] / "live" / "probe-galera-backup-backends.py"
        spec = importlib.util.spec_from_file_location("probe_galera_backup_wrong_password", str(probe_path))
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            secrets_path = root / "smb.env"
            secrets_path.write_text(
                "GALERA_BACKUP_TEST_SMB_USERNAME=test-user\n"
                "GALERA_BACKUP_TEST_SMB_PASSWORD=test-secret\n",
                encoding="utf-8",
            )
            backend = MagicMock()
            backend._credentials_file = None
            backend._is_mounted = False
            fake_mod = MagicMock()
            fake_mod.SMBBackend.return_value = backend
            fake_mod.BackupError = pipeline.BackupError

            with patch.object(probe, "load_installed_runner", return_value=fake_mod):
                ok, message = probe.run_on_node_full(
                    cluster_name="claude-r10b",
                    mode="wrong-password",
                    source="//nas/share",
                    mount_point=str(root / "mount"),
                    smb_secrets_path=secrets_path,
                )

        self.assertFalse(ok)
        self.assertIn("unexpectedly mounted", message)
        backend.close.assert_called_once_with()

    def test_probe_env_parser_preserves_unmatched_quotes(self):
        import importlib.util

        probe_path = Path(__file__).resolve().parents[1] / "live" / "probe-galera-backup-backends.py"
        spec = importlib.util.spec_from_file_location("probe_galera_backup_env", str(probe_path))
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)

        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / "secrets.env"
            env_file.write_text(
                'MATCHED="value"\nUNMATCHED="unterminated\n',
                encoding="utf-8",
            )
            parsed = probe.read_env_file(env_file)

        self.assertEqual(parsed["MATCHED"], "value")
        self.assertEqual(parsed["UNMATCHED"], '"unterminated')
if __name__ == "__main__":
    unittest.main()
