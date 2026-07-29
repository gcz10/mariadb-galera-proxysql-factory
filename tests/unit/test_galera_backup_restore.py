import os
import sys
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.unit.galera_backup_testlib import load_galera_backup_module


class GaleraBackupRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.mod = load_galera_backup_module()
        except Exception:
            cls.mod = None

    def setUp(self):
        if self.mod is None:
            self.skipTest("galera-backup executable not implemented yet")

    def test_restore_missing_confirm_fails(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg_path = td_path / "config.json"
            env_path = td_path / "secrets.env"

            cfg_data = {
                "format_version": 1,
                "cluster_name": "claude-r10b",
                "metric_cluster_label": "r10b-galera",
                "local_role": "restore",
                "scheduler_system_hostname": "gnode4",
                "galera_nodes_expected": 3,
                "mariadb_version": "11.4.12",
                "retention_days": 14,
                "flow_control_threshold_ns": 1000000000,
                "backend": {"type": "s3", "endpoint": "192.168.1.47:9000", "bucket": "r10b-galera-backups", "secure": False},
                "paths": {
                    "install_root": str(td_path),
                    "cluster_dir": str(td_path / "clusters" / "claude-r10b"),
                    "staging_root": str(td_path / "staging"),
                    "datadir": str(td_path / "datadir"),
                    "socket": str(td_path / "mysql.sock"),
                    "metric_file": str(td_path / "metrics.prom"),
                },
            }
            cfg_path.write_text(json.dumps(cfg_data))
            env_path.write_text('GALERA_BACKUP_ENCRYPTION_KEY="key"\nGALERA_BACKUP_S3_ACCESS_KEY="a"\nGALERA_BACKUP_S3_SECRET_KEY="s"\n')
            os.chmod(env_path, 0o600)

            with patch("socket.gethostname", return_value="rnode1"):
                with self.assertRaises(self.mod.BackupError) as ctx:
                    self.mod.run_restore(config_path=cfg_path, secrets_path=env_path, cluster_name="claude-r10b", confirm=False)
                self.assertEqual(ctx.exception.code, "E_RESTORE_CONFIRM")

    def test_restore_role_or_hostname_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg_path = td_path / "config.json"
            env_path = td_path / "secrets.env"

            # Case A: local_role is scheduler instead of restore
            cfg_data = {
                "format_version": 1,
                "cluster_name": "claude-r10b",
                "metric_cluster_label": "r10b-galera",
                "local_role": "scheduler",
                "scheduler_system_hostname": "gnode4",
                "galera_nodes_expected": 3,
                "mariadb_version": "11.4.12",
                "retention_days": 14,
                "flow_control_threshold_ns": 1000000000,
                "backend": {"type": "s3", "endpoint": "192.168.1.47:9000", "bucket": "r10b-galera-backups", "secure": False},
                "paths": {
                    "install_root": str(td_path),
                    "cluster_dir": str(td_path / "clusters" / "claude-r10b"),
                    "staging_root": str(td_path / "staging"),
                    "datadir": str(td_path / "datadir"),
                    "socket": str(td_path / "mysql.sock"),
                    "metric_file": str(td_path / "metrics.prom"),
                },
            }
            cfg_path.write_text(json.dumps(cfg_data))
            env_path.write_text('GALERA_BACKUP_ENCRYPTION_KEY="key"\nGALERA_BACKUP_S3_ACCESS_KEY="a"\nGALERA_BACKUP_S3_SECRET_KEY="s"\n')
            os.chmod(env_path, 0o600)

            with patch("socket.gethostname", return_value="rnode1"):
                with self.assertRaises(self.mod.BackupError) as ctx:
                    self.mod.run_restore(config_path=cfg_path, secrets_path=env_path, cluster_name="claude-r10b", confirm=True)
                self.assertEqual(ctx.exception.code, "E_RESTORE_CONFIRM")

            # Case B: hostname matches scheduler host gnode4
            cfg_data["local_role"] = "restore"
            cfg_path.write_text(json.dumps(cfg_data))
            with patch("socket.gethostname", return_value="gnode4"):
                with self.assertRaises(self.mod.BackupError) as ctx:
                    self.mod.run_restore(config_path=cfg_path, secrets_path=env_path, cluster_name="claude-r10b", confirm=True)
                self.assertEqual(ctx.exception.code, "E_RESTORE_CONFIRM")

    def test_tar_member_safety_validation(self):
        # Validate member check logic
        member_safe = MagicMock()
        member_safe.name = "var/lib/mysql/ibdata1"
        member_safe.isreg.return_value = True
        member_safe.isdir.return_value = False
        member_safe.issym.return_value = False
        member_safe.islnk.return_value = False
        member_safe.isfifo.return_value = False
        member_safe.ischr.return_value = False
        member_safe.isblk.return_value = False

        self.assertTrue(self.mod.is_safe_tar_member(member_safe))

        # Unsafe cases
        unsafe_names = ["/etc/passwd", "../etc/shadow", "foo/../../bar"]
        for name in unsafe_names:
            m = MagicMock()
            m.name = name
            m.isreg.return_value = True
            m.isdir.return_value = False
            m.issym.return_value = False
            m.islnk.return_value = False
            self.assertFalse(self.mod.is_safe_tar_member(m))

        # Symlink/FIFO unsafe
        m_sym = MagicMock()
        m_sym.name = "symlink_file"
        m_sym.isreg.return_value = False
        m_sym.isdir.return_value = False
        m_sym.issym.return_value = True
        self.assertFalse(self.mod.is_safe_tar_member(m_sym))

    def test_mariadb_version_compatibility(self):
        # Equal version is compatible
        self.assertTrue(self.mod.is_mariadb_version_compatible("11.4.12", "11.4.12"))
        # Older backup version is compatible
        self.assertTrue(self.mod.is_mariadb_version_compatible("10.6.18", "11.4.12"))
        # Newer backup version is incompatible
        self.assertFalse(self.mod.is_mariadb_version_compatible("11.5.1", "11.4.12"))


if __name__ == "__main__":
    unittest.main()
