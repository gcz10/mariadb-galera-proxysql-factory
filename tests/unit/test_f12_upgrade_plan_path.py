#!/usr/bin/env python3
"""f12_upgrade_plan.yml: plan major upgrade'u idzie po jednym LTS na raz.

Defekt wyjściowy: generator planował rolling 11.4 -> 12.3 jednym krokiem,
choć Galera wspiera rolling wyłącznie o jeden major/LTS (mariadb.com/docs,
General Upgrade Information). Jednocześnie strażnik EOL blokował przystanek
11.8, więc jedyna wspierana ścieżka wymagała wyłączenia strażnika.

Prawdziwy `ansible-playbook` na play generującym plan (localhost). Fakty z
węzłów Galery, które w playbooku zbiera play read-only, podaje inwentarz
fixture'a; plan trafia do katalogu fixture'a, nie do repo.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "playbooks" / "f12_upgrade_plan.yml"
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")


@unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostępny w PATH")
class UpgradePlanPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="f12-plan-")
        cls.addClassCleanup(cls._tmp.cleanup)
        base = Path(cls._tmp.name)
        play_dir = base / "playbooks"
        play_dir.mkdir()
        os.symlink(REPO / "playbooks" / "vars", play_dir / "vars")
        os.symlink(REPO / "versions", base / "versions")
        plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
        plan_play = next(p for p in plays if p.get("hosts") == "localhost")
        (play_dir / "plan.yml").write_text(yaml.safe_dump([plan_play], sort_keys=False), encoding="utf-8")
        (base / "ansible.cfg").write_text(
            "[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n",
            encoding="utf-8",
        )
        cls.base = base
        cls.plan = base / "docs" / "plans" / "major-upgrade-plan.md"

    def run_plan(self, current, lock_file, *extra):
        inventory = {
            "all": {
                "vars": {"ansible_connection": "local"},
                "children": {
                    "galera": {
                        "hosts": {
                            "g1": {
                                "f12_mariadb_version": {"stdout": current},
                                "f12_gcache_size": {"stdout": "4G"},
                            }
                        }
                    }
                },
            }
        }
        inv = self.base / "inventory.yml"
        inv.write_text(yaml.safe_dump(inventory), encoding="utf-8")
        if self.plan.exists():
            self.plan.unlink()
        cluster = {"versions": {"lock_file": lock_file}, "galera": {"nodes_expected": 3}}
        env = os.environ.copy()
        env["ANSIBLE_CONFIG"] = str(self.base / "ansible.cfg")
        return subprocess.run(
            [ANSIBLE_PLAYBOOK, "-i", str(inv), str(self.base / "playbooks" / "plan.yml"),
             "-e", json.dumps(cluster), *extra],
            capture_output=True, text=True, env=env, timeout=180,
        )

    def test_11_4_plans_two_lts_stages_without_eol_override(self):
        proc = self.run_plan("11.4.13", "versions/versions.lock.yml")
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:])
        doc = self.plan.read_text(encoding="utf-8")
        self.assertIn("# Plan major upgrade: MariaDB 11.4.13 → 12.3 LTS", doc)
        self.assertIn("**Etapy:** 11.4 → 11.8 → 12.3.", doc)
        stage1 = doc.index("## Etap 1: 11.4 → 11.8")
        stage2 = doc.index("## Etap 2: 11.8 → 12.3")
        self.assertIn("`versions/versions-el9-118.lock.yml`.", doc[stage1:stage2])
        self.assertIn("`versions/versions-el9-123.lock.yml`.", doc[stage2:])
        self.assertNotIn("BRAK", doc)

    def test_cluster_on_target_series_gets_no_plan(self):
        proc = self.run_plan("12.3.3", "versions/versions-el9-123.lock.yml")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("jest już na serii 12.3", proc.stdout)
        self.assertFalse(self.plan.exists())

    def test_transit_series_as_final_target_still_shortens_support(self):
        proc = self.run_plan("11.4.13", "versions/versions.lock.yml", "-e", "f12_target=11.8")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("REGRESJA WSPARCIA", proc.stdout)
        self.assertFalse(self.plan.exists())

    def test_series_outside_lts_map_has_no_path(self):
        proc = self.run_plan("11.6.2", "versions/versions.lock.yml")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Brak ścieżki rolling z 11.6 do 12.3", proc.stdout)
        self.assertFalse(self.plan.exists())


if __name__ == "__main__":
    unittest.main()
