"""cluster-build wybiera fresh/converge ze stanu, nigdy z intencji operatora."""
import unittest
from pathlib import Path
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAY = REPO / "playbooks" / "f2_preflight.yml"
MAKE = REPO / "Makefile"


class ResumablePreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PLAY.read_text(encoding="utf-8")
        cls.tasks = yaml.safe_load(cls.text)[0]["tasks"]

    def test_mode_is_derived_from_observed_state(self):
        self.assertIn("preflight_effective_mode", self.text)
        self.assertIn("mariadb_pkgs | length == 0", self.text)
        self.assertIn("mariadb_datadir.stat.exists", self.text)
        self.assertIn("mariadb_proc.stdout == 'STOPPED'", self.text)
        self.assertNotIn("PREFLIGHT_MODE", self.text)

    def test_converge_checks_pinned_version(self):
        self.assertIn("MariaDB-server", self.text)
        self.assertIn("lock.mariadb.version", self.text)
        self.assertIn("Build nie wykonuje ukrytego upgrade", self.text)

    def test_existing_datadir_requires_cluster_identity(self):
        self.assertIn("wsrep_cluster_name", self.text)
        self.assertIn("galera.cluster_name", self.text)
        self.assertIn("Odmowa bez wipe", self.text)

    def test_partial_install_without_datadir_does_not_require_config_identity(self):
        identity_tasks = [t for t in self.tasks if "tozsamosc" in t.get("name", "")]
        self.assertTrue(identity_tasks)
        for task in identity_tasks:
            self.assertIn("mariadb_datadir.stat.exists", task.get("when", []))

    def test_direct_bootstrap_stays_fail_closed_but_build_may_skip_existing_primary(self):
        bootstrap = (REPO / "playbooks" / "bootstrap.yml").read_text(encoding="utf-8")
        self.assertIn("bootstrap_skip_existing_primary", bootstrap)
        self.assertIn("ansible.builtin.meta: end_play", bootstrap)
        build = MAKE.read_text(encoding="utf-8").split("cluster-build:", 1)[1].split("cluster-discover:", 1)[0]
        self.assertIn("bootstrap_skip_existing_primary=true", build)
        direct = MAKE.read_text(encoding="utf-8").split("cluster-bootstrap:", 1)[1].split("cluster-join:", 1)[0]
        self.assertNotIn("bootstrap_skip_existing_primary=true", direct)

    def test_cluster_build_keeps_using_validate_without_fresh_override(self):
        body = MAKE.read_text(encoding="utf-8")
        recipe = body.split("cluster-build:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("$(MAKE) cluster-validate", recipe)
        self.assertNotIn("PREFLIGHT_MODE=fresh", recipe)


if __name__ == "__main__":
    unittest.main()
