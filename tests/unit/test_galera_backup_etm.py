"""F2 — Encrypt-then-MAC: HMAC-SHA256 nad ciphertext backupu.

Pokrycie:
- KAT (known-answer test): PBKDF2 w Pythonie == PBKDF2 w openssl (ten sam
  klucz = ten sam wektor, wiec MAC mozna liczyc bez sekretow w argv).
- Round-trip compute/verify; tamper 1 bajt; obcy klucz; zly salt/tag.
- Integracja run_restore:
  * backup legacy (bez pola hmac_sha256) przechodzi gate MAC bez zmian;
  * backup v2 (z tagiem): nietamperowany = MAC OK (padajac dalej, przy
    deszyfrowaniu pliku testowego), tamperowany = E_INTEGRITY PRZED
    deszyfrowaniem.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "roles" / "galera_backup" / "files"))

from galera_backup import pipeline  # noqa: E402
from galera_backup.crypto import (  # noqa: E402
    ETM_PBKDF2_ITERATIONS,
    compute_etm_tag,
    derive_etm_key,
    verify_etm,
)

PASS = "etm-test-passphrase-2026"
# Salt ASCII (16 znakow): OpenSSL `-kdfopt salt:<str>` traktuje go doslownie
# jako bajty ASCII, wiec strona Pythona KAT musi uzywac tych samych bajtow
# (`SALT.encode()`), nie hex-dekodowania. Wlasnie to test porownuje.
SALT = b"0123456789abcdef"


def _openssl_pbkdf2(passphrase: str, salt: bytes, iterations: int, keylen: int = 32) -> bytes:
    out = subprocess.run(
        [
            "openssl", "kdf", "-keylen", str(keylen),
            "-kdfopt", "digest:SHA256",
            "-kdfopt", f"iter:{iterations}",
            "-kdfopt", f"salt:{salt.decode('ascii')}",
            "-kdfopt", f"pass:{passphrase}",
            "PBKDF2",
        ],
        stdout=subprocess.PIPE,
        check=True,
    ).stdout
    # openssl drukuje hex w liniach 16 bajtow z dwukropek
    return bytes.fromhex(out.decode().replace(":", "").replace("\n", ""))


class EtmKdfTests(unittest.TestCase):
    def test_kat_matches_openssl_kdf(self):
        """Wektor KAT: PBKDF2-SHA256 Python == OpenSSL (te same bajty wejscia)."""
        self.assertEqual(
            derive_etm_key(PASS, SALT).hex(),
            _openssl_pbkdf2(PASS, SALT, ETM_PBKDF2_ITERATIONS).hex(),
        )


class EtmComputeVerifyTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.payload = self.td / "backup.tar.enc"
        self.payload.write_bytes(b"\x00\x01\x02ciphertext-block" * 100)

    def tearDown(self):
        self._td.cleanup()

    def test_roundtrip_verify_ok(self):
        tag = compute_etm_tag(PASS, SALT, self.payload)
        verify_etm(PASS, SALT.hex(), tag, self.payload)  # nie rzuca

    def test_tamper_single_byte_detected(self):
        tag = compute_etm_tag(PASS, SALT, self.payload)
        data = bytearray(self.payload.read_bytes())
        data[len(data) // 2] ^= 0xFF
        self.payload.write_bytes(bytes(data))
        with self.assertRaises(ValueError, msg="HMAC-SHA256 mismatch"):
            verify_etm(PASS, SALT.hex(), tag, self.payload)

    def test_wrong_key_rejected(self):
        tag = compute_etm_tag(PASS, SALT, self.payload)
        with self.assertRaises(ValueError):
            verify_etm("obcy-klucz", SALT.hex(), tag, self.payload)

    def test_salt_change_rejected(self):
        """Ten sam ciphertext z innym soleniem = inna odpowiedz MAC."""
        tag = compute_etm_tag(PASS, SALT, self.payload)
        other_salt = bytes.fromhex("ffeeddccbbaa99887766554433221100")
        with self.assertRaises(ValueError):
            verify_etm(PASS, other_salt.hex(), tag, self.payload)

    def test_bad_salt_format_rejected(self):
        tag = compute_etm_tag(PASS, SALT, self.payload)
        for bad_salt in ("zzzz", SALT.hex()[:30], SALT.hex() + "00"):
            with self.assertRaises(ValueError, msg=f"salt={bad_salt!r}"):
                verify_etm(PASS, bad_salt, tag, self.payload)

    def test_bad_tag_format_rejected(self):
        for bad_tag in ("", "abc", "x" * 64):
            with self.assertRaises(ValueError, msg=f"tag={bad_tag!r}"):
                verify_etm(PASS, SALT.hex(), bad_tag, self.payload)


def _restore_env(td: Path):
    """Szkielet harnessa restore (patrz test_galera_backup_restore.py)."""
    cluster_dir = td / "clusters" / "claude-r10b"
    cfg = {
        "format_version": 1,
        "cluster_name": "claude-r10b",
        "metric_cluster_label": "r10b-galera",
        "local_role": "restore",
        "scheduler_system_hostname": "gnode4",
        "galera_nodes_expected": 3,
        "mariadb_version": "11.4",
        "retention_days": 14,
        "flow_control_threshold_ns": 1000000000,
        "backend": {"type": "s3", "endpoint": "192.168.1.47:9000", "bucket": "b", "secure": False},
        "paths": {
            "install_root": str(td),
            "cluster_dir": str(cluster_dir),
            "staging_root": str(td / "staging"),
            "datadir": str(td / "datadir"),
            "socket": str(td / "mysql.sock"),
            "metric_file": str(td / "metrics.prom"),
        },
    }
    cfg_path = td / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    env_path = td / "secrets.env"
    env_path.write_text(
        f'GALERA_BACKUP_ENCRYPTION_KEY="{PASS}"\n'
        'GALERA_BACKUP_S3_ACCESS_KEY="s3_access_fixture"\n'
        'GALERA_BACKUP_S3_SECRET_KEY="s3_secret_fixture"\n',
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)
    return cfg_path, env_path, cluster_dir


class EtmRestoreIntegrationTests(unittest.TestCase):
    def _run_restore_with_payload(self, payload: Path, meta: dict):
        # metadata.json i checksum sa czescia kontraktu ArtifactSet — helper
        # je zapisuje przy payloadzie, bo to `fetch_latest` musialby dostarczyc.
        (payload.parent / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        (payload.parent / "backup.sha256").write_text("sha  backup.tar.enc\n", encoding="utf-8")
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg_path, env_path, cluster_dir = _restore_env(td_path)
            backend = MagicMock()
            backend.preflight.return_value = None
            backend.fetch_latest.return_value = pipeline.ArtifactSet(
                backup_name="galera-claude-r10b-20260831-000000",
                payload_path=payload,
                checksum_path=payload.parent / "backup.sha256",
                metadata_path=payload.parent / "metadata.json",
            )
            with patch("socket.gethostname", return_value="rnode1"):
                with patch.object(pipeline, "get_storage_backend", return_value=backend):
                    with self.assertRaises(pipeline.BackupError) as ctx:
                        pipeline.run_restore(
                            config_path=cfg_path,
                            secrets_path=env_path,
                            cluster_name="claude-r10b",
                            confirm=True,
                        )
            return ctx.exception

    def test_legacy_backup_without_hmac_passes_mac_gate(self):
        """Backup sprzed F2 (brak pola hmac_sha256) nie jest blokowany przez gate."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            payload = td_path / "backup.tar.enc"
            payload.write_bytes(b"legacy-encrypted-bytes")
            meta = {
                "format_version": 1,
                "cluster_name": "claude-r10b",
                "mariadb_version": "11.4",
                "encrypted_sha256": hashlib.sha256(b"legacy-encrypted-bytes").hexdigest(),
                "encrypted_size_bytes": len(b"legacy-encrypted-bytes"),
                "plaintext_sha256": "x" * 64,
            }
            (td_path / "backup.sha256").write_text("sha  backup.tar.enc\n")
            (td_path / "metadata.json").write_text(json.dumps(meta))
            exc = self._run_restore_with_payload(payload, meta)
            self.assertEqual(exc.code, "E_INTEGRITY")
            # Padajac przy DESZYFROWANIU (plik testowy nie jest CBC), nie przy MAC.
            self.assertIn("Decryption failed", str(exc))

    def test_tampered_etm_backup_rejected_before_decrypt(self):
        """Model ataku F2: atakujacy ma prawo zapisu do backupu i metadata,
        wiec tamperuje ciphertext i PRZELICZA niezakluczony sha256_encrypted
        (co oszukuje stara kontrole). Zakluczonego HMACu sfaleszowac NIE MOZE
        (nie ma klucz) — wiec gate MAC musi go zlapac PRZED deszyfrowaniem."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            payload = td_path / "backup.tar.enc"
            original = b"v2-encrypted-bytes-0123456789"
            payload.write_bytes(original)
            tag = compute_etm_tag(PASS, SALT, payload)
            # Tamper: jeden bajt po obliczeniu tagu.
            tampered = bytearray(original)
            tampered[3] ^= 0x01
            payload.write_bytes(bytes(tampered))
            meta = {
                "format_version": 1,
                "cluster_name": "claude-r10b",
                "mariadb_version": "11.4",
                # Atakujacy przelicza sha256 na nowym ciphertext (oszukiwa starą
                # kontrole spelnialnosci — to jest wlasnie luka F2).
                "encrypted_sha256": hashlib.sha256(bytes(tampered)).hexdigest(),
                "encrypted_size_bytes": len(tampered),
                "plaintext_sha256": "x" * 64,
                # Tag pozostaje oryginalny — sfalszowania wymaga klucz.
                "hmac_sha256": tag,
                "hmac_salt": SALT.hex(),
            }
            exc = self._run_restore_with_payload(payload, meta)
            self.assertEqual(exc.code, "E_INTEGRITY")
            self.assertIn("HMAC-SHA256 verification failed", str(exc))


if __name__ == "__main__":
    unittest.main()
