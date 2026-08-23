"""Kontrakt swiezosci dowodu cold recovery.

Plik wyboru wezla jest artefaktem jednego przebiegu, nie pamiecia ostatniego
sukcesu. Samo `test -s` przepuszczalo plik sprzed tygodnia, gdy Ansible konczyl
z rc=0 bez wykonania play (np. pusty/unparsowalny inventory).
"""

import configparser
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
VERIFY = REPO / "tests" / "validation" / "verify-recovery-state.py"
MAKEFILE = REPO / "Makefile"
PLAYBOOK = REPO / "playbooks" / "cluster_recover.yml"


class InventoryFailureModeTests(unittest.TestCase):
    def test_unparsed_inventory_is_fatal(self):
        config = configparser.ConfigParser()
        config.read(REPO / "ansible.cfg")
        self.assertTrue(config.getboolean("inventory", "unparsed_is_failed"))

    def test_ci_lint_uses_explicit_safe_inventory(self):
        workflow = yaml.safe_load(
            (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        lint_steps = workflow["jobs"]["lint"]["steps"]
        lint = next(step for step in lint_steps if step.get("run", "").startswith("ansible-lint"))
        self.assertEqual(
            lint["env"]["ANSIBLE_INVENTORY"],
            "clusters/example-cluster/inventory.yml",
        )


class RecoveryStateVerifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "recover-state.json"
        self.inventory = self.root / "inventory.yml"
        self.inventory.write_text(
            yaml.safe_dump(
                {
                    "all": {
                        "children": {
                            "galera": {
                                "hosts": {
                                    "gnode1": {"ansible_host": "10.0.0.1"},
                                    "gnode2": {"ansible_host": "10.0.0.2"},
                                }
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def write_state(self, **overrides):
        payload = {
            "run_id": "current-run",
            "generated_at": "2026-08-23T20:00:00Z",
            "node": "gnode2",
        }
        payload.update(overrides)
        self.state.write_text(json.dumps(payload), encoding="utf-8")

    def verify(self, run_id="current-run"):
        return subprocess.run(
            [
                "python3",
                str(VERIFY),
                str(self.state),
                run_id,
                str(self.inventory),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
        )

    def test_accepts_current_run_and_prints_only_selected_node(self):
        self.write_state()
        proc = self.verify()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "gnode2\n")

    def test_rejects_stale_run_id(self):
        self.write_state(run_id="last-week")
        proc = self.verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("run_id", proc.stderr)

    def test_rejects_legacy_plaintext_or_malformed_json(self):
        self.state.write_text("gnode1\n", encoding="utf-8")
        proc = self.verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("JSON", proc.stderr)

    def test_rejects_node_outside_inventory_galera_group(self):
        self.write_state(node="other-cluster-g1")
        proc = self.verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("galera", proc.stderr)

    def test_rejects_missing_timestamp(self):
        self.write_state(generated_at="")
        proc = self.verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("generated_at", proc.stderr)

    def test_rejects_inventory_node_unsafe_for_make_arguments(self):
        unsafe = "gnode1;touch-injected"
        self.inventory.write_text(
            yaml.safe_dump(
                {
                    "all": {
                        "children": {
                            "galera": {
                                "hosts": {
                                    unsafe: {"ansible_host": "10.0.0.1"},
                                }
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.write_state(node=unsafe)
        proc = self.verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("safe inventory identifier", proc.stderr)


class RecoveryStateProducerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.makefile = MAKEFILE.read_text(encoding="utf-8")
        cls.plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
        cls.select_play = next(
            play for play in cls.plays if "wybierz wezel bootstrap" in play["name"]
        )
        cls.write_task = next(
            task
            for task in cls.select_play["tasks"]
            if task.get("name", "").startswith("Zapisz wybrany wezel")
        )

    def test_make_removes_old_state_before_ansible(self):
        recipe_start = self.makefile.index("cluster-recover:")
        recipe_end = self.makefile.index("\ncluster-upgrade-plan:", recipe_start)
        recipe = self.makefile[recipe_start:recipe_end]
        self.assertIn('rm -f "$(RECOVER_STATE_FILE)"', recipe)
        self.assertLess(recipe.index("rm -f"), recipe.index("ansible-playbook"))

    def test_make_binds_playbook_and_verifier_to_same_run_id(self):
        recipe_start = self.makefile.index("cluster-recover:")
        recipe_end = self.makefile.index("\ncluster-upgrade-plan:", recipe_start)
        recipe = self.makefile[recipe_start:recipe_end]
        self.assertIn("recover_run_id", recipe)
        self.assertIn("verify-recovery-state.py", recipe)
        self.assertIn("$(RECOVER_RUN_ID)", recipe)
        self.assertIn("RECOVER_NODE_FILE", recipe)
        self.assertIn('> "$(RECOVER_NODE_FILE).tmp"', recipe)
        self.assertNotIn("test -s", recipe)
        self.assertNotIn("cat $(RECOVER_STATE_FILE)", recipe)
        self.assertNotIn('cat "$(RECOVER_STATE_FILE)"', recipe)

    def test_playbook_writes_structured_run_bound_state(self):
        copy = self.write_task["ansible.builtin.copy"]
        content = str(copy["content"])
        self.assertIn("recover_run_id", content)
        self.assertIn("generated_at", content)
        self.assertIn("recover_bootstrap_choice", content)
        self.assertIn("to_nice_json", content)
        self.assertEqual(copy["mode"], "0644")
        self.assertEqual(self.write_task["delegate_to"], "localhost")
        self.assertFalse(self.write_task["become"])


if __name__ == "__main__":
    unittest.main()
