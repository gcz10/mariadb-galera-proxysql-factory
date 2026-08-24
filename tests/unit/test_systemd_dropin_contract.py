"""Ten sam systemd drop-in nie moze oscylowac miedzy etapami F2 i F5."""

import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DEST = "/etc/systemd/system/mariadb.service.d/timeoutstartsec.conf"


def dropin(path):
    plays = yaml.safe_load(path.read_text(encoding="utf-8"))
    for play in plays:
        for task in play.get("tasks", []):
            copy = task.get("ansible.builtin.copy") or {}
            if copy.get("dest") == DEST:
                return copy
    raise AssertionError(f"brak {DEST} w {path}")


class CanonicalMariaDBDropinTests(unittest.TestCase):
    def test_install_and_join_render_byte_identical_dropin(self):
        install = dropin(REPO / "playbooks" / "f2_install.yml")
        join = dropin(REPO / "playbooks" / "f5_join.yml")
        self.assertEqual(
            install["content"],
            join["content"],
            "F5 i F2 przepisuja ten sam plik na przemian; drugi converge nie jest no-op",
        )
        self.assertEqual(install["mode"], join["mode"])
        self.assertEqual(install["owner"], join["owner"])
        self.assertEqual(install["group"], join["group"])


if __name__ == "__main__":
    unittest.main()
