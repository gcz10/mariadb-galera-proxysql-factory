import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = WORKSPACE_ROOT / "tests" / "lab" / "probe-backup.py"


class ProbeBackupInventoryTests(unittest.TestCase):
    def test_alias_only_group_does_not_crash_or_override_host_address(self):
        cluster = {
            "cluster": {"name": "example"},
            "backup": {
                "s3": {
                    "endpoint": "gnode1:9000",
                    "bucket": "example-backups",
                    "secure": False,
                }
            },
        }
        inventory = {
            "all": {
                "children": {
                    "galera": {
                        "hosts": {
                            "gnode1": {"ansible_host": "127.0.0.1"},
                        }
                    },
                    "discovery_bench": {
                        "hosts": {"gnode1": None},
                    },
                }
            }
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cluster_path = root / "cluster.yml"
            inventory_path = root / "inventory.yml"
            cluster_path.write_text(yaml.safe_dump(cluster), encoding="utf-8")
            inventory_path.write_text(yaml.safe_dump(inventory), encoding="utf-8")
            env = {
                "CLUSTER_CONFIG": str(cluster_path),
                "CLUSTER_INVENTORY": str(inventory_path),
            }
            with patch.dict(os.environ, env, clear=False):
                try:
                    namespace = runpy.run_path(str(PROBE_PATH))
                except AttributeError as exc:
                    self.fail(f"Alias-only inventory host crashed probe parsing: {exc}")

        self.assertEqual(namespace["inventory_addresses"]["gnode1"], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
