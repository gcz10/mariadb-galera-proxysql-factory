#!/usr/bin/env python3
"""Porazka backupu MUSI zostawic po sobie sygnal — desync zdjety i metryka czerwona.

Dwa defekty zmierzone 2026-09-14, oba w sciezce bledu runnera:

1. `backup.py` wlaczal `wsrep_desync=ON`, a dopiero POTEM wchodzil w `try/finally`
   odpowiedzialny za zdjecie desyncu — emisja zdarzenia `galera.desync` stala
   przed `try`. Pelny dysk na katalogu zdarzen (`EventManager.emit` rzuca
   OSError) omijal sprzatanie: donor zostawal z `wsrep_desync=ON`, ProxySQL
   trzymal go poza ruchem, a kazda nastepna brama zdrowia odrzucala caly klaster
   az do recznego OFF.

2. Galaz obslugi `E_STATE` ponownie czytala ten sam nieczytelny `state.json`,
   wiec drugi `E_STATE` zastępowal oryginalna diagnostyke, a zapis metryki —
   stojacy za odczytem — nigdy nie dochodzil do skutku. Dashboard zostawal
   z zeszlonocnym `last_run_success=1` mimo nieudanego przebiegu.

Oba testy przechodza PRAWDZIWA sciezke `run_backup` (nie kopie logiki): atrapy
obejmuja wylacznie I/O na zewnatrz procesu.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "roles" / "galera_backup" / "files"))

from galera_backup import backup, pipeline  # noqa: E402

GALERA_VARS = {
    "wsrep_local_state_comment": "Synced",
    "wsrep_cluster_status": "Primary",
    "wsrep_ready": "ON",
    "wsrep_connected": "ON",
    "wsrep_cluster_size": "3",
    "wsrep_flow_control_paused_ns": "1000",
}


class _Fixture:
    """Katalog klastra + config + sekrety, wystarczajace do wejscia w run_backup."""

    def __init__(self, td):
        self.td = Path(td)
        self.cluster_dir = self.td / "clusters" / "probe-r10"
        self.config_path = self.td / "config.json"
        self.secrets_path = self.td / "secrets.env"
        self.metric_file = self.td / "metrics.prom"
        self.config_path.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "cluster_name": "probe-r10",
                    "metric_cluster_label": "r10-galera",
                    "local_role": "scheduler",
                    "scheduler_system_hostname": "gnode1",
                    "node_system_address": "192.0.2.11",
                    "galera_nodes_expected": 3,
                    "proxysql": {
                        "admin_host": "192.0.2.21",
                        "admin_port": 6032,
                        "writer_hostgroup": 10,
                        "backup_hostgroup": 20,
                    },
                    "galera_nodes": ["192.0.2.11", "192.0.2.12", "192.0.2.13"],
                    "mariadb_version": "11.4.13",
                    "retention_days": 14,
                    "flow_control_threshold_ns": 1000000000,
                    "backend": {
                        "type": "s3",
                        "endpoint": "192.0.2.30:9000",
                        "bucket": "probe-r10-backups",
                        "secure": False,
                    },
                    "paths": {
                        "install_root": str(self.td),
                        "cluster_dir": str(self.cluster_dir),
                        "staging_root": str(self.td / "staging"),
                        "datadir": str(self.td / "datadir"),
                        "socket": str(self.td / "mysql.sock"),
                        "metric_file": str(self.metric_file),
                    },
                }
            ),
            encoding="utf-8",
        )
        self.secrets_path.write_text(
            'GALERA_BACKUP_ENCRYPTION_KEY="enc_key_probe"\n'
            'GALERA_BACKUP_S3_ACCESS_KEY="s3_access_probe"\n'
            'GALERA_BACKUP_S3_SECRET_KEY="s3_secret_probe"\n'
            'GALERA_BACKUP_PROXYSQL_STATS_USER="stats"\n'
            'GALERA_BACKUP_PROXYSQL_STATS_PASSWORD="stats_probe_pass"\n',
            encoding="utf-8",
        )
        os.chmod(self.secrets_path, 0o600)
        self.cluster_dir.mkdir(parents=True, exist_ok=True)

    def run(self):
        return backup.run_backup(
            config_path=self.config_path,
            secrets_path=self.secrets_path,
            cluster_name="probe-r10",
        )


def _stub_backend():
    """Atrapa backendu: `preflight` nie moze dotknac sieci.

    Bez tego `run_backup` buduje prawdziwego klienta S3 i `preflight()` wisi na
    polaczeniu do nieistniejacego endpointu — test mierzylby timeout sieci, a nie
    sciezke bledu runnera.
    """
    backend = MagicMock()
    backend.preflight.return_value = None
    backend.publish.return_value = pipeline.PublishedArtifact(
        backup_name="galera-probe-r10-20260914-000000",
        prefix="p",
        encrypted_sha256="sha",
        encrypted_size=1,
        unixtime=1,
    )
    return backend


class DesyncAlwaysClearedTests(unittest.TestCase):
    """Porazka sinka zdarzen nie moze zostawic wezla odsynchronizowanego."""

    def test_event_sink_failure_still_disables_desync(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = _Fixture(td)
            desync_calls = []

            def fake_set_desync(socket_path, runner, enable):
                desync_calls.append(enable)
                return True

            original_emit = backup.EventManager.emit

            def emit_or_fail(self, event, data):
                if event == "galera.desync":
                    # Pelny dysk na katalogu zdarzen — dokladnie ta awaria,
                    # ktora omijala `try/finally`.
                    raise OSError(28, "No space left on device")
                return original_emit(self, event, data)

            with patch.object(backup, "elect_backup_donor", return_value="192.0.2.11"):
                with patch.object(backup, "assert_scheduler_is_not_writer"):
                    with patch.object(backup, "query_galera_vars", return_value=GALERA_VARS):
                        with patch.object(backup, "get_storage_backend", return_value=_stub_backend()):
                            with patch.object(backup, "set_wsrep_desync", fake_set_desync):
                                with patch.object(backup, "wait_until_synced"):
                                    with patch.object(backup.EventManager, "emit", emit_or_fail):
                                        with self.assertRaises(OSError):
                                            fixture.run()

        self.assertIn(True, desync_calls, "desync nie zostal wlaczony")
        self.assertIn(
            False,
            desync_calls,
            "porazka emisji zdarzenia zostawila wezel z wsrep_desync=ON",
        )
        self.assertEqual(
            desync_calls.index(True),
            desync_calls.index(False) - 1,
            f"desync zdjety nie natychmiast po wlaczeniu: {desync_calls}",
        )


class ContendedLockKeepsItsOwnStatusTests(unittest.TestCase):
    """Blokada to `status="locked"`, nie `"failed"` — i metryka MUSI powstac.

    Zmierzone przy okazji porzadkowania sankow: zlanie zapisu blokady z zapisem
    porazki zmienilo `last_run.status`, po ktorym konsument rozpoznaje blokade
    (ten sam status zapisuje `restore.py`). Sank ma chronic metryke, nie
    przedefiniowywac kontrakt stanu.
    """

    def test_lock_contention_records_locked_and_publishes_the_metric(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = _Fixture(td)
            fixture.metric_file.write_text(
                'galera_backup_last_run_success{cluster="r10-galera",'
                'cluster_name="probe-r10",backend="s3"} 1\n',
                encoding="utf-8",
            )
            # Ten sam plik blokady jest trzymany przez inny przebieg.
            holder = pipeline.LockManager(pipeline.resolve_lock_path("probe-r10", fixture.cluster_dir))
            holder.acquire()
            try:
                with self.assertRaises(pipeline.BackupError) as ctx:
                    fixture.run()
            finally:
                holder.release()

            self.assertEqual(ctx.exception.code, "E_LOCKED")

            state = json.loads((fixture.cluster_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                state["last_run"]["status"],
                "locked",
                "blokada zapisana jako inny status niz kontrakt (restore.py uzywa 'locked')",
            )
            metric = fixture.metric_file.read_text(encoding="utf-8")
            self.assertRegex(
                metric,
                r"galera_backup_last_run_success\{[^}]*\} 0",
                f"blokada nie zglosila porazki w metryce: {metric!r}",
            )


class UnreadableStateStillPublishesFailureMetricTests(unittest.TestCase):
    """Uszkodzony `state.json` nie moze zamrozic dashboardu na zielono."""

    def test_corrupt_state_publishes_failure_and_keeps_original_error(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = _Fixture(td)
            # Poprzedniej nocy backup sie udal: metryka jest zielona.
            fixture.metric_file.write_text(
                'galera_backup_last_run_success{cluster="r10-galera",'
                'cluster_name="probe-r10",backend="s3"} 1\n',
                encoding="utf-8",
            )
            # ...ale plik stanu jest nieczytelny (przerwany zapis / uszkodzenie).
            (fixture.cluster_dir / "state.json").write_text(
                '{"format_version": 1, "last_success": ', encoding="utf-8"
            )

            with patch.object(backup, "elect_backup_donor", return_value="192.0.2.11"):
                with patch.object(backup, "assert_scheduler_is_not_writer"):
                    with patch.object(backup, "query_galera_vars", return_value=GALERA_VARS):
                        with self.assertRaises(pipeline.BackupError) as ctx:
                            fixture.run()

            self.assertEqual(
                ctx.exception.code,
                "E_STATE",
                "ponowny odczyt zastapil oryginalna diagnostyke",
            )
            metric = fixture.metric_file.read_text(encoding="utf-8")
            self.assertRegex(
                metric,
                r"galera_backup_last_run_success\{[^}]*\} 0",
                f"metryka nie zglosila porazki (zostala zielona): {metric!r}",
            )
            self.assertNotRegex(
                metric,
                r"galera_backup_last_run_success\{[^}]*\} 1",
                "dashboard nadal twierdzi, ze ostatni przebieg sie udal",
            )


if __name__ == "__main__":
    unittest.main()
