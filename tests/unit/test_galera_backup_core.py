import os
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.unit.galera_backup_testlib import load_galera_backup_module, WORKSPACE_ROOT


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


class TemplateContractTests(unittest.TestCase):
    def setUp(self):
        try:
            import jinja2
            self.jinja2 = jinja2
        except ImportError:
            self.skipTest("jinja2 not installed")

        self.templates_dir = WORKSPACE_ROOT / "roles" / "galera_backup" / "templates"
        self.env = self.jinja2.Environment(
            loader=self.jinja2.FileSystemLoader(str(self.templates_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters["to_json"] = lambda v: json.dumps(v)
        self.env.filters["regex_replace"] = lambda s, p, r: re.sub(p, r, str(s))

    def test_templates_rendered_contract(self):
        ctx = {
            "cluster": {
                "name": "claude-r10b",
                "monitoring": {"pmm": {"cluster_name": "r10b-galera"}},
            },
            "backup": {
                "destination": "s3",
                "full_backup_schedule": "0 2 * * *",
                "retention_days": 14,
                "flow_control_threshold_ns": 1000000000,
                "scheduler": {"host": "gnode4", "timezone": "UTC"},
                "s3": {"endpoint": "192.168.1.47:9000", "bucket": "r10b-galera-backups", "secure": False},
            },
            "galera": {"nodes_expected": 3},
            "lock": {"mariadb": {"version": "11.4.12"}},
            "galera_backup_local_role": "scheduler",
            "galera_backup_encryption_key": "enc_pass_123",
            "galera_backup_s3_access_key": "access_key_456",
            "galera_backup_s3_secret_key": "secret_key_789",
        }

        # 1. config.json.j2
        tmpl_config = self.env.get_template("config.json.j2")
        rendered_config = tmpl_config.render(ctx)
        self.assertNotIn("enc_pass_123", rendered_config)
        self.assertNotIn("secret_key_789", rendered_config)
        cfg_dict = json.loads(rendered_config)
        self.assertEqual(cfg_dict["cluster_name"], "claude-r10b")

        # 2. secrets.env.j2
        tmpl_secrets = self.env.get_template("secrets.env.j2")
        rendered_secrets = tmpl_secrets.render(ctx)
        self.assertIn('GALERA_BACKUP_ENCRYPTION_KEY="enc_pass_123"', rendered_secrets)
        self.assertIn('GALERA_BACKUP_S3_ACCESS_KEY="access_key_456"', rendered_secrets)
        self.assertIn('GALERA_BACKUP_S3_SECRET_KEY="secret_key_789"', rendered_secrets)

        # 3. cron.j2
        tmpl_cron = self.env.get_template("cron.j2")
        rendered_cron = tmpl_cron.render(ctx)
        self.assertIn("CRON_TZ=UTC", rendered_cron)
        self.assertIn("PATH=", rendered_cron)
        self.assertIn("root", rendered_cron)
        self.assertIn("systemd-cat", rendered_cron)
        self.assertIn("/opt/galera-backup/galera-backup backup claude-r10b", rendered_cron)
        self.assertNotIn("enc_pass_123", rendered_cron)

        # 4. minio-policy.json.j2
        tmpl_policy = self.env.get_template("minio-policy.json.j2")
        rendered_policy = tmpl_policy.render(ctx)
        policy_dict = json.loads(rendered_policy)
        self.assertEqual(policy_dict["Version"], "2012-10-17")
        resources = []
        for stmt in policy_dict["Statement"]:
            resources.extend(stmt["Resource"])
        self.assertIn("arn:aws:s3:::r10b-galera-backups", resources)
        self.assertIn("arn:aws:s3:::r10b-galera-backups/galera-claude-r10b-*", resources)
        self.assertIn("arn:aws:s3:::r10b-galera-backups/galera-backup-owner.json", resources)
        for res in resources:
            self.assertNotIn("*/*", res)
            self.assertNotIn("arn:aws:s3:::second-bucket", res)
class CutoverContractTests(unittest.TestCase):
    def test_restore_playbook_invokes_galera_backup_runner(self):
        restore_playbook = (WORKSPACE_ROOT / "playbooks" / "f10_restore.yml").read_text()
        self.assertIn("/opt/galera-backup/galera-backup", restore_playbook)
        self.assertIn("restore", restore_playbook)
        self.assertIn("--confirm", restore_playbook)
        self.assertNotIn("s3_object.py", restore_playbook)
        self.assertNotIn("openssl enc", restore_playbook)
        self.assertNotIn("mariadb-backup --copy-back", restore_playbook)

    def test_no_legacy_backup_references_in_repo(self):
        legacy_terms = [
            "backup-run.sh",
            "s3_object.py",
            "/var/lib/mariadb-backup-state",
            "isa_backup_last_success_unixtime",
        ]
        files_to_check = []
        for path in WORKSPACE_ROOT.rglob("*"):
            if path.is_file() and not any(part.startswith(".") for part in path.parts):
                if "tests/unit" in str(path):
                    continue
                files_to_check.append(path)

        for term in legacy_terms:
            matches = []
            for path in files_to_check:
                try:
                    content = path.read_text()
                    if term in content:
                        matches.append(str(path.relative_to(WORKSPACE_ROOT)))
                except Exception:
                    pass
            self.assertEqual(matches, [], f"Legacy term '{term}' still referenced in files: {matches}")

    def test_pmm_probe_expects_galera_backup_metrics(self):
        pmm_probe = (WORKSPACE_ROOT / "tests" / "lab" / "probe-pmm-native.py").read_text()
        for metric in [
            "galera_backup_last_success_unixtime",
            "galera_backup_last_failure_unixtime",
            "galera_backup_last_run_success",
            "galera_backup_last_size_bytes",
            "galera_backup_last_duration_seconds",
        ]:
            self.assertIn(metric, pmm_probe)
        self.assertNotIn("isa_backup_last_success_unixtime", pmm_probe)

    def test_alerts_playbook_includes_galera_backup_rules(self):
        alerts_playbook = (WORKSPACE_ROOT / "playbooks" / "f15_alerts.yml").read_text()
        self.assertIn("galera_backup_last_success_unixtime", alerts_playbook)
        self.assertIn("galera_backup_last_run_success", alerts_playbook)
        self.assertIn("Backup run failed", alerts_playbook)
        self.assertIn("Backup freshness stale", alerts_playbook)
        self.assertNotIn("isa_backup_last_success_unixtime", alerts_playbook)
if __name__ == "__main__":
    unittest.main()
