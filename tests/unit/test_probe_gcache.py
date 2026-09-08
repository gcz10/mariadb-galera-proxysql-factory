"""Behavioral regressions for tests/lab/probe-gcache.py (ISC-68 gcache gate).

Validates:
- Real bash execution of the generated workload script against a tracking fake mariadb binary.
- Successful workload exclusively creates, populates, and drops the measurement DB at exit.
- Collision on CREATE DATABASE prevents claiming ownership: trap never drops colliding unowned DB.
- Failure during CREATE TABLE or INSERT triggers trap-based cleanup because OWNED=1 was set.
- Failure during DROP DATABASE in cleanup forces a non-zero exit code (no || true masking).
- Environment guard strictly fails closed on production, missing, or unknown environments,
  allowing only documented lab environments without environment variable overrides.
- Measurement DB names respect MariaDB identifier length and isolate from legacy 'gcache_meas'.
"""

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT_PATH = WORKSPACE / "tests" / "lab" / "probe-gcache.py"
PROBE_COMMON_DIR = WORKSPACE / "tests" / "lab"

CLUSTER_YML = {
    "cluster": {"environment": "laboratory", "name": "test-cluster"},
    "proxysql": {"hostgroup_base": 10},
}

INVENTORY_YML = {
    "all": {
        "children": {
            "galera": {
                "hosts": {
                    "gnode1": {"ansible_host": "192.0.2.11"},
                }
            },
            "proxysql": {
                "hosts": {
                    "pnode1": {"ansible_host": "192.0.2.10"},
                }
            },
        }
    }
}

FAKE_MARIADB_SCRIPT = """#!/usr/bin/env python3
import os
import sys
import json
import re
import signal

state_file = os.environ.get("FAKE_MARIADB_STATE")
state = {"created": [], "dropped": [], "commands": []}
if state_file and os.path.exists(state_file):
    try:
        with open(state_file, "r") as f:
            state = json.load(f)
    except Exception:
        pass

cmd_str = " ".join(sys.argv)
state["commands"].append(cmd_str)

fail_create_db = os.environ.get("FAIL_CREATE_DB") == "1"
fail_create_tbl = os.environ.get("FAIL_CREATE_TABLE") == "1"
fail_insert = os.environ.get("FAIL_INSERT") == "1"
fail_drop_db = os.environ.get("FAIL_DROP_DB") == "1"

# Klient MariaDB przyjmuje SQL na DWA sposoby: przez `-e` i przez stdin. Atrapa
# czytala tylko argv, wiec po przejsciu obciazenia na jedno polaczenie z
# potokiem przestala widziec inserty — i przestala umiec je zepsuc. Test, ktory
# nie potrafi wywolac bledu, nie dowodzi sprzatania po bledzie.
sql = ""
for i, arg in enumerate(sys.argv):
    if arg == "-e" and i + 1 < len(sys.argv):
        sql = sys.argv[i + 1]
        break
if not sql and not sys.stdin.isatty():
    sql = sys.stdin.read()

if "CREATE DATABASE" in sql:
    m = re.search(r"CREATE DATABASE `?([a-zA-Z0-9_]+)`?", sql)
    db = m.group(1) if m else "unknown"
    if fail_create_db:
        sys.stderr.write(f"ERROR 1007 (HY000): Can't create database '{db}'; database exists\\n")
        sys.exit(1)
    state["created"].append(db)

elif "CREATE TABLE" in sql:
    if fail_create_tbl:
        sys.stderr.write("ERROR 1146: Table creation failed\\n")
        sys.exit(1)

elif "INSERT INTO" in sql:
    if os.environ.get("SIGNAL_ON_INSERT") == "1":
        os.kill(os.getppid(), signal.SIGTERM)
    if fail_insert:
        sys.stderr.write("ERROR: Insert failed\\n")
        sys.exit(1)

elif "DROP DATABASE" in sql:
    m = re.search(r"DROP DATABASE (?:IF EXISTS )?`?([a-zA-Z0-9_]+)`?", sql)
    db = m.group(1) if m else "unknown"
    if fail_drop_db:
        sys.stderr.write(f"ERROR: Failed to drop database {db}\\n")
        sys.exit(1)
    state["dropped"].append(db)

elif "SHOW STATUS LIKE 'wsrep_replicated_bytes'" in sql:
    sys.stdout.write("wsrep_replicated_bytes\\t1000000\\n")

if state_file:
    with open(state_file, "w") as f:
        json.dump(state, f)

sys.exit(0)
"""


class ProbeGcacheBehavioralTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.cfg_file = root / "cluster.yml"
        cls.inv_file = root / "inventory.yml"
        cls.cfg_file.write_text(yaml.safe_dump(CLUSTER_YML), encoding="utf-8")
        cls.inv_file.write_text(yaml.safe_dump(INVENTORY_YML), encoding="utf-8")

        # Set up fake mariadb binary
        cls.bin_dir = root / "bin"
        cls.bin_dir.mkdir(parents=True, exist_ok=True)
        fake_bin = cls.bin_dir / "mariadb"
        fake_bin.write_text(FAKE_MARIADB_SCRIPT, encoding="utf-8")
        fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        if str(PROBE_COMMON_DIR) not in sys.path:
            sys.path.insert(0, str(PROBE_COMMON_DIR))

        spec = importlib.util.spec_from_file_location("probe_gcache_under_test", SCRIPT_PATH)
        with mock.patch.dict(
            os.environ,
            {
                "CLUSTER": "test-cluster",
                "CLUSTER_CONFIG": str(cls.cfg_file),
                "CLUSTER_INVENTORY": str(cls.inv_file),
            },
        ):
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)

    def _run_bash_workload(self, db_name, workload_seconds=1, extra_env=None):
        script = self.mod.build_workload_script(db_name, workload_seconds)
        state_file = Path(self.tmp.name) / f"mariadb_state_{db_name}.json"
        if state_file.exists():
            state_file.unlink()

        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '')}"
        env["FAKE_MARIADB_STATE"] = str(state_file)
        if extra_env:
            env.update(extra_env)

        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
        )

        state = {"created": [], "dropped": [], "commands": []}
        if state_file.exists():
            with open(state_file, "r") as f:
                state = json.load(f)

        return proc, state

    # --- Real Bash Execution Against Fake MariaDB ---

    def test_workload_script_success_lifecycle(self):
        db_name = self.mod.generate_measurement_db_name()
        proc, state = self._run_bash_workload(db_name, workload_seconds=1)

        self.assertEqual(proc.returncode, 0, f"script failed: {proc.stderr}")
        self.assertIn("RATE_BPS=", proc.stdout)
        self.assertIn(db_name, state["created"])
        self.assertIn(db_name, state["dropped"])

    def test_workload_script_colliding_create_never_drops_database(self):
        db_name = "gcache_meas_collision"
        proc, state = self._run_bash_workload(
            db_name, workload_seconds=1, extra_env={"FAIL_CREATE_DB": "1"}
        )

        # Script must fail on CREATE DATABASE collision
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Can't create database", proc.stderr)
        # Because OWNED was never set to 1, cleanup trap MUST NOT have dropped the database
        self.assertEqual(state["dropped"], [], "colliding unowned DB must never be dropped")

    def test_workload_script_table_failure_cleans_up_created_database(self):
        db_name = "gcache_meas_tbl_fail"
        proc, state = self._run_bash_workload(
            db_name, workload_seconds=1, extra_env={"FAIL_CREATE_TABLE": "1"}
        )

        # Script must fail on CREATE TABLE
        self.assertNotEqual(proc.returncode, 0)
        # Because DB was created (OWNED=1), trap MUST have dropped the database
        self.assertIn(db_name, state["created"])
        self.assertIn(db_name, state["dropped"], "table failure must trigger cleanup of created DB")

    def test_workload_script_insert_failure_cleans_up_created_database(self):
        db_name = "gcache_meas_ins_fail"
        proc, state = self._run_bash_workload(
            db_name, workload_seconds=1, extra_env={"FAIL_INSERT": "1"}
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(db_name, state["created"])
        self.assertIn(db_name, state["dropped"], "insert failure must trigger cleanup of created DB")

    def test_workload_script_failed_drop_forces_nonzero_exit(self):
        db_name = "gcache_meas_drop_fail"
        proc, state = self._run_bash_workload(
            db_name, workload_seconds=1, extra_env={"FAIL_DROP_DB": "1"}
        )

        # Cleanup failure must force non-zero returncode (no || true masking)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cleanup failed to drop measurement database", proc.stderr)


    def test_termination_cleans_owned_database_and_fails(self):
        name = "gcache_meas_interrupted"
        proc, state = self._run_bash_workload(name, extra_env={"SIGNAL_ON_INSERT": "1"})
        self.assertEqual(proc.returncode, 143, proc.stderr)
        self.assertIn(name, state["dropped"])
    # --- Environment Guard (Fail-Closed on Production / Unknown / Missing) ---

    def test_environment_guard_refuses_production(self):
        with mock.patch.dict(self.mod.CTX.config, {"cluster": {"environment": "production"}}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = self.mod.main()

            self.assertEqual(rc, 1)
            self.assertIn("REFUSED:", out.getvalue())
            self.assertIn("production", out.getvalue())

    def test_environment_guard_fails_closed_on_missing_environment(self):
        with mock.patch.dict(self.mod.CTX.config, {"cluster": {}}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = self.mod.main()

            self.assertEqual(rc, 1)
            self.assertIn("REFUSED:", out.getvalue())
            self.assertIn("missing cluster.environment", out.getvalue())

    def test_environment_guard_fails_closed_on_unknown_environment(self):
        with mock.patch.dict(self.mod.CTX.config, {"cluster": {"environment": "staging"}}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = self.mod.main()

            self.assertEqual(rc, 1)
            self.assertIn("REFUSED:", out.getvalue())
            self.assertIn("staging", out.getvalue())

    def test_measure_write_rate_refuses_non_lab_environment(self):
        failures = []
        undetermined = []
        with mock.patch.dict(self.mod.CTX.config, {"cluster": {"environment": "production"}}):
            rate = self.mod.measure_write_rate("gnode1", failures, undetermined)
            self.assertIsNone(rate)
            self.assertTrue(any("must not run" in f for f in failures))

    # --- DB Name Format Constraints ---

    def test_generate_measurement_db_name_constraints(self):
        name1 = self.mod.generate_measurement_db_name()
        name2 = self.mod.generate_measurement_db_name()

        self.assertTrue(name1.startswith("gcache_meas_"))
        self.assertNotEqual(name1, "gcache_meas", "must never equal pre-existing gcache_meas")
        self.assertLessEqual(len(name1), 64, "MariaDB identifier limit is 64 chars")
        self.assertNotEqual(name1, name2, "consecutive DB names must be distinct")


if __name__ == "__main__":
    unittest.main()
