"""Invalid restore timestamps must report failure, not crash or pass freshness."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import yaml

REPO = Path(__file__).resolve().parents[2]


class RestoreTimestampTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "cluster.yml"
            inventory = root / "inventory.yml"
            config.write_text(yaml.safe_dump({
                "cluster": {"name": "test-cluster"},
                "backup": {"enabled": True, "restore_test_schedule": "0 4 * * 0"},
            }))
            inventory.write_text(yaml.safe_dump({
                "all": {"children": {"restore": {"hosts": {"rnode1": {}}}}},
            }))
            spec = importlib.util.spec_from_file_location(
                "restore_timestamp_probe", REPO / "tests/lab/probe-restore.py"
            )
            with mock.patch.dict(os.environ, {
                "CLUSTER": "test-cluster",
                "CLUSTER_CONFIG": str(config), "CLUSTER_INVENTORY": str(inventory),
            }), mock.patch.object(sys, "path", [str(REPO / "tests/lab"), *sys.path]):
                self.probe = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(self.probe)
                from _probe_common import AnsibleResult
                self.result = AnsibleResult()

    def run_state(self, last_success):
        self.result.bodies["rnode1"] = json.dumps({
            "cluster": "test-cluster",
            "last_run": {"command": "restore", "status": "success"},
            "last_success": {
                "command": "restore",
                "artifact": {"backup_name": "restore-fixture", "rows_verified": 12},
                **last_success,
            },
        })
        output = io.StringIO()
        with mock.patch.object(self.probe, "run_ansible", return_value=self.result), \
                contextlib.redirect_stdout(output):
            code = self.probe.main()
        return code, output.getvalue()

    def test_missing_or_unusable_timestamp_fails_without_exception(self):
        for state in ({}, {"unixtime": None}, {"unixtime": "invalid"},
                      {"unixtime": 0}, {"unixtime": "nan"}, {"unixtime": "inf"}):
            with self.subTest(state=state):
                code, output = self.run_state(state)
                self.assertEqual(code, 1)
                self.assertIn("ISC-37", output)
                self.assertNotIn("PASS:", output)

    def test_timestamp_still_controls_freshness(self):
        now = datetime.now(timezone.utc).timestamp()
        for age, expected in ((3600, 0), (10 * 86400, 1)):
            with self.subTest(age=age):
                code, output = self.run_state({"unixtime": now - age})
                self.assertEqual(code, expected, output)
                self.assertIn("PASS:" if expected == 0 else "ISC-37", output)


if __name__ == "__main__":
    unittest.main()
