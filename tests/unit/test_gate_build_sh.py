"""Kontrakt bramki budowy (tests/validation/gate-build.sh, F4).

Makefile deleguje do skryptu polityke warunkowa budowy: sprzezenie seed->backup
i pomijanie krokow przez BUILD_SKIP. Kontrakt testujemy na preflight — czysta
logika, bez infrastruktury. `steps` woli make (kontrakt kolejnosci jest
orkiestracja Makefile i wymagalby zywego klastra), wiec tutaj go nie odpalamy.
"""

import subprocess
import unittest
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "validation" / "gate-build.sh"


def run_preflight(backup_enabled: str, build_skip: str, existing_data: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(GATE), "preflight", backup_enabled, build_skip, existing_data],
        capture_output=True,
        text=True,
        timeout=30,
    )


class GateBuildPreflightTest(unittest.TestCase):
    def test_script_exists_and_parses(self):
        self.assertTrue(GATE.is_file(), f"brak {GATE}")
        subprocess.run(["bash", "-n", str(GATE)], check=True, timeout=30)

    def test_seed_backup_skipped_together_is_legal(self):
        proc = run_preflight("true", "seed backup", "")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_seed_skipped_without_backup_refuses_without_existing_data(self):
        proc = run_preflight("true", "seed", "")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("EXISTING_DATA=yes", proc.stderr)

    def test_seed_skipped_with_declared_existing_data_is_legal(self):
        proc = run_preflight("true", "seed", "yes")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_backup_disabled_disarms_the_coupling(self):
        # Na klastrze bez backupu drill nie istnieje, wiec EXISTING_DATA jest
        # pytaniem o dane dla przebiegu, ktory sie nie odbędzie.
        proc = run_preflight("false", "seed", "")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_skips_is_legal(self):
        proc = run_preflight("true", "", "")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_unknown_subcommand_refuses(self):
        proc = subprocess.run([str(GATE), "nonsense"], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
