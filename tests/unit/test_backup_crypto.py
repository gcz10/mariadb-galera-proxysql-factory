"""Kontrakt szyfrowania kopii (tests/unit/test_backup_crypto.py — P1-B).

AES-256-GCM (format_version 3) szyfruje strumieniowo i uwierzytelnia caly
artefakt. Czytniki formatow 2 (GCM one-shot) i 1 (CBC przez `openssl enc`)
pozostaja dostepne, aby istniejace kopie nadal dalo sie odtworzyc.
"""

import filecmp
import shutil
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKSPACE_ROOT / "roles" / "galera_backup" / "files"))

from galera_backup import crypto  # noqa: E402
from galera_backup.errors import BackupError  # noqa: E402
from galera_backup.runner import CommandRunner  # noqa: E402

KEY_MATERIAL = "test-encryption-key-f2"
PLAINTEXT = b"galera-backup-payload-" + b"x" * 4096


def write_v2_payload(payload: Path, plaintext: bytes, key_material: str) -> None:
    """Zbuduj niezalezny fixture formatu GB2G zapisywanego przed P1-B."""
    salt = b"\x11" * 16
    nonce = b"\x22" * 12
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=200_000,
    )
    key = kdf.derive(key_material.encode("utf-8"))
    token = AESGCM(key).encrypt(nonce, plaintext, None)
    payload.write_bytes(b"GB2G" + salt + nonce + token)


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
            self.assertTrue(blob.startswith(crypto.MAGIC_V3), "payload v3 musi miec naglowek GB3G")

            out = td_path / "restored.tar"
            fmt = crypto.decrypt_payload(payload, out, KEY_MATERIAL, runner)
            self.assertEqual(fmt, "v3")
            self.assertEqual(out.read_bytes(), PLAINTEXT)

    def test_tampered_payload_fails_authentication(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            tar = td_path / "backup.tar"
            payload = td_path / "backup.tar.enc"
            out = td_path / "restored.tar"
            out.write_bytes(b"existing-safe-output")
            tar.write_bytes(PLAINTEXT)
            runner = CommandRunner(secret_values=[KEY_MATERIAL])
            crypto.encrypt_payload(tar, payload, KEY_MATERIAL)

            blob = bytearray(payload.read_bytes())
            blob[-1] ^= 0xFF  # podmiana ostatniego bajtu (tag)
            payload.write_bytes(bytes(blob))

            with self.assertRaises(BackupError) as ctx:
                crypto.decrypt_payload(payload, out, KEY_MATERIAL, runner)
            self.assertEqual(ctx.exception.code, "E_INTEGRITY")
            self.assertEqual(out.read_bytes(), b"existing-safe-output")

    def test_v3_header_cannot_be_downgraded_to_v2(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            tar = td_path / "backup.tar"
            payload = td_path / "backup.tar.enc"
            out = td_path / "restored.tar"
            tar.write_bytes(PLAINTEXT)
            crypto.encrypt_payload(tar, payload, KEY_MATERIAL)

            with payload.open("r+b") as target:
                target.write(crypto.MAGIC_V2)

            with self.assertRaises(BackupError) as ctx:
                crypto.decrypt_payload(payload, out, KEY_MATERIAL, None)
            self.assertEqual(ctx.exception.code, "E_INTEGRITY")
            self.assertFalse(out.exists())

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

    def test_large_payload_roundtrip_uses_bounded_python_memory(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            tar = td_path / "large.tar"
            payload = td_path / "large.tar.enc"
            out = td_path / "restored.tar"
            block = b"x" * (1024 * 1024)
            with tar.open("wb") as target:
                for _ in range(24):
                    target.write(block)
            source_size = tar.stat().st_size

            tracemalloc.start()
            try:
                crypto.encrypt_payload(tar, payload, KEY_MATERIAL)
                encrypt_peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()

            tracemalloc.start()
            try:
                fmt = crypto.decrypt_payload(payload, out, KEY_MATERIAL, None)
                decrypt_peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()

            self.assertEqual(fmt, "v3")
            self.assertTrue(filecmp.cmp(tar, out, shallow=False))
            self.assertLess(encrypt_peak, source_size // 2)
            self.assertLess(decrypt_peak, source_size // 2)


class GcmV2CompatibilityTests(unittest.TestCase):
    def test_existing_v2_payload_remains_decryptable(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            payload = td_path / "backup-v2.tar.enc"
            out = td_path / "restored.tar"
            write_v2_payload(payload, PLAINTEXT, KEY_MATERIAL)

            fmt = crypto.decrypt_payload(payload, out, KEY_MATERIAL, None)

            self.assertEqual(fmt, "v2")
            self.assertEqual(out.read_bytes(), PLAINTEXT)


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
            self.assertFalse(
                payload.read_bytes().startswith((crypto.MAGIC_V3, crypto.MAGIC_V2)),
                "legacy payload nie moze miec naglowka AES-GCM",
            )

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
            self.assertFalse(out.exists(), "bledny klucz legacy nie moze opublikowac plaintextu")


if __name__ == "__main__":
    unittest.main()
