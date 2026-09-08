import os
import subprocess
import tempfile
import unittest
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROBE = WORKSPACE_ROOT / "tests" / "validation" / "probe-no-secrets-leak.sh"


class SecretProbeTests(unittest.TestCase):
    def test_probe_rejects_representative_secret_assignments(self):
        fixture = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                prefix=".secret-probe-",
                suffix=".yml",
                dir=WORKSPACE_ROOT / "tests" / "unit",
                delete=False,
                encoding="utf-8",
            ) as handle:
                fixture = Path(handle.name)
                handle.write('password: "my_password_1"\n')
                handle.write('api_key: "AKIA5EXAMPLEKEYX9"\n')
                handle.write('secret: "s3cr3tvalue"\n')

            result = subprocess.run(
                ["bash", str(PROBE)],
                cwd=WORKSPACE_ROOT,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "SECRET_PROBE_EXTRA_PATHS": str(fixture)},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"{fixture.relative_to(WORKSPACE_ROOT)}:1", result.stdout)
            self.assertIn(f"{fixture.relative_to(WORKSPACE_ROOT)}:2", result.stdout)
            self.assertIn(f"{fixture.relative_to(WORKSPACE_ROOT)}:3", result.stdout)
            # Bramka orzekajaca "sekret nie ma prawa wyciec" nie moze go sama
            # wypisac: ta sonda biegnie w CI, a log CI jest trwalym artefaktem.
            # Wczesniejszy kontrakt wymagal DOKLADNIE odwrotnie — wartosci w
            # stdout — wiec kazdy przebieg z prawdziwym znaleziskiem publikowal
            # sekret. Operator ma plik i numer linii.
            combined_output = result.stdout + result.stderr
            self.assertNotIn("my_password_1", combined_output)
            self.assertNotIn("AKIA5EXAMPLEKEYX9", combined_output)
            self.assertNotIn("s3cr3tvalue", combined_output)
        finally:
            if fixture is not None:
                fixture.unlink(missing_ok=True)

    def test_probe_ignores_empty_assignments(self):
        # Pusta wartosc nie moze byc sekretem. Zarchiwizowane stany Terraform
        # (docs/records/archives/**/terraform.tfstate) niosa 54 klucze
        # "password": "" i bez tego wyjatku kazdy przebieg CI konczy sie
        # czerwona bramka na udowodnionym falszywym alarmie.
        fixture = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                prefix=".secret-probe-",
                suffix=".json",
                dir=WORKSPACE_ROOT / "tests" / "unit",
                delete=False,
                encoding="utf-8",
            ) as handle:
                fixture = Path(handle.name)
                handle.write('{"password": "", "token": "", "api_key": ""}\n')

            result = subprocess.run(
                ["bash", str(PROBE)],
                cwd=WORKSPACE_ROOT,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "SECRET_PROBE_EXTRA_PATHS": str(fixture)},
            )

            self.assertNotIn(
                str(fixture.relative_to(WORKSPACE_ROOT)),
                result.stdout,
                "pusta wartosc zgloszona jako sekret",
            )
        finally:
            if fixture is not None:
                fixture.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
