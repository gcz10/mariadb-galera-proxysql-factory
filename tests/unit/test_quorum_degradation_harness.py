import json
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


class MutationLifecycleTests(unittest.TestCase):
    def test_arm_never_kills_after_reload_failure(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        calls = []

        def run(host, script, timeout=120):
            calls.append(script)
            if "daemon-reload" in script:
                return {"ok": False, "rc": 1, "output": "", "error": "reload failed"}
            return {"ok": True, "rc": 0, "output": "no" if "show mariadb" in script else "", "error": ""}

        with self.assertRaises(ns["EvidenceError"]):
            ns["arm_node"]("n16g2", run=run)
        self.assertFalse(any("pkill" in command for command in calls))

    def test_arm_rejects_kill_failure_and_still_alive_process(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))

        def kill_fails(host, script, timeout=120):
            if "show mariadb" in script:
                return {"ok": True, "rc": 0, "output": "no", "error": ""}
            if "pkill" in script:
                return {"ok": False, "rc": 1, "output": "", "error": "kill failed"}
            return {"ok": True, "rc": 0, "output": "", "error": ""}

        with self.assertRaises(ns["EvidenceError"]):
            ns["arm_node"]("n16g2", run=kill_fails)

        def still_alive(host, script, timeout=120):
            if "show mariadb" in script:
                output = "no"
            elif "pgrep" in script:
                output = "ALIVE"
            else:
                output = ""
            return {"ok": True, "rc": 0, "output": output, "error": ""}

        with self.assertRaises(ns["EvidenceError"]):
            ns["arm_node"]("n16g2", run=still_alive)

    def test_cleanup_attempts_every_operation_on_every_host(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        calls = []

        def run(host, script, timeout=120):
            calls.append((host, script))
            if host == "n16g2" and script.startswith("rm -f"):
                return {"ok": False, "rc": None, "output": "", "error": "timeout"}
            if "test ! -e" in script:
                return {"ok": True, "rc": 0, "output": "ABSENT", "error": ""}
            if "show mariadb" in script:
                return {"ok": True, "rc": 0, "output": "on-abnormal", "error": ""}
            return {"ok": True, "rc": 0, "output": "", "error": ""}

        result = ns["cleanup_nodes"](
            ("n16g2", "n16g3"), {"n16g2": "on-abnormal", "n16g3": "on-abnormal"}, run=run
        )
        for host in ("n16g2", "n16g3"):
            host_commands = [command for called_host, command in calls if called_host == host]
            self.assertTrue(any("daemon-reload" in command for command in host_commands))
            self.assertTrue(any("start --no-block" in command for command in host_commands))
            self.assertTrue(any("test ! -e" in command for command in host_commands))
            self.assertTrue(any("show mariadb" in command for command in host_commands))
        self.assertFalse(result["n16g2"]["remove_ok"])
        self.assertTrue(result["n16g3"]["dropin_absent"])

    def test_cleanup_set_is_both_intended_hosts_before_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        self.assertEqual(tuple(ns["STOPPED"]), ("n16g2", "n16g3"))

class RecordAndArtifactTests(unittest.TestCase):
    def test_new_record_initializes_all_local_acceptance_flags_to_false(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        record = ns["new_record"]()
        self.assertEqual(record["run_id"], ns["RUN_ID"])
        self.assertEqual(record["cluster"], ns["CLUSTER_NAME"])
        self.assertEqual(record["outcome"], ns["OUTCOME_UNRESOLVED"])
        self.assertFalse(record["contract"]["match"])
        self.assertEqual(record["errors"], [])
        for name in ns["LOCAL_ACCEPTANCE"]:
            self.assertIn(name, record["acceptance"])
            self.assertIs(record["acceptance"][name], False)
        self.assertIsNone(record["acceptance"]["platform_verify"])
        self.assertIsNone(record["acceptance"]["post_build_gate"])
        self.assertIs(record["cleanup"]["credential_profile_absent"], False)

    def test_write_artifact_is_mode_0600_from_creation(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
            record = ns["new_record"]()
            record["test_field"] = "sample_data"
            dest = Path(td) / "test-evidence.json"
            with patch.dict(ns["write_artifact"].__globals__, {
                "Path": lambda p: dest,
            }):
                written = ns["write_artifact"](record)
            self.assertEqual(written, dest)
            self.assertTrue(dest.exists())
            mode = os.stat(dest).st_mode & 0o777
            self.assertEqual(mode, 0o600)
            loaded = json.loads(dest.read_text(encoding="utf-8"))
            self.assertEqual(loaded["test_field"], "sample_data")
            self.assertEqual(loaded["run_id"], ns["RUN_ID"])

    def test_write_artifact_cleans_up_temp_file_on_write_failure(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
            dest = Path(td) / "test-evidence.json"
            created_tmps = []
            orig_mkstemp = tempfile.mkstemp

            def tracking_mkstemp(*args, **kwargs):
                fd, path = orig_mkstemp(*args, **kwargs)
                created_tmps.append(path)
                return fd, path

            with patch.dict(ns["write_artifact"].__globals__, {
                "Path": lambda p: dest,
                "tempfile": type("TempModule", (), {"mkstemp": staticmethod(tracking_mkstemp)})(),
                "json": type("JsonModule", (), {"dumps": lambda *a, **kw: (_ for _ in ()).throw(ValueError("boom"))})(),
            }):
                with self.assertRaises(ValueError):
                    ns["write_artifact"]({})
            self.assertEqual(len(created_tmps), 1)
            self.assertFalse(os.path.exists(created_tmps[0]))
            self.assertFalse(dest.exists())


class OrchestrationTests(unittest.TestCase):
    def test_run_measurement_baseline_failure_stops_before_arming(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        arm_calls = []

        def fake_collect_versions():
            raise ns["EvidenceError"]("mocked baseline failure")

        def fake_arm(host, run=None):
            arm_calls.append(host)
            return True

        with patch.dict(ns["run_measurement"].__globals__, {
            "collect_versions": fake_collect_versions,
            "arm_node": fake_arm,
        }):
            rec = ns["new_record"]()
            result_rec = ns["run_measurement"](rec)

        self.assertEqual(arm_calls, [])
        self.assertFalse(result_rec["acceptance"]["baseline_complete"])
        self.assertTrue(any("baseline: mocked baseline failure" in err for err in result_rec["errors"]))

    def test_main_fails_closed_and_records_errors_when_credential_removal_fails(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        written_records = []

        def fake_remove(attempts=3):
            return {
                "absent": False,
                "history": [{"attempt": 1, "remove": {"ok": True}, "verify": {"ok": True, "output": "PRESENT"}}],
            }

        def fake_write_artifact(record):
            written_records.append(record)
            return Path("/tmp/fake-artifact.json")

        def fake_run_measurement(record):
            for k in ns["LOCAL_ACCEPTANCE"]:
                record["acceptance"][k] = True
            record["outcome"] = "degraded"
            record["contract"] = {"expected": "degraded", "observed": "degraded", "match": True}
            return record

        with patch.dict(ns["main"].__globals__, {
            "validate_target": lambda *a, **kw: [],
            "install_app_profile": lambda: None,
            "run_measurement": fake_run_measurement,
            "remove_app_profile": fake_remove,
            "write_artifact": fake_write_artifact,
        }):
            exit_code = ns["main"]()

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(written_records), 1)
        rec = written_records[0]
        self.assertFalse(rec["cleanup"]["credential_profile_absent"])
        self.assertFalse(rec["acceptance"]["credential_profile_absent"])
        self.assertTrue(any("credential profile remains" in err for err in rec["errors"]))

    def test_main_fails_closed_and_cleans_up_when_setup_fails(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        written_records = []
        cleanup_calls = []

        def fake_install():
            raise ns["EvidenceError"]("copy failed")

        def fake_remove(attempts=3):
            cleanup_calls.append(attempts)
            return {"absent": True, "history": []}

        def fake_write_artifact(record):
            written_records.append(record)
            return Path("/tmp/fake-artifact.json")

        with patch.dict(ns["main"].__globals__, {
            "validate_target": lambda *a, **kw: [],
            "install_app_profile": fake_install,
            "remove_app_profile": fake_remove,
            "write_artifact": fake_write_artifact,
        }):
            exit_code = ns["main"]()

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(cleanup_calls), 1)
        self.assertEqual(len(written_records), 1)
        rec = written_records[0]
        self.assertTrue(any("credential/setup: copy failed" in err for err in rec["errors"]))


    def test_main_refuses_invalid_contract_before_profile_or_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        install_calls = []
        measurement_calls = []

        def fake_install():
            install_calls.append(True)

        def fake_run_measurement(record):
            measurement_calls.append(record)
            return record

        with patch.dict(ns["main"].__globals__, {
            "CONTRACT": "invalid_contract_mode",
            "validate_target": lambda *a, **kw: [],
            "install_app_profile": fake_install,
            "run_measurement": fake_run_measurement,
        }):
            exit_code = ns["main"]()

        self.assertEqual(exit_code, 1)
        self.assertEqual(install_calls, [])
        self.assertEqual(measurement_calls, [])
if __name__ == "__main__":
    unittest.main()
