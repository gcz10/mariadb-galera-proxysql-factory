"""Kontrakt wyjecia machines-from-elsewhere w probe-inventory-tf-consistency.

Deklaracja klastra lub platformy z `terraform_managed: false` oznacza hosty
dostarczone poza Terraformem. Sonda pomija wtedy wymog roota dla tej definicji;
bez pola lub z wartoscia true wymog pozostaje. Wyjecie musi byc jawne.
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


PLATFORM_INVENTORY = """\
all:
  children:
    proxysql:
      hosts:
        p1: { ansible_host: "172.24.10.21" }
        p2: { ansible_host: "172.24.10.22" }
    app:
      hosts:
        app1: { ansible_host: "172.24.10.23" }
    infra:
      hosts:
        infra1: { ansible_host: "172.24.10.24" }
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


class PlatformProvisioningOwnershipTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.platform_dir = self.root / "platform" / "manual-platform"
        self.platform_dir.mkdir(parents=True)
        (self.platform_dir / "inventory.yml").write_text(
            PLATFORM_INVENTORY, encoding="utf-8"
        )

    def test_external_platform_and_tenant_do_not_require_terraform_roots(self):
        (self.platform_dir / "platform.yml").write_text(
            "platform:\n  name: manual-platform\nterraform_managed: false\n",
            encoding="utf-8",
        )
        tenant_dir = self.root / "clusters" / "manual-tenant"
        tenant_dir.mkdir(parents=True)
        (tenant_dir / "inventory.yml").write_text(INVENTORY, encoding="utf-8")
        (tenant_dir / "cluster.yml").write_text(
            "cluster:\n  name: manual-tenant\nterraform_managed: false\n",
            encoding="utf-8",
        )
        self.assertEqual(probe_tf.scan(self.root), [])


    def test_managed_platform_requires_a_root_by_default_and_when_explicit(self):
        for ownership in ("", "terraform_managed: true\n"):
            with self.subTest(ownership=ownership or "omitted"):
                (self.platform_dir / "platform.yml").write_text(
                    "platform:\n  name: manual-platform\n" + ownership,
                    encoding="utf-8",
                )
                violations = probe_tf.scan(self.root)
                self.assertTrue(
                    any("brak terraform/manual-platform/main.tf" in v for v in violations),
                    violations,
                )

    def test_managed_platform_checks_inventory_drift_in_both_directions(self):
        (self.platform_dir / "platform.yml").write_text(
            "platform:\n  name: manual-platform\nterraform_managed: true\n",
            encoding="utf-8",
        )
        tf_dir = self.root / "terraform" / "manual-platform"
        tf_dir.mkdir(parents=True)
        main_tf = tf_dir / "main.tf"
        main_tf.write_text(
            "locals {\n  vms = {\n"
            "    p1 = {}\n    p2 = {}\n    app1 = {}\n    infra1 = {}\n"
            "  }\n}\n",
            encoding="utf-8",
        )
        self.assertEqual(probe_tf.scan(self.root), [])
        main_tf.write_text(
            "locals {\n  vms = {\n"
            "    p1 = {}\n    p2 = {}\n    app1 = {}\n    orphan = {}\n"
            "  }\n}\n",
            encoding="utf-8",
        )
        violations = probe_tf.scan(self.root)
        self.assertEqual(len(violations), 2)
        self.assertTrue(any("infra1" in v for v in violations), violations)
        self.assertTrue(any("orphan" in v for v in violations), violations)

    def test_self_test_supports_a_site_without_terraform_managed_hosts(self):
        (self.platform_dir / "platform.yml").write_text(
            "platform:\n  name: manual-platform\nterraform_managed: false\n",
            encoding="utf-8",
        )
        self.assertEqual(probe_tf.self_test(self.root), 0)


if __name__ == "__main__":
    unittest.main()
