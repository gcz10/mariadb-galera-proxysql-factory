import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = WORKSPACE_ROOT / "tests" / "lab" / "probe-backup.py"


class ProbeBackupInventoryTests(unittest.TestCase):
    @staticmethod
    def _run_probe(cluster, inventory, env_extra=None):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cluster_path = root / "cluster.yml"
            inventory_path = root / "inventory.yml"
            cluster_path.write_text(yaml.safe_dump(cluster), encoding="utf-8")
            inventory_path.write_text(yaml.safe_dump(inventory), encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "CLUSTER": cluster["cluster"]["name"],
                    "CLUSTER_CONFIG": str(cluster_path),
                    "CLUSTER_INVENTORY": str(inventory_path),
                }
            )
            env.update(env_extra or {})
            return subprocess.run(
                [sys.executable, str(PROBE_PATH)],
                cwd=WORKSPACE_ROOT,
                capture_output=True,
                text=True,
                env=env,
            )

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
                "CLUSTER": "example",
                "CLUSTER_CONFIG": str(cluster_path),
                "CLUSTER_INVENTORY": str(inventory_path),
            }
            # runpy.run_path nie dodaje katalogu skryptu do sys.path (w przeciwienstwie
            # do `python3 sciezka/sonda.py`), a sonda importuje `_probe_common`
            # z wlasnego katalogu. Bez tego test lamalby probe, ktora w realnym
            # uruchomieniu dziala.
            sys.path.insert(0, str(PROBE_PATH.parent))
            with patch.dict(os.environ, env, clear=False):
                try:
                    namespace = runpy.run_path(str(PROBE_PATH))
                except AttributeError as exc:
                    self.fail(f"Alias-only inventory host crashed probe parsing: {exc}")
                finally:
                    sys.path.remove(str(PROBE_PATH.parent))

        self.assertEqual(namespace["inventory_addresses"]["gnode1"], "127.0.0.1")

    def test_disabled_backup_without_s3_block_skips_before_storage_parsing(self):
        """Wylaczona kopia nie ma backendu do parsowania.

        Szablon z `destination: smb` i `enabled: false` legalnie nie zawiera
        bloku `s3`. Sonda ma podjac decyzje SKIP przed jakimkolwiek odczytem
        pól konkretnego magazynu — inaczej kazde kolejne pole daje osobny
        `KeyError`, co pokazaly dwa kolejne przebiegi sigma-r9.
        """
        cluster = {
            "cluster": {"name": "disabled-backup"},
            "backup": {"enabled": False, "destination": "smb"},
        }
        inventory = {"all": {"children": {}}}

        result = self._run_probe(cluster, inventory)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SKIP: backup wylaczony", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_enabled_smb_backup_is_undetermined_not_green(self):
        """Brak sondy backendu jest jawnym brakiem pomiaru, nie PASS."""
        cluster = {
            "cluster": {"name": "smb-backup"},
            "backup": {"enabled": True, "destination": "smb"},
        }
        result = self._run_probe(cluster, {"all": {"children": {}}})
        output = result.stdout + result.stderr

        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("UNDETERMINED", output)
        self.assertIn("brak sondy dla destination=smb", output)
        self.assertNotIn("Traceback", output)

    def test_enabled_s3_path_uses_resolved_local_configuration(self):
        """Aktywna sciezka S3 nie odwoluje sie do usunietych globali.

        Niedostepny endpoint ma dac kontrolowane UNDETERMINED, nie `NameError`.
        """
        cluster = {
            "cluster": {"name": "s3-backup"},
            "backup": {
                "enabled": True,
                "destination": "s3",
                "s3": {
                    "endpoint": "127.0.0.1:1",
                    "bucket": "s3-backup",
                    "secure": False,
                },
            },
        }
        result = self._run_probe(
            cluster,
            {"all": {"children": {}}},
            {
                "GALERA_BACKUP_S3_ACCESS_KEY": "access-key",
                "GALERA_BACKUP_S3_SECRET_KEY": "secret-key-value",
            },
        )
        output = result.stdout + result.stderr

        self.assertNotEqual(result.returncode, 0, output)
        self.assertNotIn("NameError", output)
        self.assertNotIn("Traceback", output)
        self.assertIn("S3 127.0.0.1:1 nie odpowiada", output)


if __name__ == "__main__":
    unittest.main()
