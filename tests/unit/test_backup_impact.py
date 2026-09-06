"""Behavioral regressions for tests/lab/backup-impact.py (ISC-39 flow gate).

Skrypt ladowany przez importlib (dash w nazwie) z tymczasowym
cluster.yml/inventory.yml przez CLUSTER_CONFIG/CLUSTER_INVENTORY — konwencja
jak w test_inventory_tf_consistency_probe.py. Ansible, playbook backupu,
sleep i zdalne logi sa stubowane, wiec main() wykonuje sie w calosci bez
zywego klastra.
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT_PATH = WORKSPACE / "tests" / "lab" / "backup-impact.py"

CLUSTER_YML = {
    "cluster": {"environment": "lab", "name": "probe"},
    "proxysql": {"endpoint": {"address": "192.0.2.10", "port": 3306}},
}
INVENTORY_YML = {
    "all": {"children": {
        "galera": {"hosts": {"gnode1": None, "gnode2": None, "gnode3": None}},
        "app": {"hosts": {"app1": None}},
    }},
}


def banner(node, payload, rc=0):
    status = "CHANGED" if rc == 0 else "FAILED"
    return f"{node} | {status} | rc={rc} >>\n{payload}"


class BackupImpactGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "cluster.yml").write_text(yaml.safe_dump(CLUSTER_YML), encoding="utf-8")
        (root / "inventory.yml").write_text(yaml.safe_dump(INVENTORY_YML), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("backup_impact_under_test", SCRIPT_PATH)
        with mock.patch.dict(os.environ, {
            "CLUSTER_CONFIG": str(root / "cluster.yml"),
            "CLUSTER_INVENTORY": str(root / "inventory.yml"),
            "APP_DB_PASSWORD": "test-password",
        }):
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)

    # --- parse_flow_counter -------------------------------------------------

    def test_parse_accepts_plain_show_row(self):
        result = SimpleNamespace(returncode=0,
                                 stdout=banner("gnode1", "wsrep_flow_control_paused_ns\t12345"),
                                 stderr="")
        self.assertEqual(self.mod.parse_flow_counter("gnode1", result), 12345)

    def test_parse_fails_closed_on_nonzero_returncode(self):
        result = SimpleNamespace(returncode=2,
                                 stdout=banner("gnode1", "ERROR 2002 (HY000): lost connection", rc=2),
                                 stderr="")
        with self.assertRaises(self.mod.MeasurementError) as ctx:
            self.mod.parse_flow_counter("gnode1", result)
        self.assertIn("gnode1", str(ctx.exception))
        self.assertIn("rc=2", str(ctx.exception))

    def test_parse_fails_closed_on_missing_output(self):
        result = SimpleNamespace(returncode=0, stdout=banner("gnode2", ""), stderr="")
        with self.assertRaises(self.mod.MeasurementError) as ctx:
            self.mod.parse_flow_counter("gnode2", result)
        self.assertIn("empty", str(ctx.exception))

    def test_parse_fails_closed_on_malformed_counter(self):
        bad_rows = [
            "wsrep_flow_control_paused_ns\t12abc",
            "wsrep_flow_control_paused_ns\t-5",
            "wsrep_flow_control_paused\t7",
            "garbage line",
            "wsrep_flow_control_paused_ns\t1\nwsrep_flow_control_paused_ns\t2",
        ]
        for row in bad_rows:
            with self.subTest(row=row):
                result = SimpleNamespace(returncode=0, stdout=banner("gnode2", row), stderr="")
                with self.assertRaises(self.mod.MeasurementError):
                    self.mod.parse_flow_counter("gnode2", result)

    # --- flow_gate_failures -------------------------------------------------

    def test_gate_passes_when_every_node_delta_below_threshold(self):
        before = {"gnode1": 100, "gnode2": 100, "gnode3": 100}
        after = {"gnode1": 150, "gnode2": 100, "gnode3": 110}
        failures, deltas = self.mod.flow_gate_failures(before, after)
        self.assertEqual(failures, [])
        self.assertEqual(deltas, {"gnode1": 50, "gnode2": 0, "gnode3": 10})

    def test_gate_fails_when_nondominant_node_exceeds_threshold(self):
        threshold = self.mod.FLOW_THRESHOLD_NS
        before = {"gnode1": threshold * 4, "gnode2": 0, "gnode3": 0}
        after = {"gnode1": threshold * 4, "gnode2": threshold + 1, "gnode3": 0}
        failures, deltas = self.mod.flow_gate_failures(before, after)
        self.assertEqual(deltas["gnode1"], 0)
        self.assertTrue(any("gnode2" in f and str(threshold + 1) in f for f in failures))

    def test_gate_fails_closed_on_missing_node(self):
        failures, _ = self.mod.flow_gate_failures(
            {"gnode1": 1, "gnode3": 1},
            {"gnode1": 2, "gnode2": 2, "gnode3": 2})
        self.assertTrue(any("gnode2" in f and "baseline" in f for f in failures))

    def test_gate_fails_closed_on_reset_counter(self):
        failures, deltas = self.mod.flow_gate_failures(
            {"gnode1": 5, "gnode2": 500, "gnode3": 5},
            {"gnode1": 6, "gnode2": 0, "gnode3": 6})
        self.assertNotIn("gnode2", deltas)
        self.assertTrue(any("decreased" in f and "gnode2" in f for f in failures))

    # --- main() bez zywego klastra -------------------------------------------

    def _run_main(self, before, after, show_failures=None):
        """Pelny main() na stubach: ansible/playbook/sleep/logi w pamieci.

        show_failures: node -> indeks probki SHOW STATUS (0=baseline,
        1=post-backup), ktora ma zwrocic rc != 0. Zwraca (rc, output, cleanup).
        """
        mod = self.mod
        phase = {"backup_done": False}
        show_reads = {}
        calls = {"cleanup": []}

        def ok(node, payload=""):
            return SimpleNamespace(returncode=0, stdout=banner(node, payload), stderr="")

        def sh_stub(node, script, timeout=60, check=False):
            if "SHOW STATUS LIKE 'wsrep_flow_control_paused_ns'" in script:
                index = show_reads.get(node, 0)
                show_reads[node] = index + 1
                if show_failures and show_failures.get(node) == index:
                    return SimpleNamespace(
                        returncode=2,
                        stdout=banner(node, "ERROR 2002 (HY000): lost connection", rc=2),
                        stderr="")
                value = (before if index == 0 else after)[node]
                return ok(node, f"wsrep_flow_control_paused_ns\t{value}")
            # Uwaga: launch workloadu tez zawiera LOG_REMOTE w komendzie, wiec
            # czytanie logu rozpoznajemy po prefiksie `cat`.
            if script.startswith("cat /tmp/workload.log"):
                now = time.time()
                rows = "\n".join(f"{now - 0.5 + i * 0.3:.6f} {i + 1}" for i in range(2))
                return ok(node, rows)
            if "state.json" in script:
                unixtime = 1_000 if not phase["backup_done"] else 2_000
                return ok(node, json.dumps(
                    {"last_success": {"command": "backup", "unixtime": unixtime}}))
            if script.startswith("rm -f /tmp/workload.run"):
                calls["cleanup"].append(script)
                return ok(node)
            if "CREATE DATABASE" in script or "touch /tmp/workload.run" in script:
                return ok(node)
            raise AssertionError(f"unexpected sh() call: {node}: {script}")

        def run_stub(cmd, **kwargs):
            if any("f10_backup.yml" in part for part in cmd):
                phase["backup_done"] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(mod, "sh", sh_stub), \
                mock.patch.object(mod.subprocess, "run", run_stub), \
                mock.patch.object(mod.time, "sleep", lambda *_: None), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = mod.main()
        return rc, out.getvalue(), calls["cleanup"]

    def test_main_passes_when_all_nodes_below_threshold(self):
        before = {"gnode1": 100_000_000, "gnode2": 50_000_000, "gnode3": 0}
        after = {"gnode1": 500_000_000, "gnode2": 50_000_000, "gnode3": 10_000_000}
        rc, out, cleanup = self._run_main(before, after)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS", out)
        self.assertIn("worst-node flow control 400000000 ns on gnode1", out)
        self.assertTrue(cleanup, "workload must be stopped even on PASS")

    def test_main_cannot_pass_when_nondominant_node_pauses_above_threshold(self):
        # Stara metryka max(after)-max(before) dawala tu 0: dominujaca baseline
        # na gnode1 maskowala realny pause na gnode2.
        threshold = self.mod.FLOW_THRESHOLD_NS
        before = {"gnode1": threshold * 4, "gnode2": 0, "gnode3": 0}
        after = {"gnode1": threshold * 4, "gnode2": threshold + 1, "gnode3": 0}
        rc, out, cleanup = self._run_main(before, after)
        self.assertEqual(rc, 1, out)
        self.assertIn("gnode2", out)
        self.assertIn("per-node deltas", out)
        self.assertIn("gnode1=0", out)
        self.assertTrue(cleanup, "workload must be stopped despite failed measurement")

    def test_main_fails_closed_on_reset_counter(self):
        before = {"gnode1": 100, "gnode2": 1_000_000_000, "gnode3": 100}
        after = {"gnode1": 200, "gnode2": 0, "gnode3": 200}
        rc, out, _ = self._run_main(before, after)
        self.assertEqual(rc, 1, out)
        self.assertIn("decreased", out)
        self.assertIn("gnode2", out)

    def test_main_fails_closed_when_show_status_errors_mid_measurement(self):
        before = {"gnode1": 100, "gnode2": 100, "gnode3": 100}
        after = {"gnode1": 200, "gnode2": 200, "gnode3": 200}
        rc, out, cleanup = self._run_main(before, after, show_failures={"gnode3": 1})
        self.assertEqual(rc, 1, out)
        self.assertIn("gnode3", out)
        self.assertIn("rc=2", out)
        self.assertTrue(cleanup)


if __name__ == "__main__":
    unittest.main()
