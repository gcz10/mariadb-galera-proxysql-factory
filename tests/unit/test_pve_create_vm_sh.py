"""Testy kontraktu skryptu tools/pve-create-vm.sh.

Weryfikacja składni bash, obsługi flag --help, walidacji wymaganych argumentów,
walidacji formatu IP oraz obecności zmiennych środowiskowych PVE.
"""

import os
import subprocess
import tempfile
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


class PveCreateVmScriptBehavioralExecutionTests(unittest.TestCase):
    """Behawioralne testy pętli oczekiwania SSH i kodów wyjścia (WATCHDOG §5)."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.td_path = Path(self.td.name)

        # Mock curl emulujący odpowiedzi Proxmox VE REST API
        self.mock_bin = self.td_path / "bin"
        self.mock_bin.mkdir()
        mock_curl = self.mock_bin / "curl"
        mock_curl.write_text(
            "#!/usr/bin/env bash\n"
            "is_mutation=0\n"
            "for arg in \"$@\"; do\n"
            "  case \"$arg\" in\n"
            "    POST|PUT) is_mutation=1 ;;\n"
            "  esac\n"
            "done\n"
            "if [ \"$is_mutation\" -eq 1 ]; then\n"
            "  echo '{\"data\":\"UPID:pve:0001:0002:6A000000:task:10020:root@pam!isa-tf:\"}'\n"
            "  exit 0\n"
            "fi\n"
            "for arg in \"$@\"; do\n"
            "  case \"$arg\" in\n"
            "    */tasks/*/status)\n"
            "      echo '{\"data\":{\"status\":\"stopped\",\"exitstatus\":\"OK\"}}'\n"
            "      exit 0\n"
            "      ;;\n"
            "    */qemu|*/local-zfs/content)\n"
            "      echo '{\"data\":[]}'\n"
            "      exit 0\n"
            "      ;;\n"
            "  esac\n"
            "done\n"
            "echo '{\"data\":[]}'\n",
            encoding="utf-8",
        )
        mock_curl.chmod(0o755)

        # Atrapa klucza SSH
        self.key_file = self.td_path / "ssh_key.pub"
        self.key_file.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI-test-key test@lab\n", encoding="utf-8")

        self.env = {
            "PATH": f"{self.mock_bin}:{os.environ.get('PATH', '/bin:/usr/bin')}",
            "PROXMOX_VE_ENDPOINT": "https://127.0.0.1:8006",
            "PROXMOX_VE_API_TOKEN": "root@pam!token=secret",
            "PVE_SSH_WAIT_RETRIES": "2",
            "PVE_SSH_WAIT_SLEEP": "0",
        }

    def test_ssh_timeout_fails_closed_with_exit_1(self):
        """Czerwony test kontraktu: brak portu 22 w budżecie czasu MUSI kończyć się kodem 1 (fail-closed)."""
        proc = subprocess.run(
            [
                str(SCRIPT),
                "--vmid", "10020",
                "--name", "c12db1",
                "--ip", "192.168.1.254",
                "--cluster", "test-cluster",
                "--key-file", str(self.key_file),
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode,
            1,
            f"Timeout SSH musi zwracać exit 1 (fail-closed), otrzymano: {proc.returncode}. Output:\n{proc.stdout}\n{proc.stderr}",
        )
        self.assertIn("BŁĄD: Maszyna wystartowała, ale port 22", proc.stderr)
        self.assertNotIn("exit 0", proc.stderr)

    def test_no_wait_ssh_flag_succeeds_with_exit_0(self):
        """Zielony test kontraktu: jawna flaga --no-wait-ssh pomija pętlę i kończy się kodem 0."""
        proc = subprocess.run(
            [
                str(SCRIPT),
                "--vmid", "10020",
                "--name", "c12db1",
                "--ip", "192.168.1.254",
                "--cluster", "test-cluster",
                "--key-file", str(self.key_file),
                "--no-wait-ssh",
            ],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"--no-wait-ssh musi zwracać exit 0, otrzymano: {proc.returncode}. Stderr:\n{proc.stderr}",
        )
        self.assertIn("Pominięto oczekiwanie na SSH (--no-wait-ssh)", proc.stdout)
    def test_invalid_wait_env_variables_fallback_safely(self):
        """Niepoprawne zmienne środowiskowe PVE_SSH_WAIT_* bezpiecznie powracają do wartości domyślnych."""
        bad_env = dict(self.env)
        bad_env["PVE_SSH_WAIT_RETRIES"] = "-5"
        bad_env["PVE_SSH_WAIT_SLEEP"] = "invalid"
        proc = subprocess.run(
            [
                str(SCRIPT),
                "--vmid", "10020",
                "--name", "c12db1",
                "--ip", "192.168.1.254",
                "--cluster", "test-cluster",
                "--key-file", str(self.key_file),
                "--no-wait-ssh",
            ],
            capture_output=True,
            text=True,
            env=bad_env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)

    def test_static_script_contract_has_fail_closed_exit(self):
        """Weryfikacja kodu skryptu: gałąź timeoutu musi kończyć się exit 1."""
        body = SCRIPT.read_text(encoding="utf-8")
        timeout_branch = body[body.find("echo \"BŁĄD: Maszyna wystartowała") :]
        self.assertIn("exit 1", timeout_branch)
        self.assertNotIn("exit 0", timeout_branch[: timeout_branch.find("else")])
if __name__ == "__main__":
    unittest.main()
