"""Testy kontraktu skryptu tools/pve-create-vm.sh.

Weryfikacja składni bash, obsługi flag --help, walidacji wymaganych argumentów,
walidacji formatu IP oraz obecności zmiennych środowiskowych PVE.
"""

import os
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "pve-create-vm.sh"


class PveCreateVmScriptContractTests(unittest.TestCase):
    def test_script_exists_and_syntax_ok(self):
        self.assertTrue(SCRIPT.is_file(), f"Brak pliku {SCRIPT}")
        self.assertTrue(os.access(SCRIPT, os.X_OK), f"Plik {SCRIPT} nie jest wykonywalny")
        proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Błąd składni bash: {proc.stderr}")

    def test_help_flag_succeeds_and_documents_options(self):
        for flag in ["-h", "--help"]:
            with self.subTest(flag=flag):
                proc = subprocess.run([str(SCRIPT), flag], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0)
                self.assertIn("Użycie:", proc.stdout)
                self.assertIn("--vmid", proc.stdout)
                self.assertIn("--name", proc.stdout)
                self.assertIn("--ip", proc.stdout)
                self.assertIn("--cluster", proc.stdout)
                self.assertIn("--role", proc.stdout)

    def test_missing_required_parameters_fail_fast(self):
        cases = [
            ([], "BŁĄD: --vmid jest wymagany"),
            (["--vmid", "10020"], "BŁĄD: --name jest wymagany"),
            (["--vmid", "10020", "--name", "c12db1"], "BŁĄD: --ip jest wymagany"),
            (["--vmid", "10020", "--name", "c12db1", "--ip", "40"], "BŁĄD: --cluster jest wymagany"),
        ]
        for args, expected_err in cases:
            with self.subTest(args=args):
                proc = subprocess.run([str(SCRIPT)] + args, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 2)
                self.assertIn(expected_err, proc.stderr)

    def test_invalid_ip_format_rejected(self):
        cases = ["abc", "192.168.1", "192.168.1.999.1", "foo.bar"]
        for bad_ip in cases:
            with self.subTest(bad_ip=bad_ip):
                proc = subprocess.run(
                    [str(SCRIPT), "--vmid", "10020", "--name", "c12db1", "--ip", bad_ip, "--cluster", "test-c"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 2)
                self.assertIn("BŁĄD: Niepoprawny format IP", proc.stderr)

    def test_unknown_parameter_rejected(self):
        proc = subprocess.run([str(SCRIPT), "--unknown-flag"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Nieznany parametr", proc.stderr)

    def test_missing_environment_fails_closed(self):
        env = {
            "PATH": os.environ.get("PATH", "/bin:/usr/bin"),
        }
        # Brak PROXMOX_VE_ENDPOINT i PROXMOX_VE_API_TOKEN
        proc = subprocess.run(
            [str(SCRIPT), "--vmid", "10020", "--name", "c12db1", "--ip", "40", "--cluster", "test-c"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PROXMOX_VE_ENDPOINT", proc.stderr)


if __name__ == "__main__":
    unittest.main()
