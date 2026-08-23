"""Kontrakt trwałości redo: bezpieczny default, jawny wyjątek produkcyjny."""

import copy
import subprocess
import tempfile
import unittest
from pathlib import Path

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "roles" / "mariadb_install" / "templates" / "server.cnf.j2"
SCHEMA = REPO / "clusters" / "schema" / "cluster.schema.json"
EXAMPLE = REPO / "clusters" / "example-cluster" / "cluster.yml"
VALIDATOR = REPO / "tests" / "validation" / "validate-cluster-schema.py"


class DurabilityTemplateDefaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        line = next(
            line
            for line in TEMPLATE.read_text(encoding="utf-8").splitlines()
            if line.startswith("innodb_flush_log_at_trx_commit =")
        )
        cls.expression = line.split("=", 1)[1].strip()

    def render(self, tuning):
        return jinja2.Template(self.expression).render(mariadb_tuning=tuning).strip()

    def test_omitted_setting_defaults_to_full_commit_durability(self):
        self.assertEqual(self.render({}), "1")

    def test_laboratory_can_explicitly_opt_out(self):
        self.assertEqual(self.render({"innodb_flush_log_at_trx_commit": 0}), "0")


    def test_existing_container_labs_pin_their_previous_opt_out(self):
        for name in ("lab-cluster", "lab2-cluster"):
            config = yaml.safe_load(
                (REPO / "clusters" / name / "cluster.yml").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                config["mariadb_tuning"]["innodb_flush_log_at_trx_commit"],
                0,
                f"{name} musi zachowac poprzednia efektywna wartosc jawnie",
            )

class ProductionDurabilityValidatorTests(unittest.TestCase):
    def setUp(self):
        self.base = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
        self.base["cluster"]["environment"] = "production"
        self.base["cluster"]["profile"] = "production"
        self.base["versions"]["policy"] = "locked"
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def validate(self, cluster):
        path = Path(self.tmp.name) / "cluster.yml"
        path.write_text(yaml.safe_dump(cluster), encoding="utf-8")
        return subprocess.run(
            ["python3", str(VALIDATOR), str(path), str(SCHEMA)],
            cwd=REPO,
            capture_output=True,
            text=True,
        )

    def test_production_rejects_reduced_durability_without_acceptance(self):
        cluster = copy.deepcopy(self.base)
        cluster["mariadb_tuning"]["innodb_flush_log_at_trx_commit"] = 0
        cluster["mariadb_tuning"].pop("durability_risk_accepted", None)
        proc = self.validate(cluster)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("durability_risk_accepted", proc.stdout + proc.stderr)

    def test_production_accepts_explicit_machine_readable_exception(self):
        cluster = copy.deepcopy(self.base)
        cluster["mariadb_tuning"]["innodb_flush_log_at_trx_commit"] = 0
        cluster["mariadb_tuning"]["durability_risk_accepted"] = True
        proc = self.validate(cluster)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_production_omission_is_safe_and_needs_no_exception(self):
        cluster = copy.deepcopy(self.base)
        cluster["mariadb_tuning"].pop("innodb_flush_log_at_trx_commit", None)
        cluster["mariadb_tuning"].pop("durability_risk_accepted", None)
        proc = self.validate(cluster)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_laboratory_explicit_zero_remains_valid(self):
        cluster = copy.deepcopy(self.base)
        cluster["cluster"]["environment"] = "laboratory"
        cluster["cluster"]["profile"] = "laboratory"
        cluster["versions"]["policy"] = "candidate"
        cluster["mariadb_tuning"]["innodb_flush_log_at_trx_commit"] = 0
        cluster["mariadb_tuning"].pop("durability_risk_accepted", None)
        proc = self.validate(cluster)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_risk_acceptance_must_be_boolean(self):
        cluster = copy.deepcopy(self.base)
        cluster["mariadb_tuning"]["innodb_flush_log_at_trx_commit"] = 0
        cluster["mariadb_tuning"]["durability_risk_accepted"] = "yes"
        proc = self.validate(cluster)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("durability_risk_accepted", proc.stdout + proc.stderr)

    def test_flush_value_is_limited_to_mariadb_modes(self):
        cluster = copy.deepcopy(self.base)
        cluster["mariadb_tuning"]["innodb_flush_log_at_trx_commit"] = 3
        proc = self.validate(cluster)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("innodb_flush_log_at_trx_commit", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
