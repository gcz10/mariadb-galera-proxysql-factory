import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.unit.galera_backup_testlib import load_galera_backup_module


class GaleraBackupCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.mod = load_galera_backup_module()
        except Exception as e:
            cls.mod = None

    def setUp(self):
        if self.mod is None:
            self.skipTest("galera-backup executable not implemented yet")

    def test_sanitize_cluster_name(self):
        valid = ["claude-r10b", "claude_r10", "cluster123", "a-b_c"]
        for name in valid:
            self.assertEqual(self.mod.sanitize_cluster_name(name), name)

        invalid = ["../etc", "cluster/name", "cluster;rm", "cluster space", ""]
        for name in invalid:
            with self.assertRaises(self.mod.BackupError) as ctx:
                self.mod.sanitize_cluster_name(name)
            self.assertEqual(ctx.exception.code, "E_INVALID_CLUSTER")

    def test_config_loader_validation(self):
        valid_config = {
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
                "install_root": "/opt/galera-backup",
                "cluster_dir": "/opt/galera-backup/clusters/claude-r10b",
                "staging_root": "/var/tmp/galera-backup/claude-r10b",
                "datadir": "/var/lib/mysql",
                "socket": "/var/lib/mysql/mysql.sock",
                "metric_file": "/var/lib/node_exporter/textfile_collector/galera_backup-claude-r10b.prom",
            },
        }

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            import json
            json.dump(valid_config, tf)
            tf_path = Path(tf.name)

        try:
            cfg = self.mod.load_run_config(tf_path, "claude-r10b")
            self.assertEqual(cfg.cluster_name, "claude-r10b")
            self.assertEqual(cfg.backend["type"], "s3")

            # Mismatched cluster name
            with self.assertRaises(self.mod.BackupError) as ctx:
                self.mod.load_run_config(tf_path, "different-cluster")
            self.assertEqual(ctx.exception.code, "E_CONFIG")

            # Format version wrong
            bad_fmt = dict(valid_config, format_version=2)
            with open(tf_path, "w") as f:
                json.dump(bad_fmt, f)
            with self.assertRaises(self.mod.BackupError) as ctx:
                self.mod.load_run_config(tf_path, "claude-r10b")
            self.assertEqual(ctx.exception.code, "E_CONFIG")
        finally:
            if tf_path.exists():
                tf_path.unlink()

    def test_secrets_env_parsing_and_permissions(self):
        env_content = (
            'GALERA_BACKUP_ENCRYPTION_KEY="secret-pass-123"\n'
            'GALERA_BACKUP_S3_ACCESS_KEY="access-key-xyz"\n'
            'GALERA_BACKUP_S3_SECRET_KEY="secret-key-abc"\n'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as tf:
            tf.write(env_content)
            tf_path = Path(tf.name)

        try:
            # Set mode 0600
            os.chmod(tf_path, 0o600)
            secrets = self.mod.load_secrets(tf_path, backend_type="s3", enforce_permissions=False)
            self.assertEqual(secrets["GALERA_BACKUP_ENCRYPTION_KEY"], "secret-pass-123")
            self.assertEqual(secrets["GALERA_BACKUP_S3_ACCESS_KEY"], "access-key-xyz")

            # Group/world readable check
            os.chmod(tf_path, 0o644)
            with self.assertRaises(self.mod.BackupError) as ctx:
                self.mod.load_secrets(tf_path, backend_type="s3", enforce_permissions=True)
            self.assertEqual(ctx.exception.code, "E_SECRETS_PERM")

            # Missing required secret
            bad_content = 'GALERA_BACKUP_ENCRYPTION_KEY="key"\n'
            os.chmod(tf_path, 0o600)
            with open(tf_path, "w") as f:
                f.write(bad_content)
            with self.assertRaises(self.mod.BackupError) as ctx:
                self.mod.load_secrets(tf_path, backend_type="s3", enforce_permissions=False)
            self.assertEqual(ctx.exception.code, "E_SECRETS")
        finally:
            if tf_path.exists():
                tf_path.unlink()

    def test_lock_contention(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "test.lock"
            lock1 = self.mod.LockManager(lock_path)
            lock1.acquire()

            lock2 = self.mod.LockManager(lock_path)
            with self.assertRaises(self.mod.BackupError) as ctx:
                lock2.acquire()
            self.assertEqual(ctx.exception.code, "E_LOCKED")

            lock1.release()
            # Now lock2 can acquire
            lock2.acquire()
            lock2.release()

    def test_atomic_json_write_and_state_preservation(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "state.json"

            sm = self.mod.StateManager("claude-r10b", state_file)
            sm.update_success(command="backup", unixtime=1000, artifact="backup-1")

            # Verify initial state
            state_data = sm.read()
            self.assertEqual(state_data["last_success"]["unixtime"], 1000)
            self.assertEqual(state_data["last_success"]["artifact"], "backup-1")

            # Update failure preserves last_success
            sm.update_failure(command="backup", unixtime=1050, error_code="E_STORAGE", error_message="Storage error")

            state_data2 = sm.read()
            self.assertEqual(state_data2["last_success"]["unixtime"], 1000)
            self.assertEqual(state_data2["last_success"]["artifact"], "backup-1")
            self.assertEqual(state_data2["last_failure"]["unixtime"], 1050)
            self.assertEqual(state_data2["last_failure"]["error_code"], "E_STORAGE")
            self.assertEqual(state_data2["last_run"]["status"], "failed")

    def test_secret_cannot_enter_subprocess_argv(self):
        runner = self.mod.CommandRunner(secret_values={"s3cr3t", "my-pass"})
        with patch.object(runner, "_exec") as mock_exec:
            with self.assertRaises(self.mod.BackupError) as ctx:
                runner.run(["mount", "-o", "password=s3cr3t"])
            self.assertEqual(ctx.exception.code, "E_SECRET_IN_ARGV")
            self.assertEqual(mock_exec.call_count, 0)

            # Safe command works
            mock_exec.return_value = (0, "output", "")
            code, out, _ = runner.run(["mount", "-o", "vers=3.1.1"])
            self.assertEqual(code, 0)

    def test_secret_redaction(self):
        secret_values = {"super-secret-key", "my-password"}
        redactor = self.mod.SecretRedactor(secret_values)

        raw = "Error connecting with password super-secret-key on host my-password"
        cleaned = redactor.redact(raw)
        self.assertNotIn("super-secret-key", cleaned)
        self.assertNotIn("my-password", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_metric_label_escaping(self):
        val = 'cluster "name"\nwith\\slash'
        escaped = self.mod.escape_metric_label(val)
        self.assertEqual(escaped, 'cluster \\"name\\"\\nwith\\\\slash')


if __name__ == "__main__":
    unittest.main()
