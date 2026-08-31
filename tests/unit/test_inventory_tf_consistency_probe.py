"""Kontrakt wyjecia machines-from-elsewhere w probe-inventory-tf-consistency.

Cluster.yml z `terraform_managed: false` oznacza VM-y niezarzadzane przez
Terraform (runbook machines-from-elsewhere). Sonda MUSI wtedy pominac wymog
`terraform/<name>/main.tf`; bez pola — wymog stoi (wyjecie nie moze powstac
przez przypadek: brak pola + brak roota = naruszenie).
"""

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE / "tests" / "validation"))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "probe_tf", WORKSPACE / "tests" / "validation" / "probe-inventory-tf-consistency.py"
)
probe_tf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe_tf)

INVENTORY = """\
all:
  children:
    galera:
      hosts:
        g1: { ansible_host: "192.168.1.164" }
    restore:
      hosts:
        r1: { ansible_host: "192.168.1.167" }
"""

TF_MAIN = """\
locals {
  vms = {
    g1 = { id = 10004, ip = 164, role = "galera" }
    r1 = { id = 10007, ip = 167, role = "restore" }
  }
}
"""


class ManualProvisioningOptOutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "clusters" / "manual-r9").mkdir(parents=True)
        (self.root / "clusters" / "manual-r9" / "inventory.yml").write_text(INVENTORY)

    def _write_cluster_yml(self, body: str) -> None:
        (self.root / "clusters" / "manual-r9" / "cluster.yml").write_text(
            textwrap.dedent(body), encoding="utf-8"
        )

    def test_tf_managed_without_root_is_a_violation(self):
        self._write_cluster_yml('cluster:\n  name: "manual-r9"\n')
        violations = probe_tf.scan(self.root)
        self.assertTrue(
            any("brak terraform/manual-r9/main.tf" in v for v in violations),
            f"brak pola + brak roota = naruszenie (wyjecie musi byc jawne): {violations}",
        )

    def test_terraform_managed_false_skips_root_requirement(self):
        self._write_cluster_yml(
            'cluster:\n  name: "manual-r9"\nterraform_managed: false\n'
        )
        self.assertEqual(probe_tf.scan(self.root), [])

    def test_terraform_managed_with_root_stays_consistent(self):
        self._write_cluster_yml('cluster:\n  name: "manual-r9"\n')
        tf_dir = self.root / "terraform" / "manual-r9"
        tf_dir.mkdir(parents=True)
        (tf_dir / "main.tf").write_text(TF_MAIN)
        self.assertEqual(probe_tf.scan(self.root), [])


if __name__ == "__main__":
    unittest.main()
