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
            self.assertIn("my_password_1", result.stdout)
            self.assertIn("AKIA5EXAMPLEKEYX9", result.stdout)
            self.assertIn("s3cr3tvalue", result.stdout)
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
