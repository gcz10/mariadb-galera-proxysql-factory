"""Szyfrowanie kopii: strumieniowy AES-256-GCM (v3) + odczyt v2/v1.

Format v3 usuwa buforowanie calego archiwum przez `AESGCM.encrypt/decrypt`.
Szyfrowanie i deszyfrowanie uzywa przyrostowego `Cipher(..., modes.GCM(...))`;
tag jest zapisywany jako stala przyczepa, wiec pamiec pozostaje ograniczona
rozmiarem pojedynczego fragmentu.

Format pliku `backup.tar.enc` (v3):
    b"GB3G" | salt(16) | nonce(12) | ciphertext | tag(16)

Naglowek v3 jest AAD, czyli jest uwierzytelniony, ale nie szyfrowany. Dane
odszyfrowane trafiaja najpierw do pliku tymczasowego i sa publikowane atomowo
dopiero po poprawnym `finalize()`. Dokumentacja uzytego API i warunku
weryfikacji tagu:
https://github.com/pyca/cryptography/blob/main/docs/hazmat/primitives/symmetric-encryption.rst

Czytnik zachowuje format v2 (`GB2G`, GCM bez AAD) oraz legacy v1 (CBC przez
`openssl enc`), aby istniejace kopie pozostaly odtwarzalne. Material klucza
pozostaje bez zmian: GALERA_BACKUP_ENCRYPTION_KEY i PBKDF2-HMAC-SHA256.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .errors import BackupError
from .runner import CommandRunner

MAGIC_V3 = b"GB3G"
MAGIC_V2 = b"GB2G"
SALT_LEN = 16
NONCE_LEN = 12
TAG_LEN = 16
KEY_LEN = 32
PBKDF2_ITERATIONS = 200_000
CHUNK_SIZE = 1024 * 1024
HEADER_LEN = len(MAGIC_V3) + SALT_LEN + NONCE_LEN

# Wartosci pisane do metadata.json. Nowe kopie uzywaja FORMAT_VERSION,
# a wszystkie czytniki akceptuja jawny zbior wersji migracyjnych.
FORMAT_VERSION = 3
GCM_ONESHOT_FORMAT_VERSION = 2
LEGACY_FORMAT_VERSION = 1
SUPPORTED_FORMAT_VERSIONS = frozenset(
    {LEGACY_FORMAT_VERSION, GCM_ONESHOT_FORMAT_VERSION, FORMAT_VERSION}
)
ENCRYPTION_METHOD_V3 = "aes-256-gcm-stream-pbkdf2-sha256"


def _derive_key(key_material: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(key_material.encode("utf-8"))


@contextmanager
def _atomic_target(target: Path) -> Iterator[Path]:
    """Udostepnij plik tymczasowy i podmien target dopiero po sukcesie."""
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        yield temporary
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_exact(source: BinaryIO, size: int, field: str) -> bytes:
    value = source.read(size)
    if len(value) != size:
        raise BackupError("E_INTEGRITY", f"Encrypted payload has a truncated {field}")
    return value


def _decrypt_gcm(
    payload_path: Path,
    tar_out: Path,
    key_material: str,
    *,
    expected_magic: bytes,
    authenticate_header: bool,
    format_name: str,
) -> str:
    payload_size = payload_path.stat().st_size
    if payload_size < HEADER_LEN + TAG_LEN:
        raise BackupError("E_INTEGRITY", "Encrypted payload is too short")

    ciphertext_size = payload_size - HEADER_LEN - TAG_LEN
    with payload_path.open("rb") as source:
        header = _read_exact(source, HEADER_LEN, "header")
        if header[: len(expected_magic)] != expected_magic:
            raise BackupError("E_INTEGRITY", "Encrypted payload format marker changed during decryption")

        salt_start = len(expected_magic)
        salt = header[salt_start : salt_start + SALT_LEN]
        nonce = header[salt_start + SALT_LEN :]
        source.seek(HEADER_LEN + ciphertext_size)
        tag = _read_exact(source, TAG_LEN, "authentication tag")
        source.seek(HEADER_LEN)

        decryptor = Cipher(
            algorithms.AES(_derive_key(key_material, salt)),
            modes.GCM(nonce, tag),
        ).decryptor()
        if authenticate_header:
            decryptor.authenticate_additional_data(header)

        try:
            with _atomic_target(tar_out) as temporary:
                with temporary.open("wb") as target:
                    remaining = ciphertext_size
                    while remaining:
                        chunk = source.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            raise BackupError(
                                "E_INTEGRITY",
                                "Encrypted payload was truncated during decryption",
                            )
                        remaining -= len(chunk)
                        target.write(decryptor.update(chunk))
                    target.write(decryptor.finalize())
        except InvalidTag as exc:
            raise BackupError(
                "E_INTEGRITY",
                "AES-GCM authentication failed: payload was modified or the "
                "encryption key does not match this backup",
            ) from exc

    return format_name


def encrypt_payload(tar_file: Path, payload_file: Path, key_material: str) -> None:
    """Zaszyfruj tar strumieniowo do atomowo publikowanego payloadu v3."""
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    header = MAGIC_V3 + salt + nonce
    encryptor = Cipher(
        algorithms.AES(_derive_key(key_material, salt)),
        modes.GCM(nonce),
    ).encryptor()
    encryptor.authenticate_additional_data(header)

    with _atomic_target(payload_file) as temporary:
        with tar_file.open("rb") as source, temporary.open("wb") as target:
            target.write(header)
            while chunk := source.read(CHUNK_SIZE):
                target.write(encryptor.update(chunk))
            target.write(encryptor.finalize())
            target.write(encryptor.tag)


def decrypt_payload(
    payload_path: Path,
    tar_out: Path,
    key_material: str,
    runner: CommandRunner,
) -> str:
    """Odszyfruj payload do tar_out; zwroc wykryty format v3, v2 albo v1."""
    with payload_path.open("rb") as source:
        magic = source.read(len(MAGIC_V3))

    if magic == MAGIC_V3:
        return _decrypt_gcm(
            payload_path,
            tar_out,
            key_material,
            expected_magic=MAGIC_V3,
            authenticate_header=True,
            format_name="v3",
        )

    if magic == MAGIC_V2:
        return _decrypt_gcm(
            payload_path,
            tar_out,
            key_material,
            expected_magic=MAGIC_V2,
            authenticate_header=False,
            format_name="v2",
        )

    # Legacy format_version 1: CBC z iterowanym PBKDF2 wewnatrz openssl.
    with _atomic_target(tar_out) as temporary:
        code, out, err = runner.run(
            [
                "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
                "-iter", "200000", "-md", "sha256",
                "-in", str(payload_path),
                "-out", str(temporary),
                "-pass", "env:GALERA_BACKUP_ENCRYPTION_KEY",
            ],
            env={"GALERA_BACKUP_ENCRYPTION_KEY": key_material},
        )
        if code != 0:
            raise BackupError("E_INTEGRITY", f"Decryption failed: {err or out}")
    return "v1"


