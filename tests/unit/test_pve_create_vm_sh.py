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
        self.curl_log = self.td_path / "curl.log"

        mock_curl = self.mock_bin / "curl"
        mock_curl.write_text(
            "#!/usr/bin/env bash\n"
            "if [ -n \"${CURL_LOG:-}\" ]; then\n"
            "  for a in \"$@\"; do\n"
            "    if [[ \"$a\" == *\"PVEAPIToken\"* ]]; then\n"
            "      echo \"TOKEN_LEAK_IN_ARGV: $a\" >> \"$CURL_LOG\"\n"
            "    fi\n"
            "  done\n"
            "  echo \"curl-called\" >> \"$CURL_LOG\"\n"
            "fi\n"
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

        # Deterministic mock timeout — eliminuje zależność od sieci/zewnętrznego IP
        mock_timeout = self.mock_bin / "timeout"
        mock_timeout.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"${MOCK_SSH_PROBE_SUCCESS:-0}\" = \"1\" ]; then\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        mock_timeout.chmod(0o755)

        # Atrapa klucza SSH
        self.key_file = self.td_path / "ssh_key.pub"
        self.key_file.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI-test-key test@lab\n", encoding="utf-8")

        self.env = {
            "PATH": f"{self.mock_bin}:{os.environ.get('PATH', '/bin:/usr/bin')}",
            "PROXMOX_VE_ENDPOINT": "https://127.0.0.1:8006",
            "PROXMOX_VE_API_TOKEN": "root@pam!token=secret",
            "PVE_SSH_WAIT_RETRIES": "2",
            "PVE_SSH_WAIT_SLEEP": "0",
            "CURL_LOG": str(self.curl_log),
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

    def test_ssh_probe_success_exits_0(self):
        """Zielony test kontraktu: udana sonda portu SSH kończy skrypt kodem 0 bez oczekiwania na timeout."""
        success_env = dict(self.env)
        success_env["MOCK_SSH_PROBE_SUCCESS"] = "1"
        proc = subprocess.run(
            [
                str(SCRIPT),
                "--vmid", "10020",
                "--name", "c12db1",
                "--ip", "192.168.1.40",
                "--cluster", "test-cluster",
                "--key-file", str(self.key_file),
            ],
            capture_output=True,
            text=True,
            env=success_env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"Udana sonda SSH musi kończyć się exit 0: {proc.stderr}")
        self.assertIn("Sukces: c12db1 (10020, 192.168.1.40) odpowiada na porcie SSH 22", proc.stdout)

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

    def test_pve_token_not_leaked_in_argv_and_passed_via_file(self):
        """Bezpieczeństwo PVE: token API NIE może pojawić się w argv curl (przekazywany przez plik -H @...)."""
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
        self.assertEqual(proc.returncode, 0, f"Skrypt powinien zakończyć się kodem 0: {proc.stderr}")
        curl_log_content = self.curl_log.read_text(encoding="utf-8") if self.curl_log.is_file() else ""
        self.assertIn("curl-called", curl_log_content)
        self.assertNotIn("TOKEN_LEAK_IN_ARGV", curl_log_content, "Token PVE wyciekł do argumentów linii poleceń curl!")
    def test_invalid_wait_env_variables_fail_closed_before_pve_calls(self):
        """Niepoprawne zmienne środowiskowe PVE_SSH_WAIT_* odrzucane są kodem 2 przed wywołaniami PVE (fail-closed)."""
        bad_cases = [
            ({"PVE_SSH_WAIT_RETRIES": "-5"}, "BŁĄD: PVE_SSH_WAIT_RETRIES musi być dodatnią liczbą"),
            ({"PVE_SSH_WAIT_RETRIES": "0"}, "BŁĄD: PVE_SSH_WAIT_RETRIES musi być dodatnią liczbą"),
            ({"PVE_SSH_WAIT_RETRIES": "foo"}, "BŁĄD: PVE_SSH_WAIT_RETRIES musi być dodatnią liczbą"),
            ({"PVE_SSH_WAIT_SLEEP": "-1"}, "BŁĄD: PVE_SSH_WAIT_SLEEP musi być nieujemną liczbą"),
            ({"PVE_SSH_WAIT_SLEEP": "bar"}, "BŁĄD: PVE_SSH_WAIT_SLEEP musi być nieujemną liczbą"),
        ]
        for extra_env, expected_msg in bad_cases:
            with self.subTest(extra_env=extra_env):
                bad_env = dict(self.env)
                bad_env.update(extra_env)
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
                    env=bad_env,
                    timeout=30,
                )
                self.assertEqual(proc.returncode, 2, f"Oczekiwano exit 2 dla {extra_env}, otrzymano: {proc.returncode}")
                self.assertIn(expected_msg, proc.stderr)
                # Weryfikacja: ani jedno zapytanie curl do PVE nie zostało wykonane
                self.assertFalse(
                    self.curl_log.is_file() and len(self.curl_log.read_text(encoding="utf-8").strip()) > 0,
                    f"Błąd walidacji env wykonał zapytanie do PVE: {self.curl_log.read_text(encoding='utf-8') if self.curl_log.is_file() else ''}",
                )

    def test_static_script_contract_has_fail_closed_exit(self):
        """Weryfikacja kodu skryptu: gałąź timeoutu musi kończyć się exit 1."""
        body = SCRIPT.read_text(encoding="utf-8")
        timeout_branch = body[body.find("echo \"BŁĄD: Maszyna wystartowała") :]
        self.assertIn("exit 1", timeout_branch)
        self.assertNotIn("exit 0", timeout_branch[: timeout_branch.find("else")])


if __name__ == "__main__":
    unittest.main()
