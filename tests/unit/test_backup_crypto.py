"""Kontrakt szyfrowania kopii (tests/unit/test_backup_crypto.py — F2).

AES-256-GCM (format_version 2) musi dostarczac integralnosc: podmiana
ciphertextu badz zle haslo konczy sie E_INTEGRITY zanim cokolwiek trafi na
dysk. Sciezka legacy (format_version 1, CBC przez `openssl enc`) zostaje
utrzymana celowo — istniejace kopie sprzed migracji musza sie dac odtworzyc.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE_ROOT / "roles" / "galera_backup" / "files"))

from galera_backup import crypto  # noqa: E402
from galera_backup.errors import BackupError  # noqa: E402
from galera_backup.runner import CommandRunner  # noqa: E402

KEY_MATERIAL = "test-encryption-key-f2"
PLAINTEXT = b"galera-backup-payload-" + b"x" * 4096


class GcmRoundTripTests(unittest.TestCase):
    def test_roundtrip_produces_magic_and_recovers_plaintext(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            tar = td_path / "backup.tar"
            payload = td_path / "backup.tar.enc"
            tar.write_bytes(PLAINTEXT)
            runner = CommandRunner(secret_values=[KEY_MATERIAL])

            crypto.encrypt_payload(tar, payload, KEY_MATERIAL)
            blob = payload.read_bytes()
            self.assertTrue(blob.startswith(crypto.MAGIC), "payload v2 musi miec naglowek GB2G")

            out = td_path / "restored.tar"
            fmt = crypto.decrypt_payload(payload, out, KEY_MATERIAL, runner)
            self.assertEqual(fmt, "v2")
            self.assertEqual(out.read_bytes(), PLAINTEXT)

    def test_tampered_payload_fails_authentication(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            tar = td_path / "backup.tar"
            payload = td_path / "backup.tar.enc"
            out = td_path / "restored.tar"
            tar.write_bytes(PLAINTEXT)
            runner = CommandRunner(secret_values=[KEY_MATERIAL])
            crypto.encrypt_payload(tar, payload, KEY_MATERIAL)

            blob = bytearray(payload.read_bytes())
            blob[-1] ^= 0xFF  # podmiana ostatniego bajtu (tag)
            payload.write_bytes(bytes(blob))

            with self.assertRaises(BackupError) as ctx:
                crypto.decrypt_payload(payload, out, KEY_MATERIAL, runner)
            self.assertEqual(ctx.exception.code, "E_INTEGRITY")
            self.assertFalse(out.exists(), "odszyfrowany tar nie moze powstac z przeklamaniem")

    def test_wrong_password_fails_authentication(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            tar = td_path / "backup.tar"
            payload = td_path / "backup.tar.enc"
            out = td_path / "restored.tar"
            tar.write_bytes(PLAINTEXT)
            crypto.encrypt_payload(tar, payload, KEY_MATERIAL)

            with self.assertRaises(BackupError) as ctx:
                crypto.decrypt_payload(payload, out, "other-key", None)
            self.assertEqual(ctx.exception.code, "E_INTEGRITY")

    def test_salt_and_nonce_are_random_per_encryption(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            tar = td_path / "backup.tar"
            tar.write_bytes(PLAINTEXT)
            p1 = td_path / "one.enc"
            p2 = td_path / "two.enc"
            crypto.encrypt_payload(tar, p1, KEY_MATERIAL)
            crypto.encrypt_payload(tar, p2, KEY_MATERIAL)
            self.assertNotEqual(p1.read_bytes(), p2.read_bytes(), "ten sam plaintext nie moze dawac identycznego ciphertextu")


@unittest.skipUnless(shutil.which("openssl"), "openssl niedostepny")
def openssl_supports_pbkdf2() -> bool:
    """Sondowanie srodowiska testowego: lokalny openssl umie -pbkdf2."""
    probe = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "1", "-e",
         "-in", "/dev/null", "-out", "/dev/null", "-pass", "pass:x"],
        capture_output=True,
    )
    return probe.returncode == 0


@unittest.skipUnless(shutil.which("openssl"), "openssl niedostepny")
@unittest.skipUnless(openssl_supports_pbkdf2(), "openssl bez -pbkdf2 (legacy nie testowalny)")
class LegacyCbcDecryptTests(unittest.TestCase):
    def test_legacy_v1_payload_roundtrip_through_openssl(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            tar = td_path / "backup.tar"
            payload = td_path / "backup.tar.enc"
            out = td_path / "restored.tar"
            tar.write_bytes(PLAINTEXT)

            enc = subprocess.run(
                ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                 "-md", "sha256", "-salt",
                 "-in", str(tar), "-out", str(payload),
                 "-pass", "env:GALERA_BACKUP_ENCRYPTION_KEY"],
                env={"GALERA_BACKUP_ENCRYPTION_KEY": KEY_MATERIAL},
                capture_output=True,
            )
            self.assertEqual(enc.returncode, 0, enc.stderr)
            self.assertFalse(payload.read_bytes().startswith(crypto.MAGIC), "legacy payload nie moze miec naglowka GB2G")

            runner = CommandRunner(secret_values=[KEY_MATERIAL])
            fmt = crypto.decrypt_payload(payload, out, KEY_MATERIAL, runner)
            self.assertEqual(fmt, "v1")
            self.assertEqual(out.read_bytes(), PLAINTEXT)

    def test_legacy_wrong_password_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            tar = td_path / "backup.tar"
            payload = td_path / "backup.tar.enc"
            out = td_path / "restored.tar"
            tar.write_bytes(PLAINTEXT)
            subprocess.run(
                ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                 "-md", "sha256", "-salt",
                 "-in", str(tar), "-out", str(payload),
                 "-pass", "env:GALERA_BACKUP_ENCRYPTION_KEY"],
                env={"GALERA_BACKUP_ENCRYPTION_KEY": KEY_MATERIAL},
                capture_output=True,
                check=True,
            )

            runner = CommandRunner(secret_values=[KEY_MATERIAL])
            with self.assertRaises(BackupError) as ctx:
                crypto.decrypt_payload(payload, out, "other-key", runner)
            self.assertEqual(ctx.exception.code, "E_INTEGRITY")


if __name__ == "__main__":
    unittest.main()
