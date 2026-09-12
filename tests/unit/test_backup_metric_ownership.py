#!/usr/bin/env python3
"""Metryka kopii nalezy do DONORA wybranego w przebiegu, nie do stalego hosta.

ZMIERZONE 2026-09-12. Cron stoi na kazdym kandydacie, a donora wybiera runner
przy starcie (backup hostgroup ProxySQL). Dwa miejsca nadal zakladaly jednego,
stalego producenta:

  1. Runner pomijajacy przebieg ("nie jestem donorem") zostawial na dysku swoj
     poprzedni plik `.prom`. Po przeniesieniu donora node_exporter serwowal dwie
     sprzeczne serie tej samej metryki, a `min(galera_backup_last_run_success)`
     z reguly alertu widzial porzucone zero i palil sie mimo zdrowych kopii.
  2. Sonda PMM wymagala, zeby seria pochodzila z PIERWSZEGO wezla inwentarza —
     zdrowy klaster, w ktorym harmonogram zostal writerem i elekcja slusznie
     przeniosla backup na sasiada, oblewal bramke po budowie.

Testy sprawdzaja zachowanie: stan plikow po przebiegu runnera oraz werdykt
sondy dla serii z wezla innego niz pierwszy. Zadne z nich nie dotyka floty.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "roles" / "galera_backup" / "files"))

from galera_backup import backup, pipeline  # noqa: E402

PROBE = REPO / "tests" / "lab" / "probe-pmm-native.py"


def _load_probe():
    """Sonda importuje sasiednie moduly i czyta konfiguracje z korzenia repo."""
    sys.path.insert(0, str(PROBE.parent))
    cwd = os.getcwd()
    os.chdir(REPO)
    try:
        spec = importlib.util.spec_from_file_location("probe_pmm_under_test", PROBE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(cwd)


PROBE_MODULE = _load_probe()

DONOR = "192.168.1.51"
THIS_NODE = "192.168.1.52"


def _config(td_path):
    return {
        "format_version": 1,
        "cluster_name": "review-r10",
        "metric_cluster_label": "r10-galera",
        "local_role": "scheduler",
        "scheduler_system_hostname": "different-host",
        "node_system_address": THIS_NODE,
        "galera_nodes_expected": 3,
        "proxysql": {
            "admin_host": "192.168.1.44",
            "admin_port": 6032,
            "writer_hostgroup": 10,
            "backup_hostgroup": 20,
        },
        "galera_nodes": [DONOR, THIS_NODE, "192.168.1.53"],
        "mariadb_version": "11.4.12",
        "retention_days": 14,
        "flow_control_threshold_ns": 1000000000,
        "backend": {
            "type": "s3",
            "endpoint": "192.168.1.47:9000",
            "bucket": "r10-galera-backups",
            "secure": False,
        },
        "paths": {
            "install_root": str(td_path),
            "cluster_dir": str(td_path / "clusters" / "review-r10"),
            "staging_root": str(td_path / "staging"),
            "datadir": str(td_path / "datadir"),
            "socket": str(td_path / "mysql.sock"),
            "metric_file": str(td_path / "metrics.prom"),
        },
    }


SECRETS = (
    'GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\n'
    'GALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\n'
    'GALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\n'
    'GALERA_BACKUP_PROXYSQL_STATS_USER="admin"\n'
    'GALERA_BACKUP_PROXYSQL_STATS_PASSWORD="proxysql_pass_999"\n'
)

STALE_METRIC = (
    'galera_backup_last_success_unixtime{cluster="r10-galera"} 1757000000\n'
    'galera_backup_last_run_success{cluster="r10-galera"} 0\n'
)


class NonDonorLeavesNoMetricTests(unittest.TestCase):
    """Wezel, ktory nie zostal donorem, nie moze zostawic wlasnej serii."""

    def setUp(self):
        self.writer_guard = patch.object(backup, "assert_scheduler_is_not_writer")
        self.writer_guard.start()
        self.addCleanup(self.writer_guard.stop)
        self.donor_election = patch.object(
            backup, "elect_backup_donor", return_value=DONOR
        )
        self.donor_election.start()
        self.addCleanup(self.donor_election.stop)

    def _run_skipped_backup(self, td_path):
        cfg_path = td_path / "config.json"
        env_path = td_path / "secrets.env"
        cfg_path.write_text(json.dumps(_config(td_path)))
        env_path.write_text(SECRETS)
        os.chmod(env_path, 0o600)
        with patch("socket.gethostname", return_value="current-host"):
            with patch.object(backup, "get_storage_backend") as backend:
                with patch.object(backup, "run_retention") as retention:
                    pipeline.run_backup(
                        config_path=cfg_path,
                        secrets_path=env_path,
                        cluster_name="review-r10",
                    )
            backend.assert_not_called()
        return retention

    def test_skipped_run_removes_the_metric_left_by_an_earlier_donor_turn(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            metric = td_path / "metrics.prom"
            metric.write_text(STALE_METRIC)

            self._run_skipped_backup(td_path)

            self.assertFalse(
                metric.exists(),
                "porzucona seria zostaje i psuje min(last_run_success) w alercie",
            )

    def test_retention_still_runs_and_absent_metric_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)

            retention = self._run_skipped_backup(td_path)

            # Brak pliku to normalny stan kandydata, ktory nigdy nie byl donorem.
            self.assertFalse((td_path / "metrics.prom").exists())
            retention.assert_called_once()
            events = (
                Path(_config(td_path)["paths"]["cluster_dir"]) / "events.jsonl"
            ).read_text()
            self.assertIn("skipped.not_elected", events)


class BackupMetricProducerTests(unittest.TestCase):
    """Sonda PMM ocenia serie, nie nazwe wezla, ktory ja opublikowal."""

    @classmethod
    def setUpClass(cls):
        cls.nodes = list(PROBE_MODULE.EXPECTED_NODES)
        cls.first_node = PROBE_MODULE.FIRST_GALERA_NODE
        cls.other_node = next(
            node for node in cls.nodes if node != cls.first_node
        )
        cls.logical = PROBE_MODULE.CLUSTER_CONFIG["cluster"]["name"]
        cls.backend = PROBE_MODULE.CLUSTER_CONFIG["backup"]["destination"]

    def series(self, node_name, value, scraped_at):
        return [
            {
                "metric": {
                    "node_name": node_name,
                    "logical_cluster": self.logical,
                    "backend": self.backend,
                },
                "value": [scraped_at, str(value)],
            }
        ]

    def test_fresh_series_from_a_non_first_donor_is_accepted(self):
        now = 1_800_000_000.0
        results = self.series(self.other_node, now - 60, now)
        self.assertTrue(
            PROBE_MODULE.backup_metric_valid(
                "galera_backup_last_success_unixtime",
                results,
                now - 60,
                now,
                now - 120,
            )
        )

    def test_series_from_a_node_outside_the_cluster_is_rejected(self):
        now = 1_800_000_000.0
        results = self.series("someone-elses-node", now - 60, now)
        self.assertFalse(
            PROBE_MODULE.backup_metric_valid(
                "galera_backup_last_success_unixtime",
                results,
                now - 60,
                now,
                now - 120,
            )
        )

    def test_two_series_mean_an_abandoned_file_and_stay_rejected(self):
        now = 1_800_000_000.0
        results = self.series(self.first_node, now - 60, now) + self.series(
            self.other_node, now - 60, now
        )
        self.assertFalse(
            PROBE_MODULE.backup_metric_valid(
                "galera_backup_last_success_unixtime",
                results,
                now - 60,
                now,
                now - 120,
            )
        )

    def test_failed_run_from_the_donor_is_still_a_failure(self):
        now = 1_800_000_000.0
        results = self.series(self.other_node, 0, now)
        self.assertFalse(
            PROBE_MODULE.backup_metric_valid(
                "galera_backup_last_run_success", results, 0, now, now - 120
            )
        )

    def test_stale_success_from_the_donor_is_rejected(self):
        now = 1_800_000_000.0
        age = (PROBE_MODULE.BACKUP_FRESHNESS_SLA_HOURS + 1) * 3600
        results = self.series(self.other_node, now - age, now)
        self.assertFalse(
            PROBE_MODULE.backup_metric_valid(
                "galera_backup_last_success_unixtime",
                results,
                now - age,
                now,
                now - 120,
            )
        )


if __name__ == "__main__":
    unittest.main()
