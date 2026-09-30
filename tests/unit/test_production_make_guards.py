"""Make command-line variables cannot relabel production and reach lab mutators."""

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
import test_production_profile as production_fixtures

REPO = Path(__file__).resolve().parents[2]


@unittest.skipIf(shutil.which("make") is None, "make unavailable")
class ProductionMakeGuardTests(unittest.TestCase):
    def assert_refuses_mutator(self, kind, target, derived_variable):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Makefile").write_text((REPO / "Makefile").read_text())
            declaration = root / ("clusters" if kind == "cluster" else "platform") / "production-test"
            declaration.mkdir(parents=True)
            if kind == "cluster":
                configuration = production_fixtures.production_cluster()
            else:
                fixture = production_fixtures.ProductionPlatformPolicyTests()
                fixture.setUp()
                configuration = fixture.config
            (declaration / f"{kind}.yml").write_text(yaml.safe_dump(configuration))
            witness = root / "mutator-reached"
            program = "#!/usr/bin/env python3\nfrom pathlib import Path\nPath(" + repr(str(witness)) + ").write_text('mutated')\nraise SystemExit(1)\n"
            if kind == "cluster":
                mutator = root / "tests/lab/probe-idempotence.py"
            else:
                mutator = root / "bin/ansible-playbook"
            mutator.parent.mkdir(parents=True)
            mutator.write_text(program)
            mutator.chmod(0o700)
            environment = dict(os.environ)
            environment["PATH"] = str(root / "bin") + ":" + str(Path(sys.executable).parent) + ":" + environment["PATH"]
            environment["APP_DB_PASSWORD"] = secrets.token_urlsafe(24)
            environment["PMM_ADMIN_PASSWORD"] = secrets.token_urlsafe(24)
            result = subprocess.run([
                shutil.which("make"), target, f"{kind.upper()}=production-test",
                f"{derived_variable}=laboratory",
            ], cwd=root, env=environment, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(witness.exists(), "a lab mutator was reached through a Make variable override")
            self.assertIn("REFUSED", result.stdout + result.stderr)

    def test_cluster_environment_override_cannot_enter_mutating_lab_gate(self):
        self.assert_refuses_mutator("cluster", "lab-post-build-gate", "cluster_environment")

    def test_platform_environment_override_cannot_enter_local_pmm_backup(self):
        self.assert_refuses_mutator("platform", "platform-pmm-backup", "platform_environment")


if __name__ == "__main__":
    unittest.main()
