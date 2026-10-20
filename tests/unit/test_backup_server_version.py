#!/usr/bin/env python3
"""Metadane kopii niosą wersję serwera, który wyprodukował pliki.

Defekt 2026-10-20 (cassiopeiav18-r9): po major upgrade 11.4.13 -> 12.3.3
pierwsza kopia miała w metadanych `11.4.13`, bo runner brał wersję z
`config.json` renderowanego przy configure. Restore porównuje tę wartość z
wersją hosta odtwarzania, więc kłamstwo w metadanych wyłącza jego strażnika.
Źródłem jest teraz `mariadb_backup_info` zapisany przez samo mariadb-backup.
"""

import tempfile
import unittest
from pathlib import Path

from galera_backup_testlib import load_galera_backup_module

MOD = load_galera_backup_module()

INFO_12_3 = (
    "tool_name = mariadb-backup\n"
    "tool_version = 12.3.3-MariaDB\n"
    "ibbackup_version = 12.3.3-MariaDB\n"
    "server_version = 12.3.3-MariaDB\n"
)


class FakeRunner:
    """mariadb-backup --backup zostawia w katalogu docelowym pliki jak prawdziwe."""

    def __init__(self, backup_info):
        self.backup_info = backup_info

    def run(self, cmd, **_kwargs):
        if "--backup" in cmd:
            target = Path(next(a for a in cmd if a.startswith("--target-dir=")).split("=", 1)[1])
            (target / "mariadb_backup_galera_info").write_text("cluster-uuid 42\n", encoding="utf-8")
            if self.backup_info is not None:
                (target / "mariadb_backup_info").write_text(self.backup_info, encoding="utf-8")
        return 0, "", ""


class PhysicalBackupServerVersionTests(unittest.TestCase):
    def run_backup(self, backup_info):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            return MOD.perform_physical_backup(
                work, work / "datadir", work / "mysql.sock", FakeRunner(backup_info)
            )

    def test_server_version_comes_from_mariadb_backup_report(self):
        self.assertEqual(self.run_backup(INFO_12_3), ("cluster-uuid", "42", "12.3.3"))

    def test_missing_report_fails_instead_of_guessing_a_version(self):
        with self.assertRaises(MOD.BackupError) as ctx:
            self.run_backup(None)
        self.assertIn("mariadb_backup_info", str(ctx.exception))

    def test_report_without_server_version_fails(self):
        with self.assertRaises(MOD.BackupError) as ctx:
            self.run_backup("tool_name = mariadb-backup\n")
        self.assertIn("server_version", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
