import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "tests" / "lab" / "chaos-app-degradation.py"


def load_probe(tmp):
    cluster = {
        "cluster": {"name": "newclaude16-r9", "environment": "laboratory"},
        "galera": {"nodes_expected": 3},
        "proxysql": {"app_user": "app_user_n16", "hostgroup_base": 810,
                     "endpoint": {"address": "192.0.2.10", "port": 6033}},
    }
    inventory = {"all": {"children": {
        "galera": {"hosts": {"n16g1": {"ansible_host": "192.0.2.1"},
                              "n16g2": {"ansible_host": "192.0.2.2"},
                              "n16g3": {"ansible_host": "192.0.2.3"}}},
        "proxysql": {"hosts": {"fcp1": {"ansible_host": "192.0.2.11"},
                                "fcp2": {"ansible_host": "192.0.2.12"}}},
        "app": {"hosts": {"fcapp": {"ansible_host": "192.0.2.20"}}},
    }}}
    config = tmp / "cluster.yml"
    inv = tmp / "inventory.yml"
    config.write_text(yaml.safe_dump(cluster), encoding="utf-8")
    inv.write_text(yaml.safe_dump(inventory), encoding="utf-8")
    env = {
        "CLUSTER": "newclaude16-r9",
        "CLUSTER_CONFIG": str(config),
        "CLUSTER_INVENTORY": str(inv),
        "APP_DB_PASSWORD": "hunter2",
        "QUORUM_RUN_ID": "a" * 32,
    }
    sys.path.insert(0, str(PROBE.parent))
    try:
        with patch.dict(os.environ, env, clear=False):
            return runpy.run_path(str(PROBE))
    finally:
        sys.path.remove(str(PROBE.parent))


class TargetGuardTests(unittest.TestCase):
    def test_other_laboratory_cluster_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        errors = ns["validate_target"](
            "other-lab", ns["EXPECTED_CONFIG"], ns["EXPECTED_INVENTORY"],
            {"n16g1", "n16g2", "n16g3"}, {"fcp1", "fcp2"}, {"fcapp"},
        )
        self.assertIn("cluster name", " ".join(errors))

    def test_noncanonical_paths_are_refused_even_with_spoofed_name(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        errors = ns["validate_target"](
            "newclaude16-r9", Path("/tmp/spoof.yml"), Path("/tmp/spoof-inventory.yml"),
            {"n16g1", "n16g2", "n16g3"}, {"fcp1", "fcp2"}, {"fcapp"},
        )
        self.assertTrue(any("config path" in error for error in errors))
        self.assertTrue(any("inventory path" in error for error in errors))


class SafeRunnerTests(unittest.TestCase):
    def test_timeout_becomes_structured_failure(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ansible", 1)):
            result = ns["safe_sh"]("n16g1", "true", timeout=1)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["rc"])
        self.assertIn("timeout", result["error"])

    def test_oserror_becomes_structured_failure(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        with patch.object(subprocess, "run", side_effect=FileNotFoundError("ansible")):
            result = ns["safe_sh"]("n16g1", "true", timeout=1)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["rc"])
        self.assertEqual(result["output"], "")
        self.assertIn("FileNotFoundError", result["error"])


class SecretTransportTests(unittest.TestCase):
    def test_app_write_uses_profile_not_password_or_setup(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        calls = []

        def fake(host, script, timeout=120):
            calls.append((host, script))
            return {"ok": True, "rc": 0, "output": "", "error": ""}

        with patch.dict(ns["app_write"].__globals__, {"safe_sh": fake}):
            ns["app_write"]()
        command = calls[-1][1]
        self.assertIn("--defaults-extra-file=/run/isa-app-degradation.cnf", command)
        self.assertNotIn("MYSQL_PWD", command)
        self.assertNotIn("hunter2", command)
        self.assertNotIn("CREATE TABLE", command)

    def test_profile_removal_retries_and_verifies_absence(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        responses = iter([
            {"ok": True, "rc": 0, "output": "", "error": ""},
            {"ok": True, "rc": 0, "output": "PRESENT", "error": ""},
            {"ok": True, "rc": 0, "output": "", "error": ""},
            {"ok": True, "rc": 0, "output": "ABSENT", "error": ""},
        ])
        globals_ = ns["remove_app_profile"].__globals__
        with patch.dict(globals_, {"safe_sh": lambda *args, **kwargs: next(responses)}), \
                patch.object(globals_["time"], "sleep", return_value=None):
            result = ns["remove_app_profile"]()
        self.assertTrue(result["absent"])
        self.assertEqual(len(result["history"]), 2)


class StructuredCollectorTests(unittest.TestCase):
    def test_missing_log_mark_is_not_offset_zero(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        failed = {"ok": False, "rc": 1, "output": "", "error": "stat failed"}
        with patch.dict(ns["log_mark"].__globals__, {"safe_sh": lambda *args, **kwargs: failed}):
            with self.assertRaises(ns["EvidenceError"]):
                ns["log_mark"]("fcp1")

    def test_log_delta_rejects_inode_change(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        outputs = iter([
            {"ok": True, "rc": 0, "output": "222\t120", "error": ""},
        ])
        with patch.dict(ns["log_delta"].__globals__, {"safe_sh": lambda *args, **kwargs: next(outputs)}):
            with self.assertRaises(ns["EvidenceError"]):
                ns["log_delta"]("fcp1", {"inode": 111, "size": 100})

    def test_vip_holder_fails_closed_when_one_probe_errors(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        vip = ns["VIP"]

        def fake_safe_sh(host, script, timeout=120):
            if host == "fcp1":
                return {"ok": True, "rc": 0,
                        "output": f"eth0   UP   192.0.2.11/24 {vip}/32", "error": ""}
            return {"ok": False, "rc": None, "output": "", "error": "FileNotFoundError: ansible"}

        with patch.dict(ns["vip_holder"].__globals__, {"safe_sh": fake_safe_sh}):
            with self.assertRaises(ns["EvidenceError"]) as ctx:
                ns["vip_holder"]()
        self.assertIn("fcp2", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
