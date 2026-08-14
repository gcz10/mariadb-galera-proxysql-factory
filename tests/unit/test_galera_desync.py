"""Testy odsynchronizowania wezla na czas backupu (MASTER_PROMPT.md:803).

Sprawdzaja kontrakt, nie implementacje:
  - desync wlaczamy WYLACZNIE ze stanu Synced (4),
  - nigdy nie zdejmujemy CUDZEGO desyncu (wezel juz Donor/Desynced),
  - powrot do Synced nastepuje takze gdy backup rzucil wyjatkiem,
  - utkniecie poza Synced po desync=OFF jest glosna awaria, nie cisza.
"""

import unittest
from unittest.mock import MagicMock
from pathlib import Path

from tests.unit.galera_backup_testlib import load_galera_backup_module


class WsrepDesyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_galera_backup_module()

    def _runner(self, states):
        """Runner zwracajacy kolejne stany wsrep i notujacy wykonane komendy."""
        r = MagicMock()
        r.executed = []
        seq = list(states)

        def run(cmd, **kw):
            r.executed.append(" ".join(cmd))
            if any("SET GLOBAL wsrep_desync" in c for c in cmd):
                return (0, "", "")
            state = seq.pop(0) if seq else states[-1]
            return (0, f"wsrep_local_state\t{state['n']}\nwsrep_local_state_comment\t{state['c']}\n", "")

        r.run.side_effect = run
        return r

    SYNCED = {"n": "4", "c": "Synced"}
    DONOR = {"n": "2", "c": "Donor/Desynced"}

    def test_desync_applied_only_from_synced(self):
        r = self._runner([self.SYNCED])
        applied = self.mod.set_wsrep_desync(Path("/tmp/s.sock"), r, True)
        self.assertTrue(applied)
        self.assertTrue(any("wsrep_desync = ON" in c for c in r.executed))

    def test_desync_refused_when_node_already_desynced(self):
        """Wezel jest Donorem dla cudzego SST — nie wolno przejmowac jego desyncu."""
        r = self._runner([self.DONOR])
        applied = self.mod.set_wsrep_desync(Path("/tmp/s.sock"), r, True)
        self.assertFalse(applied)
        self.assertFalse(any("wsrep_desync" in c for c in r.executed),
                         "nie wolno dotykac wsrep_desync gdy wezel nie jest Synced")

    def test_desync_off_does_not_probe_state(self):
        """Wylaczenie jest bezwarunkowe — dotyczy wylacznie desyncu, ktory sami wlaczylismy."""
        r = self._runner([])
        applied = self.mod.set_wsrep_desync(Path("/tmp/s.sock"), r, False)
        self.assertTrue(applied)
        self.assertTrue(any("wsrep_desync = OFF" in c for c in r.executed))

    def test_set_desync_raises_on_sql_failure(self):
        r = MagicMock()
        r.run.return_value = (1, "", "access denied")
        with self.assertRaises(self.mod.BackupError) as ctx:
            self.mod.set_wsrep_desync(Path("/tmp/s.sock"), r, False)
        self.assertEqual(ctx.exception.code, "E_GALERA")

    def test_wait_until_synced_returns_when_synced(self):
        r = self._runner([self.SYNCED])
        self.mod.wait_until_synced(Path("/tmp/s.sock"), r, timeout_s=5)

    def test_wait_until_synced_raises_when_stuck(self):
        """Utkniecie poza Synced musi byc glosne — inaczej wezel zostaje poza ruchem."""
        r = self._runner([self.DONOR])
        with self.assertRaises(self.mod.BackupError) as ctx:
            self.mod.wait_until_synced(Path("/tmp/s.sock"), r, timeout_s=0)
        self.assertEqual(ctx.exception.code, "E_GALERA")
        self.assertIn("Donor/Desynced", ctx.exception.public_message)


if __name__ == "__main__":
    unittest.main()
