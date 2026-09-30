"""Rendered commit durability: safe default and explicit non-production tuning."""

import unittest
from pathlib import Path

import jinja2

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "roles" / "mariadb_install" / "templates" / "server.cnf.j2"


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



if __name__ == "__main__":
    unittest.main()
