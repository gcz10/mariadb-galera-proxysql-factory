"""Testy kontraktu skryptu tools/pve-create-vm.sh.

Weryfikacja składni bash, obsługi flag --help, walidacji wymaganych argumentów,
walidacji formatu IP oraz obecności zmiennych środowiskowych PVE.

AUDIT 2026-09-21 (przenośność): skrypt jest generyczny, więc KAŻDE ustawienie
zależne od infrastruktury (adresacja, brama, pula, węzeł, storage, obraz, most,
DNS) jest wymagane jawnie. Testy pinują, że brak któregokolwiek z nich kończy
pracę kodem 2, a skrót "ostatni oktet" (zgadywanie sieci labu) nie istnieje.

Adresy w testach są dokumentacyjne (RFC 5737 / RFC 3849), żeby plik sam nie
wprowadzał adresacji żadnej konkretnej instalacji.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "pve-create-vm.sh"

# Domyślny, kompletny zestaw jawnych ustawień infrastruktury.
DEFAULT_INFRA = {
    "ip": "192.0.2.10/24",
    "gateway": "192.0.2.1",
    "cluster": "example-c1",
    "pool": "example-pool",
    "bridge": "vmbr0",
    "node": "node1",
    "storage": "local-zfs",
    "image": "local:import/rocky.qcow2",
    "nameserver": "203.0.113.53",
}


def script_args(**overrides):
    """Argumenty wywołania z pełną jawną konfiguracją infrastruktury.

    Klucz `None` w overrides pomija daną flagę — tak testujemy braki.
    """
    infra = dict(DEFAULT_INFRA)
    infra.update(overrides)
    args = ["--vmid", "10020", "--name", "c12db1"]
    for flag, value in infra.items():
        if value is not None:
            args += [f"--{flag}", value]
    return args


class PveCreateVmScriptContractTests(unittest.TestCase):
    def run_script(self, args=None, env=None):
        # Domyślnie środowisko BEZ zmiennych PVE — wynik nie może zależeć od
        # tego, co wywołujący ma w swoim shellu (np. gotowego PROXMOX_VE_ENDPOINT
        # albo FLEET_POOL), inaczej test przechodzi tylko u autora.
        if env is None:
            env = {"PATH": os.environ.get("PATH", "/bin:/usr/bin")}
        return subprocess.run(
            [str(SCRIPT)] + (script_args() if args is None else args),
            capture_output=True,
            text=True,
            env=env,
        )

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

    def test_help_documents_infrastructure_dependent_options(self):
        """Ustawienia zależne od infrastruktury muszą być widoczne w --help.

        Po usunięciu wartości domyślnych operator nie ma skąd ich zgadnąć,
        więc brak flagi w pomocy jest defektem interfejsu, nie kosmetyką.
        """
        proc = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        for flag in ["--pool", "--bridge", "--node", "--storage", "--image", "--nameserver", "--prefix"]:
            with self.subTest(flag=flag):
                self.assertIn(flag, proc.stdout)

    def test_missing_required_parameters_fail_fast(self):
        cases = [
            ([], "BŁĄD: --vmid jest wymagany"),
            (["--vmid", "10020"], "BŁĄD: --name jest wymagany"),
            (["--vmid", "10020", "--name", "c12db1"], "BŁĄD: --ip jest wymagany"),
            (["--vmid", "10020", "--name", "c12db1", "--ip", "192.0.2.10/24"],
             "BŁĄD: --cluster jest wymagany"),
            (["--vmid", "10020", "--name", "c12db1", "--ip", "192.0.2.10/24", "--cluster", "example-c1"],
             "BŁĄD: --pool jest wymagany"),
            (["--vmid", "10020", "--name", "c12db1", "--ip", "192.0.2.10/24", "--cluster", "example-c1",
              "--pool", "example-pool"], "BŁĄD: --gateway jest wymagany"),
            (["--vmid", "10020", "--name", "c12db1", "--ip", "192.0.2.10/24", "--cluster", "example-c1",
              "--pool", "example-pool", "--gateway", "192.0.2.1"], "BŁĄD: --bridge jest wymagany"),
            (["--vmid", "10020", "--name", "c12db1", "--ip", "192.0.2.10/24", "--cluster", "example-c1",
              "--pool", "example-pool", "--gateway", "192.0.2.1", "--bridge", "vmbr0"],
             "BŁĄD: --node jest wymagany"),
            (["--vmid", "10020", "--name", "c12db1", "--ip", "192.0.2.10/24", "--cluster", "example-c1",
              "--pool", "example-pool", "--gateway", "192.0.2.1", "--bridge", "vmbr0", "--node", "node1"],
             "BŁĄD: --storage jest wymagany"),
            (["--vmid", "10020", "--name", "c12db1", "--ip", "192.0.2.10/24", "--cluster", "example-c1",
              "--pool", "example-pool", "--gateway", "192.0.2.1", "--bridge", "vmbr0", "--node", "node1",
              "--storage", "local-zfs"], "BŁĄD: --image jest wymagany"),
            (["--vmid", "10020", "--name", "c12db1", "--ip", "192.0.2.10/24", "--cluster", "example-c1",
              "--pool", "example-pool", "--gateway", "192.0.2.1", "--bridge", "vmbr0", "--node", "node1",
              "--storage", "local-zfs", "--image", "local:import/rocky.qcow2"],
             "BŁĄD: --nameserver jest wymagany"),
        ]
        for args, expected_err in cases:
            with self.subTest(args=args):
                proc = self.run_script(args=args)
                self.assertEqual(proc.returncode, 2)
                self.assertIn(expected_err, proc.stderr)

    def test_invalid_ip_format_rejected(self):
        """Odrzucamy wszystko, co nie jest pełnym IPv4 — w tym skrót ostatniego oktetu.

        Skrót (`--ip 40`) rozwijał się do adresu sieci labu; po usunięciu tej
        wiedzy z narzędzia musi być twardym błędem, nie domysłem.
        """
        for bad_ip in ["abc", "192.0.2", "192.0.2.999.1", "foo.bar", "40", "10"]:
            with self.subTest(bad_ip=bad_ip):
                proc = self.run_script(args=script_args(ip=bad_ip))
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("BŁĄD: Niepoprawny format IP", proc.stderr)

    def test_ip_octet_out_of_range_rejected(self):
        """Poprawnie zbudowany IPv4 z oktetem > 255 jest odrzucany."""
        proc = self.run_script(args=script_args(ip="192.0.2.300/24"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("BŁĄD: oktet poza zakresem 0-255", proc.stderr)

    def test_ip_without_network_prefix_rejected(self):
        """Pełny IPv4 bez prefiksu i bez --prefix to brak konfiguracji sieci."""
        proc = self.run_script(args=script_args(ip="192.0.2.10"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("BŁĄD: brak prefiksu sieci", proc.stderr)

    def test_prefix_out_of_range_rejected(self):
        proc = self.run_script(args=script_args(ip="192.0.2.10/33"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("BŁĄD: prefiks poza zakresem 1-32", proc.stderr)

    def test_prefix_flag_accepted_as_alternative_to_cidr(self):
        """--prefix jest równorzędnym źródłem prefiksu, gdy --ip jest samym adresem.

        Bez tego wariantu pełny IPv4 bez CIDR byłby bezużyteczny: prefiks musi
        dać się podać osobno.
        """
        args = script_args(ip="198.51.100.7") + ["--prefix", "20"]
        proc = self.run_script(args=args)
        # Przechodzi walidację sieci i zatrzymuje się dopiero na braku poświadczeń
        # PVE — czyli prefiks został przyjęty, a nie odrzucony jako brak konfiguracji.
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("PROXMOX_VE_ENDPOINT", proc.stderr)
        self.assertNotIn("brak prefiksu sieci", proc.stderr)

    def test_conflicting_prefix_sources_rejected(self):
        """CIDR i --prefix podane razem i sprzecznie to błąd, nie cichy wybór jednego."""
        args = script_args(ip="192.0.2.10/24") + ["--prefix", "16"]
        proc = self.run_script(args=args)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("BŁĄD: prefiks podany dwa razy", proc.stderr)

    def test_unknown_parameter_rejected(self):
        proc = self.run_script(args=["--unknown-flag"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Nieznany parametr", proc.stderr)

    def test_missing_environment_fails_closed(self):
        env = {
            "PATH": os.environ.get("PATH", "/bin:/usr/bin"),
        }
        # Brak PROXMOX_VE_ENDPOINT i PROXMOX_VE_API_TOKEN
        proc = self.run_script(env=env)
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
            "    */qemu|*/content)\n"
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

    def run_script(self, ip="192.0.2.254/24", env=None, extra_args=None):
        args = script_args(ip=ip) + ["--key-file", str(self.key_file)] + (extra_args or [])
        return subprocess.run(
            [str(SCRIPT)] + args,
            capture_output=True, text=True, env=env or self.env, timeout=30,
        )

    def test_ssh_timeout_fails_closed_with_exit_1(self):
        """Czerwony test kontraktu: brak portu 22 w budżecie czasu MUSI kończyć się kodem 1 (fail-closed)."""
        proc = self.run_script()
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
        proc = self.run_script(ip="192.0.2.40/24", env=success_env)
        self.assertEqual(proc.returncode, 0, f"Udana sonda SSH musi kończyć się exit 0: {proc.stderr}")
        self.assertIn("Sukces: c12db1 (10020, 192.0.2.40) odpowiada na porcie SSH 22", proc.stdout)

    def test_ssh_probe_targets_bare_address_without_prefix(self):
        """Sonda SSH idzie na adres, nie na CIDR — prefiks nie ma prawa trafić do /dev/tcp."""
        success_env = dict(self.env)
        success_env["MOCK_SSH_PROBE_SUCCESS"] = "1"
        proc = self.run_script(ip="198.51.100.7/20", env=success_env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Oczekiwanie na podniesienie SSH (198.51.100.7:22)", proc.stdout)
        after_header = proc.stdout.split("Oczekiwanie na podniesienie SSH")[1]
        self.assertNotIn("/20", after_header, f"prefiks wyciekł do sondy SSH: {after_header}")

    def test_no_wait_ssh_flag_succeeds_with_exit_0(self):
        """Zielony test kontraktu: jawna flaga --no-wait-ssh pomija pętlę i kończy się kodem 0."""
        proc = self.run_script(extra_args=["--no-wait-ssh"])
        self.assertEqual(
            proc.returncode,
            0,
            f"--no-wait-ssh musi zwracać exit 0, otrzymano: {proc.returncode}. Stderr:\n{proc.stderr}",
        )
        self.assertIn("Pominięto oczekiwanie na SSH (--no-wait-ssh)", proc.stdout)

    def test_pve_token_not_leaked_in_argv_and_passed_via_file(self):
        """Bezpieczeństwo PVE: token API NIE może pojawić się w argv curl (przekazywany przez plik -H @...)."""
        proc = self.run_script(extra_args=["--no-wait-ssh"])
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
                proc = self.run_script(env=bad_env)
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
