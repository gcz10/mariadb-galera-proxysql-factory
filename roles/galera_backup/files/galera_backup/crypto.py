"""Szyfrowanie kopii: AES-256-GCM (format_version 2) + odczyt legacy CBC.

DLACZEGO GCM I DLACZEGO NIE `openssl enc`. Tryb CBC nie daje integralnosci:
podmiana ciphertextu w wrogim magazynie S3 wykrywalna jest wylacznie przez
sha256 zapisany ... w tym samym magazynie — atakujacy przelicza checksum.
AES-GCM wnosi uwierzytelnienie (AEAD): odszyfrowanie z podmienionym bitem
konczy sie wyjatkiem InvalidTag, zanim cokolwiek trafi na dysk.

`openssl enc` NIE wspiera szyfrow AEAD (zmierzone na flacie, OpenSSL 3.5.5:
"enc: AEAD ciphers not supported"), wiec szyfrowanie idzie przez
python3-cryptography (RPM AppStream, pin w lockfile: backup_tools.crypto_package).
Klucz NIE zmienia sie wzgledem formatu 1: to dalej GALERA_BACKUP_ENCRYPTION_KEY,
dtworzony klucz AES-256 przez PBKDF2-HMAC-SHA256 (200k iteracji — parity
z legacy -iter 200000).

Format pliku `backup.tar.enc` (v2):
    b"GB2G" | salt(16) | nonce(12) | ciphertext+tag(16)
Brak magic b"GB2G" = artefakt sprzed migracji (format_version 1, CBC) —
sciezka decrypt utrzymana celowo, patrz `decrypt_payload`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .errors import BackupError
from .runner import CommandRunner

MAGIC = b"GB2G"
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32
PBKDF2_ITERATIONS = 200_000

# Wartosci pisane do metadata.json — jednorodne z format_version w backup.py.
ENCRYPTION_METHOD_V2 = "aes-256-gcm-pbkdf2-sha256"
ENCRYPTION_METHOD_V1 = "aes-256-cbc-pbkdf2-iter200k-sha256"


def _derive_key(key_material: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(key_material.encode("utf-8"))


def encrypt_payload(tar_file: Path, payload_file: Path, key_material: str) -> None:
    """Zaszyfruj tar do payload_file (format v2). Zuzywa losowy salt i nonce."""
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = _derive_key(key_material, salt)
    plaintext = tar_file.read_bytes()
    # AESGCM.encrypt zwraca ciphertext z doklejonym tagiem (16 B).
    token = AESGCM(key).encrypt(nonce, plaintext, None)
    payload_file.write_bytes(MAGIC + salt + nonce + token)


def decrypt_payload(
    payload_path: Path,
    tar_out: Path,
    key_material: str,
    runner: CommandRunner = None,
) -> str:
    """Odszyfruj payload do tar_out. Zwraca uzywany format ("v2" | "v1").

    Dispatch po MAGIC pliku — jest wczesniejszy niz metadata.json i nie da sie
    go przerobic bez psucia spojnosci artefaktu. v1 (CBC przez `openssl enc`)
    zostaje utrzymany celowo: istniejace kopie sprzed migracji MUSZA sie dac
    odtworzyc, restore drill nie moze czekac na nowy backup.
    """
    blob = payload_path.read_bytes()
    if blob[: len(MAGIC)] == MAGIC:
        salt = blob[len(MAGIC) : len(MAGIC) + SALT_LEN]
        nonce = blob[len(MAGIC) + SALT_LEN : len(MAGIC) + SALT_LEN + NONCE_LEN]
        token = blob[len(MAGIC) + SALT_LEN + NONCE_LEN :]
        key = _derive_key(key_material, salt)
        try:
            plaintext = AESGCM(key).decrypt(nonce, token, None)
        except InvalidTag as exc:
            raise BackupError(
                "E_INTEGRITY",
                "AES-GCM authentication failed: payload was modified or the "
                "encryption key does not match this backup",
            ) from exc
        tar_out.write_bytes(plaintext)
        return "v2"

    # Legacy format_version 1: CBC z iterowanym PBKDF2 wewnatrz openssl.
    code, out, err = runner.run(
        [
            "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
            "-md", "sha256",
            "-in", str(payload_path),
            "-out", str(tar_out),
            "-pass", "env:GALERA_BACKUP_ENCRYPTION_KEY",
        ],
        env={"GALERA_BACKUP_ENCRYPTION_KEY": key_material},
    )
    if code != 0:
        raise BackupError("E_INTEGRITY", f"Decryption failed: {err or out}")
    return "v1"


def openssl_supports_pbkdf2() -> bool:
    """Falsyfikowalny warunek testow legacy: lokalny openssl umie -pbkdf2."""
    probe = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "1", "-e",
         "-in", os.devnull, "-out", os.devnull, "-pass", "pass:x"],
        capture_output=True,
    )
    return probe.returncode == 0
