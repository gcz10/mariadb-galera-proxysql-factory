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

            # Case C: a concurrent restore is rejected before backend mutation
            with patch("socket.gethostname", return_value="rnode1"):
                with patch.object(
                    self.mod.LockManager,
                    "acquire",
                    side_effect=self.mod.BackupError("E_LOCKED", "already running"),
                ):
                    with self.assertRaises(self.mod.BackupError) as ctx:
                        self.mod.run_restore(
                            config_path=cfg_path,
                            secrets_path=env_path,
                            cluster_name="claude-r10b",
                            confirm=True,
                        )
            self.assertEqual(ctx.exception.code, "E_LOCKED")
            state = json.loads(
                (Path(cfg_data["paths"]["cluster_dir"]) / "state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["last_run"]["command"], "restore")
            self.assertEqual(state["last_run"]["status"], "locked")

    def test_restore_preflight_failure_is_recorded_and_backend_is_closed(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cluster_dir = td_path / "clusters" / "claude-r10b"
            cfg_path = td_path / "config.json"
            env_path = td_path / "secrets.env"
            cfg_path.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "cluster_name": "claude-r10b",
                        "metric_cluster_label": "r10b-galera",
                        "local_role": "restore",
                        "scheduler_system_hostname": "gnode4",
                        "galera_nodes_expected": 3,
                        "mariadb_version": "11.4.12",
                        "retention_days": 14,
                        "flow_control_threshold_ns": 1000000000,
                        "backend": {
                            "type": "s3",
                            "endpoint": "192.168.1.47:9000",
                            "bucket": "r10b-galera-backups",
                            "secure": False,
                        },
                        "paths": {
                            "install_root": str(td_path),
                            "cluster_dir": str(cluster_dir),
                            "staging_root": str(td_path / "staging"),
                            "datadir": str(td_path / "datadir"),
                            "socket": str(td_path / "mysql.sock"),
                            "metric_file": str(td_path / "metrics.prom"),
                        },
                    }
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                'GALERA_BACKUP_ENCRYPTION_KEY="encryption_fixture_value"\n'
                'GALERA_BACKUP_S3_ACCESS_KEY="access_fixture_value"\n'
                'GALERA_BACKUP_S3_SECRET_KEY="secret_fixture_value"\n',
                encoding="utf-8",
            )
            os.chmod(env_path, 0o600)

            backend = MagicMock()
            backend.preflight.side_effect = self.mod.BackupError(
                "E_OWNER_CONFLICT",
                "Backend belongs to another cluster",
            )
            with patch("socket.gethostname", return_value="rnode1"):
                with patch.object(self.mod, "get_storage_backend", return_value=backend):
                    with self.assertRaises(self.mod.BackupError):
                        self.mod.run_restore(
                            config_path=cfg_path,
                            secrets_path=env_path,
                            cluster_name="claude-r10b",
                            confirm=True,
                        )

            backend.close.assert_called()
            state = json.loads((cluster_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["last_failure"]["command"], "restore")
            self.assertEqual(state["last_failure"]["error_code"], "E_OWNER_CONFLICT")

            backend.close.side_effect = self.mod.BackupError(
                "E_STORAGE",
                "SMB cleanup failed: unmount failed",
            )
            with patch("socket.gethostname", return_value="rnode1"):
                with patch.object(self.mod, "get_storage_backend", return_value=backend):
                    with self.assertRaises(self.mod.BackupError) as cleanup_ctx:
                        self.mod.run_restore(
                            config_path=cfg_path,
                            secrets_path=env_path,
                            cluster_name="claude-r10b",
                            confirm=True,
                        )
            self.assertEqual(cleanup_ctx.exception.code, "E_OWNER_CONFLICT")
            self.assertIn("another cluster", cleanup_ctx.exception.public_message)
            self.assertIn("unmount failed", cleanup_ctx.exception.public_message)
            state = json.loads((cluster_dir / "state.json").read_text(encoding="utf-8"))
            self.assertIn("unmount failed", state["last_failure"]["error_message"])
            self.assertIn("another cluster", state["last_failure"]["error_message"])

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
        # Malformed or incomplete versions must fail closed.
        self.assertFalse(self.mod.is_mariadb_version_compatible("not-a-version", "11.4.12"))
        self.assertFalse(self.mod.is_mariadb_version_compatible("11", "11.4.12"))
        self.assertFalse(self.mod.is_mariadb_version_compatible("11.4.12", "invalid"))


    def test_restore_verification_accepts_empty_tables_after_full_schema_check(self):
        runner = MagicMock()
        runner.run.side_effect = [
            (0, "all tables OK", ""),
            (0, "mysql\ninformation_schema\napp\n", ""),
            (0, "empty_table\n", ""),
            (0, "0\n", ""),
        ]

        result = self.mod.verify_restored_database(Path("/run/mariadb.sock"), runner)

        self.assertEqual(result, (1, 1, 0))
        self.assertIn("--all-databases", runner.run.call_args_list[0].args[0])

    def test_restore_verification_fails_when_full_schema_check_fails(self):
        runner = MagicMock()
        runner.run.return_value = (1, "", "table corruption")

        with self.assertRaises(self.mod.BackupError) as ctx:
            self.mod.verify_restored_database(Path("/run/mariadb.sock"), runner)

        self.assertEqual(ctx.exception.code, "E_INTEGRITY")
        self.assertIn("table corruption", ctx.exception.public_message)

    def test_clear_datadir_delete_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            datadir = Path(td) / "mysql"
            blocked_directory = datadir / "blocked"
            blocked_directory.mkdir(parents=True)
            (blocked_directory / "ibdata").write_text("data", encoding="utf-8")

            with patch.object(
                self.mod.shutil,
                "rmtree",
                side_effect=PermissionError("deletion denied"),
            ):
                with self.assertRaises(self.mod.BackupError) as ctx:
                    self.mod.clear_datadir(datadir)

            self.assertEqual(ctx.exception.code, "E_INTEGRITY")
            self.assertTrue(blocked_directory.exists())

    # Regression: a restore drill hung for 50 minutes because the teardown asked
    # `mariadb-admin shutdown` to stop the mariadbd this process owns. That client
    # waits for the server PID to leave the process table, while the PID stays
    # <defunct> precisely because the runner is blocked on the client — only the
    # runner can reap it. The teardown must therefore signal and reap directly.
    def test_standalone_teardown_signals_and_reaps_without_external_client(self):
        events = MagicMock()
        server_proc = MagicMock()
        server_proc.poll.return_value = None
        server_proc.wait.return_value = 0

        self.mod.stop_standalone_server(server_proc, events)

        server_proc.terminate.assert_called_once_with()
        server_proc.wait.assert_called_once_with(timeout=60)
        server_proc.kill.assert_not_called()
        events.emit.assert_not_called()

    def test_standalone_teardown_reaps_already_exited_server_without_signalling(self):
        events = MagicMock()
        server_proc = MagicMock()
        server_proc.poll.return_value = 0
        server_proc.wait.return_value = 0

        self.mod.stop_standalone_server(server_proc, events)

        server_proc.terminate.assert_not_called()
        server_proc.wait.assert_called_once_with(timeout=60)
        events.emit.assert_not_called()

    def test_standalone_teardown_kills_and_records_when_sigterm_ignored(self):
        events = MagicMock()
        server_proc = MagicMock()
        server_proc.poll.return_value = None
        server_proc.wait.side_effect = [
            self.mod.subprocess.TimeoutExpired(cmd="mariadbd", timeout=60),
            -9,
        ]

        self.mod.stop_standalone_server(server_proc, events)

        server_proc.terminate.assert_called_once_with()
        server_proc.kill.assert_called_once_with()
        emitted = [call.args[0] for call in events.emit.call_args_list]
        self.assertEqual(emitted, ["restore.shutdown_failure"])

    def test_standalone_teardown_records_nonzero_exit_without_raising(self):
        events = MagicMock()
        server_proc = MagicMock()
        server_proc.poll.return_value = None
        server_proc.wait.return_value = 3

        self.mod.stop_standalone_server(server_proc, events)

        events.emit.assert_called_once()
        self.assertEqual(events.emit.call_args.args[0], "restore.shutdown_failure")
        self.assertIn("exit 3", events.emit.call_args.args[1]["message"])

if __name__ == "__main__":
    unittest.main()
