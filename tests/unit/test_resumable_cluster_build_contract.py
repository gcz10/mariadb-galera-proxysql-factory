"""cluster-build wybiera fresh/converge ze stanu, nigdy z intencji operatora."""
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLAY = REPO / "playbooks" / "f2_preflight.yml"


class ResumablePreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = PLAY.read_text(encoding="utf-8")

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



if __name__ == "__main__":
    unittest.main()
