import os
import sys
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.unit.galera_backup_testlib import load_galera_backup_module


class GaleraBackupWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.mod = load_galera_backup_module()
        except Exception:
            cls.mod = None

    def setUp(self):
        if self.mod is None:
            self.skipTest("galera-backup executable not implemented yet")

    def test_run_backup_hostname_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg_path = td_path / "config.json"
            env_path = td_path / "secrets.env"

            cfg_data = {
                "format_version": 1,
                "cluster_name": "claude-r10b",
                "metric_cluster_label": "r10b-galera",
                "local_role": "scheduler",
                "scheduler_system_hostname": "different-host",
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
            env_path.write_text('GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\nGALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\nGALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\n')
            os.chmod(env_path, 0o600)

            with patch("socket.gethostname", return_value="current-host"):
                with self.assertRaises(self.mod.BackupError) as ctx:
                    self.mod.run_backup(config_path=cfg_path, secrets_path=env_path, cluster_name="claude-r10b")
                self.assertEqual(ctx.exception.code, "E_GALERA")

    def test_run_backup_galera_unhealthy_fails(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg_path = td_path / "config.json"
            env_path = td_path / "secrets.env"

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
            env_path.write_text('GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\nGALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\nGALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\n')
            os.chmod(env_path, 0o600)

            with patch("socket.gethostname", return_value="gnode4"):
                fake_backend = MagicMock()
                with patch.object(self.mod, "get_storage_backend", return_value=fake_backend):
                    with patch.object(self.mod, "query_galera_vars", return_value={"wsrep_local_state_comment": "Donor/Desynced"}):
                        with self.assertRaises(self.mod.BackupError) as ctx:
                            self.mod.run_backup(config_path=cfg_path, secrets_path=env_path, cluster_name="claude-r10b")
                        self.assertEqual(ctx.exception.code, "E_GALERA")
    def test_run_backup_flow_control_excess_fails(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg_path = td_path / "config.json"
            env_path = td_path / "secrets.env"

            cfg_data = {
                "format_version": 1,
                "cluster_name": "claude-r10b",
                "metric_cluster_label": "r10b-galera",
                "local_role": "scheduler",
                "scheduler_system_hostname": "gnode4",
                "galera_nodes_expected": 3,
                "mariadb_version": "11.4.12",
                "retention_days": 14,
                "flow_control_threshold_ns": 100,  # low threshold
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
            env_path.write_text('GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\nGALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\nGALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\n')
            os.chmod(env_path, 0o600)

            galera_vars_seq = [
                # Initial preflight: healthy
                {
                    "wsrep_local_state_comment": "Synced",
                    "wsrep_cluster_status": "Primary",
                    "wsrep_ready": "ON",
                    "wsrep_connected": "ON",
                    "wsrep_cluster_size": "3",
                    "wsrep_flow_control_paused_ns": "1000",
                },
                # Final check after backup: flow control paused ns jumped by 500 (threshold was 100)
                {
                    "wsrep_local_state_comment": "Synced",
                    "wsrep_cluster_status": "Primary",
                    "wsrep_ready": "ON",
                    "wsrep_connected": "ON",
                    "wsrep_cluster_size": "3",
                    "wsrep_flow_control_paused_ns": "1500",
                },
            ]

            with patch("socket.gethostname", return_value="gnode4"):
                with patch.object(self.mod, "query_galera_vars", side_effect=galera_vars_seq):
                    # Mock backend
                    fake_backend = MagicMock()
                    with patch.object(self.mod, "get_storage_backend", return_value=fake_backend):
                        with patch.object(self.mod, "perform_physical_backup") as mock_backup:
                            mock_backup.return_value = ("uuid-123", "456")
                            def fake_exec(cmd, env=None, cwd=None, timeout=None):
                                # If openssl output file is in cmd, create dummy file
                                for i, arg in enumerate(cmd):
                                    if arg == "-out" and i + 1 < len(cmd):
                                        Path(cmd[i+1]).write_bytes(b"dummy-encrypted-payload")
                                return (0, "", "")

                            with patch.object(self.mod.CommandRunner, "_exec", side_effect=fake_exec):
                                with patch("subprocess.Popen") as mock_popen:
                                    mock_proc = MagicMock()
                                    mock_proc.stdout.read.side_effect = [b"tar-data", b""]
                                    mock_proc.returncode = 0
                                    mock_popen.return_value = mock_proc

                                    with self.assertRaises(self.mod.BackupError) as ctx:
                                        self.mod.run_backup(config_path=cfg_path, secrets_path=env_path, cluster_name="claude-r10b")
                                    self.assertEqual(ctx.exception.code, "E_FLOW_CONTROL")
                            # Verify publication was NOT called due to flow control excess
                            self.assertEqual(fake_backend.publish.call_count, 0)

    def test_run_backup_event_ordering(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg_path = td_path / "config.json"
            env_path = td_path / "secrets.env"
            cluster_dir = td_path / "clusters" / "claude-r10b"

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
                    "cluster_dir": str(cluster_dir),
                    "staging_root": str(td_path / "staging"),
                    "datadir": str(td_path / "datadir"),
                    "socket": str(td_path / "mysql.sock"),
                    "metric_file": str(td_path / "metrics.prom"),
                },
            }
            cfg_path.write_text(json.dumps(cfg_data))
            env_path.write_text('GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\nGALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\nGALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\n')
            os.chmod(env_path, 0o600)

            galera_vars = {
                "wsrep_local_state_comment": "Synced",
                "wsrep_cluster_status": "Primary",
                "wsrep_ready": "ON",
                "wsrep_connected": "ON",
                "wsrep_cluster_size": "3",
                "wsrep_flow_control_paused_ns": "1000",
            }

            fake_backend = MagicMock()
            fake_backend.publish.return_value = self.mod.PublishedArtifact(
                backup_name="galera-claude-r10b-20260729-120000",
                prefix="p",
                encrypted_sha256="sha",
                encrypted_size=10,
                unixtime=1000,
            )

            # Mock tar and openssl execution
            def fake_exec(cmd, env=None, cwd=None, timeout=None):
                for i, arg in enumerate(cmd):
                    if arg == "-out" and i + 1 < len(cmd):
                        Path(cmd[i+1]).write_bytes(b"dummy-encrypted-payload")
                return (0, "", "")
            with patch("socket.gethostname", return_value="gnode4"):
                with patch.object(self.mod, "query_galera_vars", return_value=galera_vars):
                    with patch.object(self.mod, "get_storage_backend", return_value=fake_backend):
                        with patch.object(self.mod, "perform_physical_backup", return_value=("uuid-1", "100")):
                            with patch.object(self.mod.CommandRunner, "_exec", side_effect=fake_exec):
                                # Mock tar file creation
                                with patch("subprocess.Popen") as mock_popen:
                                    mock_proc = MagicMock()
                                    mock_proc.stdout.read.side_effect = [b"tar-data", b""]
                                    mock_proc.returncode = 0
                                    mock_popen.return_value = mock_proc

                                    self.mod.run_backup(config_path=cfg_path, secrets_path=env_path, cluster_name="claude-r10b")

            events_file = cluster_dir / "events.jsonl"
            self.assertTrue(events_file.exists())
            lines = events_file.read_text().splitlines()
            events = [json.loads(l)["event"] for l in lines]

            self.assertIn("backend.preflight", events)
            self.assertIn("mariadb-backup.backup", events)
            self.assertIn("backend.verify", events)
            self.assertIn("state.success", events)

            self.assertLess(events.index("backend.preflight"), events.index("mariadb-backup.backup"))
            self.assertLess(events.index("backend.verify"), events.index("state.success"))

if __name__ == "__main__":
    unittest.main()
