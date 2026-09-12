#!/usr/bin/env python3
"""Odwolanie starego klucza MinIO wymaga KONWERGENCJI WSZYSTKICH konsumentow.

ZMIERZONE 2026-09-12 lokalnym przebiegiem grafu play'ow z `f10_backup.yml`:
kandydat konczyl dystrybucje nowego sekretu bledem, a koordynator i tak wykonywal
ostatni play i odwolywal stare konta serwisowe. Domyslnie Ansible usuwa z rotacji
tylko host, ktory zawiodl, i wykonuje kolejne play'e na pozostalych
(docs/docsite/rst/playbook_guide/playbooks_error_handling.rst), wiec sama
KOLEJNOSC play'ow nie jest bramka — komentarz w playbooku obiecywal gwarancje,
ktorej plik nie egzekwowal.

Skutek na flocie: wezel zostaje z kluczem juz odwolanym w MinIO, a poniewaz cron
stoi na kazdym kandydacie i sam rozstrzyga elekcje donora, jego kopia pada na
uwierzytelnieniu az do nastepnego CZYSTEGO `configure`.

Test wykonuje PRAWDZIWY `ansible-playbook` na kopii grafu play'ow: zachowuje
nazwy, wzorce `hosts`, polityke bledu i warunek `when` zadania odwolujacego, a
ciala zadan zastepuje markerem (wzorzec z test_monitor_rotation_contract.py).
Marker za ostatnim play'em jest jedynym dowodem "revoke dopuszczony".
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "playbooks" / "f10_backup.yml"

SCHEDULER_PLAY = "Configure or run Galera backup on the selected scheduler"
CANDIDATES_PLAY = "Pre-position backup runner and cron on remaining Galera candidates"
RESTORE_PLAY = "Pre-position restore executable and scoped secrets"
REVOKE_PLAY = "Revoke stale MinIO service accounts after every consumer converged"

INVENTORY = {
    "all": {
        "vars": {
            "ansible_connection": "local",
            "ansible_become": False,
        },
        "children": {
            "galera": {"hosts": {"g1": {}, "g2": {}, "g3": {}}},
            "restore": {"hosts": {"r1": {}}},
        },
    }
}


@unittest.skipUnless(shutil.which("ansible-playbook"), "brak ansible-playbook")
class RevokeRequiresFullConvergenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plays = {
            play["name"]: play
            for play in yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
        }
        for name in (SCHEDULER_PLAY, CANDIDATES_PLAY, RESTORE_PLAY, REVOKE_PLAY):
            if name not in cls.plays:
                raise AssertionError(f"playbook nie ma juz play'a '{name}'")

    def build(self, workdir, failing_host):
        """Kopia grafu play'ow: oryginalne `hosts`/polityka bledu, zadania-atrapy."""
        marker = workdir / "revoke-reached"
        revoke_when = self.plays[REVOKE_PLAY]["tasks"][0]["when"]
        plays = []
        for name in (SCHEDULER_PLAY, CANDIDATES_PLAY, RESTORE_PLAY, REVOKE_PLAY):
            source = self.plays[name]
            play = {
                "name": name,
                "hosts": source["hosts"],
                "become": False,
                "gather_facts": False,
                "vars": {
                    "backup": {"scheduler": {"host": "g1"}},
                    "cluster": {"name": "review"},
                    "galera_backup_action": "configure",
                },
            }
            for keyword in ("any_errors_fatal", "serial", "max_fail_percentage"):
                if keyword in source:
                    play[keyword] = source[keyword]
            if name == REVOKE_PLAY:
                play["tasks"] = [
                    {
                        "name": "Marker: revoke starych kont MinIO dopuszczony",
                        "ansible.builtin.copy": {
                            "dest": str(marker),
                            "content": "revoke reached\n",
                        },
                        "when": revoke_when,
                    }
                ]
            elif failing_host and name == CANDIDATES_PLAY:
                play["tasks"] = [
                    {
                        "name": "Dystrybucja nowego sekretu na kandydacie",
                        "ansible.builtin.fail": {
                            "msg": "symulowana awaria dystrybucji sekretu"
                        },
                        "when": f"inventory_hostname == '{failing_host}'",
                    }
                ]
            else:
                play["tasks"] = [
                    {"ansible.builtin.debug": {"msg": "konwergencja konsumenta"}}
                ]
            plays.append(play)

        inventory = workdir / "inventory.yml"
        inventory.write_text(yaml.safe_dump(INVENTORY), encoding="utf-8")
        playbook = workdir / "rotation.yml"
        playbook.write_text(yaml.safe_dump(plays, sort_keys=False), encoding="utf-8")
        return playbook, inventory, marker

    def run_graph(self, failing_host=None):
        workdir = Path(tempfile.mkdtemp(prefix="rotation-ordering-"))
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        playbook, inventory, marker = self.build(workdir, failing_host)
        result = subprocess.run(
            ["ansible-playbook", "-i", str(inventory), str(playbook)],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(workdir),
        )
        return result, marker

    def test_failed_consumer_stops_the_run_before_revoke(self):
        result, marker = self.run_graph(failing_host="g2")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(
            marker.exists(),
            "stare konto MinIO zostalo odwolane mimo kandydata bez nowego klucza",
        )

    def test_clean_run_still_reaches_revoke(self):
        result, marker = self.run_graph()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(
            marker.exists(),
            "czysta konwergencja musi nadal odwolywac stare konta (inaczej rosna w MinIO)",
        )


if __name__ == "__main__":
    unittest.main()
