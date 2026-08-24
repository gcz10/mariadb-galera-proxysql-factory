import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "roles" / "galera_backup" / "files"))

from galera_backup import pipeline  # noqa: E402


class GaleraBackupWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.writer_guard = patch.object(pipeline, "assert_scheduler_is_not_writer")
        self.writer_guard.start()

        def _stop_writer_guard():
            # Idempotent: the happy-path test stops this patch itself, and a
            # double stop() on the same patcher raises RuntimeError at teardown.
            try:
                self.writer_guard.stop()
            except RuntimeError:
                pass


        # Symetrycznie do straznika writera: elekcja donora odpytuje ProxySQL,
        # a te testy sprawdzaja kroki PO wyborze donora. Zwracany adres jest
        # obojetny — fixture nie ustawia `node_system_address`, wiec runner nie
        # wchodzi w galaz pomijania.
        self.donor_election = patch.object(
            pipeline, "elect_backup_donor", return_value="192.168.1.51"
        )
        self.donor_election.start()
        self.addCleanup(self.donor_election.stop)
        self.addCleanup(_stop_writer_guard)

    def test_node_that_is_not_the_elected_donor_skips_cleanly(self):
        """Dawniej: niezgodnosc hostname == blad. Teraz cron stoi na kazdym
        wezle Galery, wiec "to nie moja kolej" jest normalnym wynikiem, nie
        awaria — inaczej kazdy przebieg produkowalby falszywe porazki na
        dwoch z trzech wezlow."""
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
                "node_system_address": "192.168.1.52",
                "galera_nodes_expected": 3,
                "proxysql": {"admin_host": "192.168.1.44", "admin_port": 6032, "writer_hostgroup": 10, "backup_hostgroup": 20},
                "galera_nodes": ["192.168.1.51", "192.168.1.52", "192.168.1.53"],
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
            env_path.write_text('GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\nGALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\nGALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\nGALERA_BACKUP_PROXYSQL_STATS_USER="admin"\nGALERA_BACKUP_PROXYSQL_STATS_PASSWORD="proxysql_pass_999"\n')
            os.chmod(env_path, 0o600)

            # Donor to .51, a ten wezel to .52 — ma sie wycofac, nie pracowac.
            with patch("socket.gethostname", return_value="current-host"):
                with patch.object(pipeline, "get_storage_backend") as backend:
                    with patch.object(pipeline, "run_retention") as retention:
                        pipeline.run_backup(
                            config_path=cfg_path, secrets_path=env_path, cluster_name="claude-r10b"
                        )
                backend.assert_not_called()

            # Retencja nalezy do koordynatora, nie do donora: pominiecie backupu
            # NIE moze zatrzymac kasowania wygaslych kopii, inaczej kazde
            # przejecie backupu przez inny wezel wstrzymywaloby retencje.
            retention.assert_called_once()

            events = (Path(cfg_data["paths"]["cluster_dir"]) / "events.jsonl").read_text()
            self.assertIn("skipped.not_elected", events)
            self.assertIn("192.168.1.51", events)

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
                "proxysql": {"admin_host": "192.168.1.44", "admin_port": 6032, "writer_hostgroup": 10, "backup_hostgroup": 20},
                "galera_nodes": ["192.168.1.51", "192.168.1.52", "192.168.1.53"],
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
            env_path.write_text('GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\nGALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\nGALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\nGALERA_BACKUP_PROXYSQL_STATS_USER="admin"\nGALERA_BACKUP_PROXYSQL_STATS_PASSWORD="proxysql_pass_999"\n')
            os.chmod(env_path, 0o600)

            with patch("socket.gethostname", return_value="gnode4"):
                fake_backend = MagicMock()
                with patch.object(pipeline, "get_storage_backend", return_value=fake_backend):
                    with patch.object(pipeline, "query_galera_vars", return_value={"wsrep_local_state_comment": "Donor/Desynced"}):
                        with self.assertRaises(pipeline.BackupError) as ctx:
                            pipeline.run_backup(config_path=cfg_path, secrets_path=env_path, cluster_name="claude-r10b")
                        self.assertEqual(ctx.exception.code, "E_GALERA")
                        fake_backend.close.side_effect = pipeline.BackupError(
                            "E_STORAGE",
                            "SMB cleanup failed: unmount failed",
                        )
                        with self.assertRaises(pipeline.BackupError) as cleanup_ctx:
                            pipeline.run_backup(
                                config_path=cfg_path,
                                secrets_path=env_path,
                                cluster_name="claude-r10b",
                            )
                        self.assertEqual(cleanup_ctx.exception.code, "E_GALERA")
                        self.assertIn("not fully healthy", cleanup_ctx.exception.public_message)
                        self.assertIn("unmount failed", cleanup_ctx.exception.public_message)
                        state = json.loads(
                            (Path(cfg_data["paths"]["cluster_dir"]) / "state.json").read_text()
                        )
                        self.assertIn(
                            "unmount failed",
                            state["last_failure"]["error_message"],
                        )
                        self.assertIn(
                            "not fully healthy",
                            state["last_failure"]["error_message"],
                        )
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
                "proxysql": {"admin_host": "192.168.1.44", "admin_port": 6032, "writer_hostgroup": 10, "backup_hostgroup": 20},
                "galera_nodes": ["192.168.1.51", "192.168.1.52", "192.168.1.53"],
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
            env_path.write_text('GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\nGALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\nGALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\nGALERA_BACKUP_PROXYSQL_STATS_USER="admin"\nGALERA_BACKUP_PROXYSQL_STATS_PASSWORD="proxysql_pass_999"\n')
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
                # set_wsrep_desync(ON) sonduje wsrep_local_state — musi byc 4 (Synced),
                # inaczej runner NIE odsynchronizuje wezla (cudzy desync).
                {"wsrep_local_state": "4", "wsrep_local_state_comment": "Synced"},
                # wait_until_synced() po desync=OFF czeka na powrot do Synced.
                {"wsrep_local_state": "4", "wsrep_local_state_comment": "Synced"},
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
                with patch.object(pipeline, "query_galera_vars", side_effect=galera_vars_seq):
                    # Mock backend
                    fake_backend = MagicMock()
                    with patch.object(pipeline, "get_storage_backend", return_value=fake_backend):
                        with patch.object(pipeline, "perform_physical_backup") as mock_backup:
                            mock_backup.return_value = ("uuid-123", "456")
                            def fake_exec(cmd, env=None, cwd=None, timeout=None):
                                # If openssl output file is in cmd, create dummy file
                                for i, arg in enumerate(cmd):
                                    if arg == "-out" and i + 1 < len(cmd):
                                        Path(cmd[i+1]).write_bytes(b"dummy-encrypted-payload")
                                return (0, "", "")

                            with patch.object(pipeline.CommandRunner, "_exec", side_effect=fake_exec):
                                with patch("subprocess.Popen") as mock_popen:
                                    mock_proc = MagicMock()
                                    mock_proc.stdout.read.side_effect = [b"tar-data", b""]
                                    mock_proc.returncode = 0
                                    mock_proc.communicate.return_value = (b"", b"")
                                    mock_popen.return_value = mock_proc

                                    with self.assertRaises(pipeline.BackupError) as ctx:
                                        pipeline.run_backup(config_path=cfg_path, secrets_path=env_path, cluster_name="claude-r10b")
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
                "proxysql": {"admin_host": "192.168.1.44", "admin_port": 6032, "writer_hostgroup": 10, "backup_hostgroup": 20},
                "galera_nodes": ["192.168.1.51", "192.168.1.52", "192.168.1.53"],
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
            env_path.write_text('GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\nGALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\nGALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\nGALERA_BACKUP_PROXYSQL_STATS_USER="admin"\nGALERA_BACKUP_PROXYSQL_STATS_PASSWORD="proxysql_pass_999"\n')
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
            fake_backend.publish.return_value = pipeline.PublishedArtifact(
                backup_name="galera-claude-r10b-20260729-120000",
                prefix="p",
                encrypted_sha256="sha",
                encrypted_size=10,
                unixtime=1000,
            )

            # Mock tar and openssl execution. The writer guard is NOT patched in
            # this test, so its real `mariadb ... SELECT hostname` argv flows
            # through this same _exec patch and must be answered with a writer
            # that is not the scheduler (else the guard raises E_PROXYSQL).
            def fake_exec(cmd, env=None, cwd=None, timeout=None):
                for i, arg in enumerate(cmd):
                    if arg == "-out" and i + 1 < len(cmd):
                        Path(cmd[i+1]).write_bytes(b"dummy-encrypted-payload")
                        return (0, "", "")
                if cmd[:1] == ["mariadb"]:
                    return (0, "192.168.1.52\n", "")
                return (0, "", "")

            # Happy path exercises the REAL writer guard: stop the setUp patch so
            # assert_scheduler_is_not_writer runs and its argv reaches _exec.
            self.writer_guard.stop()
            with patch("socket.gethostname", return_value="gnode4"):
                with patch.object(pipeline, "query_galera_vars", return_value=galera_vars):
                    with patch.object(pipeline, "get_storage_backend", return_value=fake_backend):
                        with patch.object(pipeline, "perform_physical_backup", return_value=("uuid-1", "100")):
                            with patch.object(pipeline.CommandRunner, "_exec", side_effect=fake_exec) as mock_exec:
                                # Mock tar file creation
                                with patch("subprocess.Popen") as mock_popen:
                                    mock_proc = MagicMock()
                                    mock_proc.stdout.read.side_effect = [b"tar-data", b""]
                                    mock_proc.returncode = 0
                                    mock_proc.communicate.return_value = (b"", b"")
                                    mock_popen.return_value = mock_proc

                                    pipeline.run_backup(config_path=cfg_path, secrets_path=env_path, cluster_name="claude-r10b")

            # The writer guard's mariadb argv must have actually reached _exec.
            guard_calls = [
                c.args[0]
                for c in mock_exec.call_args_list
                if c.args and c.args[0][:1] == ["mariadb"]
            ]
            self.assertTrue(
                guard_calls,
                "writer guard mariadb argv never reached CommandRunner._exec",
            )
            guard_argv = guard_calls[0]
            self.assertIn("SELECT srv_host FROM stats_mysql_connection_pool", " ".join(guard_argv))
            u_idx = guard_argv.index("-u")
            self.assertEqual(guard_argv[u_idx + 1], "admin")

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

            # Backend publikacji stoi na poswiadczeniu lezacym na KAZDYM wezle
            # Galery, wiec sciezka backupu nie moze niczego kasowac. Retencja
            # ma wlasny backend i wlasny klucz — patrz
            # tests/unit/test_backup_delete_separation.py.
            fake_backend.prune.assert_not_called()

    def test_run_backup_cleanup_failure_does_not_downgrade_success(self):
        # Regresja: porazka porzadkowania (close backendu / usuniecie
        # workdir) PO zapisanym sukcesie zapisywala state.failure i metryke
        # last_run_success=0 — zweryfikowany, opublikowany backup uchodzil za
        # nieudany. Kontrakt: sukces zostaje, porazka porzadkowania dostaje
        # osobne zdarzenie cleanup.failure.
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg_path = td_path / "config.json"
            env_path = td_path / "secrets.env"
            cluster_dir = td_path / "clusters" / "claude-r10b"
            cfg_path.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "cluster_name": "claude-r10b",
                        "metric_cluster_label": "r10b-galera",
                        "local_role": "scheduler",
                        "scheduler_system_hostname": "gnode4",
                        "galera_nodes_expected": 3,
                        "proxysql": {"admin_host": "192.168.1.44", "admin_port": 6032, "writer_hostgroup": 10, "backup_hostgroup": 20},
                        "galera_nodes": ["192.168.1.51", "192.168.1.52", "192.168.1.53"],
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
                'GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\n'
                'GALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\n'
                'GALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\n'
                'GALERA_BACKUP_PROXYSQL_STATS_USER="admin"\n'
                'GALERA_BACKUP_PROXYSQL_STATS_PASSWORD="proxysql_pass_999"\n',
                encoding="utf-8",
            )
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
            fake_backend.publish.return_value = pipeline.PublishedArtifact(
                backup_name="galera-claude-r10b-20260729-120000",
                prefix="p",
                encrypted_sha256="sha",
                encrypted_size=10,
                unixtime=1000,
            )
            fake_backend.close.side_effect = pipeline.BackupError(
                "E_STORAGE",
                "SMB unmount failed",
            )

            def fake_exec(cmd, env=None, cwd=None, timeout=None):
                for index, argument in enumerate(cmd):
                    if argument == "-out" and index + 1 < len(cmd):
                        Path(cmd[index + 1]).write_bytes(b"dummy-encrypted-payload")
                return (0, "", "")

            with patch("socket.gethostname", return_value="gnode4"):
                with patch.object(pipeline, "query_galera_vars", return_value=galera_vars):
                    with patch.object(pipeline, "get_storage_backend", return_value=fake_backend):
                        with patch.object(pipeline, "perform_physical_backup", return_value=("uuid-1", "100")):
                            with patch.object(pipeline.CommandRunner, "_exec", side_effect=fake_exec):
                                with patch("subprocess.Popen") as mock_popen:
                                    mock_proc = MagicMock()
                                    mock_proc.stdout.read.side_effect = [b"tar-data", b""]
                                    mock_proc.returncode = 0
                                    mock_proc.communicate.return_value = (b"", b"")
                                    mock_popen.return_value = mock_proc

                                    # Close pada PO zapisanym sukcesie — przebieg
                                    # MUSI zakonczyc sie bez wyjatku.
                                    pipeline.run_backup(
                                        config_path=cfg_path,
                                        secrets_path=env_path,
                                        cluster_name="claude-r10b",
                                    )

            events = [
                json.loads(line)["event"]
                for line in (cluster_dir / "events.jsonl").read_text().splitlines()
            ]
            self.assertIn("state.success", events)
            self.assertIn("cleanup.failure", events)
            self.assertNotIn("state.failure", events)
            self.assertLess(events.index("state.success"), events.index("cleanup.failure"))

            state = json.loads((cluster_dir / "state.json").read_text())
            self.assertEqual(state["last_run"]["status"], "success")
            metric = (td_path / "metrics.prom").read_text()
            self.assertRegex(metric, r"galera_backup_last_run_success\{[^}]*\} 1")

    def test_run_backup_missing_flow_control_variable_fails(self):
        # Regresja: brak wsrep_flow_control_paused_ns w SHOW GLOBAL STATUS
        # domyslal sie 0, co wylaczalo zabezpieczenie flow control po cichu.
        # Brak zmiennej oznacza "nie wiemy" — jawny E_GALERA.
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg_path = td_path / "config.json"
            env_path = td_path / "secrets.env"
            cluster_dir = td_path / "clusters" / "claude-r10b"
            cfg_path.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "cluster_name": "claude-r10b",
                        "metric_cluster_label": "r10b-galera",
                        "local_role": "scheduler",
                        "scheduler_system_hostname": "gnode4",
                        "galera_nodes_expected": 3,
                        "proxysql": {"admin_host": "192.168.1.44", "admin_port": 6032, "writer_hostgroup": 10, "backup_hostgroup": 20},
                        "galera_nodes": ["192.168.1.51", "192.168.1.52", "192.168.1.53"],
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
                'GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\n'
                'GALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\n'
                'GALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\n'
                'GALERA_BACKUP_PROXYSQL_STATS_USER="admin"\n'
                'GALERA_BACKUP_PROXYSQL_STATS_PASSWORD="proxysql_pass_999"\n',
                encoding="utf-8",
            )
            os.chmod(env_path, 0o600)

            # Zdrowy stan klastra, ALE bez wsrep_flow_control_paused_ns.
            galera_vars = {
                "wsrep_local_state_comment": "Synced",
                "wsrep_cluster_status": "Primary",
                "wsrep_ready": "ON",
                "wsrep_connected": "ON",
                "wsrep_cluster_size": "3",
            }
            fake_backend = MagicMock()

            def fake_exec(cmd, env=None, cwd=None, timeout=None):
                for index, argument in enumerate(cmd):
                    if argument == "-out" and index + 1 < len(cmd):
                        Path(cmd[index + 1]).write_bytes(b"dummy-encrypted-payload")
                return (0, "", "")

            with patch("socket.gethostname", return_value="gnode4"):
                with patch.object(pipeline, "query_galera_vars", return_value=galera_vars):
                    with patch.object(pipeline, "get_storage_backend", return_value=fake_backend):
                        with patch.object(pipeline, "perform_physical_backup", return_value=("uuid-1", "100")):
                            with patch.object(pipeline.CommandRunner, "_exec", side_effect=fake_exec):
                                with patch("subprocess.Popen") as mock_popen:
                                    mock_proc = MagicMock()
                                    mock_proc.stdout.read.side_effect = [b"tar-data", b""]
                                    mock_proc.returncode = 0
                                    mock_proc.communicate.return_value = (b"", b"")
                                    mock_popen.return_value = mock_proc

                                    with self.assertRaises(pipeline.BackupError) as ctx:
                                        pipeline.run_backup(
                                            config_path=cfg_path,
                                            secrets_path=env_path,
                                            cluster_name="claude-r10b",
                                        )

            self.assertEqual(ctx.exception.code, "E_GALERA")
            self.assertIn("wsrep_flow_control_paused_ns", ctx.exception.public_message)
            # Odmowa nastepuje przed praca: zaden backup nie zostal zaczety.
            fake_backend.publish.assert_not_called()

    def test_success_cleanup_reports_both_phases_without_raising(self):
        # Wspolny epilog backupu i restore: porazka close ORAZ porazka usuniecia
        # workdir sa raportowane osobnymi zdarzeniami cleanup.failure, nigdy
        # wyjatkiem — po nich sukces zostaje juz zapisany.
        event_mgr = MagicMock()
        backend = MagicMock()
        backend.close.side_effect = pipeline.BackupError("E_STORAGE", "SMB unmount failed")

        with patch.object(
            pipeline,
            "remove_sensitive_work_dir",
            side_effect=pipeline.BackupError("E_STORAGE", "workdir removal denied"),
        ):
            pipeline._finalize_success_cleanup(
                event_mgr, backend, Path("/tmp/work"), "E_STORAGE"
            )

        emitted = event_mgr.emit.call_args_list
        self.assertEqual(
            [call.args[0] for call in emitted],
            ["cleanup.failure", "cleanup.failure"],
        )
        self.assertEqual(
            [call.args[1]["phase"] for call in emitted],
            ["backend.close", "workdir.remove"],
        )
        for call in emitted:
            self.assertEqual(call.args[1]["error_code"], "E_STORAGE")

if __name__ == "__main__":
    unittest.main()
