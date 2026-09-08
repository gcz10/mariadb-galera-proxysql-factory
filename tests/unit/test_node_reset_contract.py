"""Reset wezla kasuje dane, wiec jego bramki musza byc mierzone, nie deklarowane.

Kontekst (ISA.md, 2026-09-06): nieudany `cluster-build` zostawial host z datadirem
bez tozsamosci, a jedynym wyjsciem bylo skasowanie MASZYNY. `playbooks/node_reset.yml`
jest tania alternatywa — i dokladnie dlatego jego odmowy sa wazniejsze od jego
dzialania: playbook, ktory skasuje datadir o jeden przypadek za szeroko, jest
grozniejszy niz brak playbooka.

Bramki wejsciowe uruchamiamy NAPRAWDE (ansible-playbook na inwentarzu-atrapie).
Kolejnosc operacji destrukcyjnej sprawdzamy na strukturze zadan: wykonanie
wymagaloby roota i zywego klastra, a to, ze kasowanie stoi ZA dowodem
zatrzymanego procesu, jest niezmiennikiem, ktory musi przezyc kazda edycje.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLAY = REPO / "playbooks" / "node_reset.yml"

CLUSTER = {
    "cluster": {"name": "test-cluster", "environment": "laboratory"},
    "galera": {"cluster_name": "test_galera", "nodes_expected": 1},
}


def run_reset(inventory: dict, extra: list[str]) -> subprocess.CompletedProcess:
    """Uruchamia prawdziwy playbook na inwentarzu-atrapie (bez SSH, bez roota)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "inventory.yml").write_text(yaml.safe_dump(inventory), encoding="utf-8")
        (root / "cluster.yml").write_text(yaml.safe_dump(CLUSTER), encoding="utf-8")
        return subprocess.run(
            [
                "ansible-playbook",
                str(PLAY),
                "-i",
                str(root / "inventory.yml"),
                "-e",
                f"@{root / 'cluster.yml'}",
                # Atrapa nie ma sudo — bez tego playbook padalby na eskalacji
                # uprawnien, czyli na srodowisku, zamiast na mierzonej bramce.
                "-e",
                "ansible_become=false",
                *extra,
            ],
            capture_output=True,
            text=True,
            cwd=REPO,
        )


def inventory(hosts: list[str]) -> dict:
    return {
        "all": {
            "children": {
                "galera": {
                    "hosts": {
                        host: {
                            "ansible_connection": "local",
                            "galera_node_address": "127.0.0.1",
                        }
                        for host in hosts
                    }
                }
            }
        }
    }


@unittest.skipUnless(shutil.which("ansible-playbook"), "brak ansible-playbook")
class NodeResetGuardTests(unittest.TestCase):
    def test_reset_without_confirmation_is_refused(self):
        result = run_reset(inventory(["g1", "g2"]), ["-e", "node=g1"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confirm=yes", result.stdout)

    def test_reset_of_host_outside_galera_is_refused(self):
        result = run_reset(inventory(["g1", "g2"]), ["-e", "node=proxy1", "-e", "confirm=yes"])
        self.assertNotEqual(result.returncode, 0)

    def test_single_node_cluster_cannot_be_reset(self):
        """Bez drugiego wezla kasowanie datadiru to utrata danych, nie reset."""
        result = run_reset(inventory(["g1"]), ["-e", "node=g1", "-e", "confirm=yes"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("jedynym wezlem", result.stdout)

    def test_unreadable_donor_state_stops_the_reset(self):
        """Brak dowodu, ze dane sa gdzie indziej, musi zatrzymac operacje.

        Atrapa nie ma dzialajacej bazy, wiec odczyt stanu dawcy sie nie udaje —
        i wlasnie ten przypadek nie moze przejsc dalej.
        """
        result = run_reset(inventory(["g1", "g2"]), ["-e", "node=g1", "-e", "confirm=yes"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nie ma dowodu", result.stdout)
        self.assertNotIn("Skasuj datadir", result.stdout)


class NodeResetOrderTests(unittest.TestCase):
    """Niezmienniki kolejnosci: kasowanie jest ostatnie, po wszystkich dowodach."""

    @classmethod
    def setUpClass(cls):
        plays = yaml.safe_load(PLAY.read_text(encoding="utf-8"))
        cls.destructive = plays[-1]["tasks"]
        cls.names = [task.get("name", "") for task in cls.destructive]

    def index_of(self, needle: str) -> int:
        for position, name in enumerate(self.names):
            if needle in name:
                return position
        raise AssertionError(f"brak zadania zawierajacego '{needle}': {self.names}")

    def test_datadir_is_deleted_only_after_process_and_identity_proofs(self):
        deletion = self.index_of("Skasuj datadir")
        self.assertLess(self.index_of("nalezacego do innego klastra"), deletion)
        self.assertLess(self.index_of("pod dzialajacym procesem"), deletion)

    def test_process_check_result_actually_gates_the_deletion(self):
        """Sam fakt, ze assert stoi wyzej, nic nie znaczy — musi patrzec na pomiar."""
        guard = self.destructive[self.index_of("pod dzialajacym procesem")]
        conditions = guard["ansible.builtin.assert"]["that"]
        self.assertTrue(any("reset_running.rc" in condition for condition in conditions))


if __name__ == "__main__":
    unittest.main()
