"""Kontrakt bramki budowy (tests/validation/gate-build.sh, F4 + Punkt 5).

Makefile deleguje do skryptu politykę warunkową budowy: sprzężenie seed->backup,
kolejność kroków warunkowych (app-host PRZED backupem/drillem) i pomijanie
kroków przez BUILD_SKIP.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "validation" / "gate-build.sh"


def run_preflight(backup_enabled: str, build_skip: str, existing_data: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(GATE), "preflight", backup_enabled, build_skip, existing_data],
        capture_output=True,
        text=True,
        timeout=30,
    )


class GateBuildPreflightTest(unittest.TestCase):
    def test_script_exists_and_parses(self):
        self.assertTrue(GATE.is_file(), f"brak {GATE}")
        subprocess.run(["bash", "-n", str(GATE)], check=True, timeout=30)

    def test_seed_backup_skipped_together_is_legal(self):
        proc = run_preflight("true", "seed backup", "")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_seed_skipped_without_backup_refuses_without_existing_data(self):
        proc = run_preflight("true", "seed", "")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("EXISTING_DATA=yes", proc.stderr)

    def test_seed_skipped_with_declared_existing_data_is_legal(self):
        proc = run_preflight("true", "seed", "yes")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_backup_disabled_disarms_the_coupling(self):
        # Na klastrze bez backupu drill nie istnieje, wiec EXISTING_DATA jest
        # pytaniem o dane dla przebiegu, ktory sie nie odbędzie.
        proc = run_preflight("false", "seed", "")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_skips_is_legal(self):
        proc = run_preflight("true", "", "")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_unknown_subcommand_refuses(self):
        proc = subprocess.run([str(GATE), "nonsense"], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)


class GateBuildStepsExecutionOrderTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.td_path = Path(self.td.name)
        self.log_file = self.td_path / "make.log"
        self.fake_make = self.td_path / "fake_make"
        self.fake_make.write_text("#!/usr/bin/env bash\necho \"$*\" >> \"$MAKE_LOG\"\n", encoding="utf-8")
        self.fake_make.chmod(0o755)

    def _run_steps(self, build_skip: str) -> list[str]:
        env = dict(os.environ)
        env["MAKE"] = str(self.fake_make)
        env["MAKE_LOG"] = str(self.log_file)
        proc = subprocess.run(
            [str(GATE), "steps", build_skip],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"steps failed: {proc.stderr}")
        if not self.log_file.is_file():
            return []
        return [line.strip() for line in self.log_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_default_execution_order_runs_app_host_before_backup(self):
        invoked = self._run_steps("")
        expected = [
            "lab-seed-smoke",
            "cluster-app-host",
            "cluster-backup-configure",
            "cluster-backup",
            "cluster-restore-drill CONFIRM=yes",
            "cluster-monitoring-refresh",
            "cluster-alerts",
        ]
        self.assertEqual(invoked, expected)
        self.assertLess(
            invoked.index("cluster-app-host"),
            invoked.index("cluster-backup-configure"),
            "cluster-app-host musi być wołany przed konfiguracją i wykonaniem backupu",
        )

    def test_build_skip_app_host_excludes_app_host_only(self):
        invoked = self._run_steps("app-host")
        self.assertNotIn("cluster-app-host", invoked)
        self.assertIn("cluster-backup-configure", invoked)
        self.assertIn("cluster-backup", invoked)
        self.assertIn("lab-seed-smoke", invoked)

    def test_build_skip_backup_excludes_all_backup_targets(self):
        invoked = self._run_steps("backup")
        self.assertIn("cluster-app-host", invoked)
        self.assertIn("lab-seed-smoke", invoked)
        self.assertNotIn("cluster-backup-configure", invoked)
        self.assertNotIn("cluster-backup", invoked)
        self.assertNotIn("cluster-restore-drill CONFIRM=yes", invoked)
        self.assertNotIn("cluster-monitoring-refresh", invoked)

    def test_build_skip_all_skips_everything(self):
        invoked = self._run_steps("seed app-host backup alerts")
        self.assertEqual(invoked, [])

if __name__ == "__main__":
    unittest.main()
