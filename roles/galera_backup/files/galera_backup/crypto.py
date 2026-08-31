"""Uwierzytelnianie payloadu backupu: Encrypt-then-MAC (F2).

Kryptografia symetryczna backupu (AES-256-CBC przez `openssl enc`) nie
uwierzytelnia ciphertextu: atakujacy z prawem zapisu moze przerobic
`backup.tar.enc` i przepisac niezakluczone `sha256_encrypted` w
`metadata.json`, oszukujac kontrole spelnialnosci. Dodaje wiec kluczowany
HMAC-SHA256 naliczony na CALE ciphertext (Encrypt-then-MAC), wyprowadzony
z tego samego passphrase co klucz szyfrujacy, ale z WYDZIELONYM soleniem
(i separatorem domeny), tak aby klucz MAC nie pokrywal sie z kluczem CBC.

Decyzje projektowe:
- AES-256-GCM nie jest opcja: `openssl enc` odmawia operowania ciperami
  AEAD ("AEAD ciphers not supported", zweryfikowane na OpenSSL 3.5.5),
  a repozytorium celowo szyfruje przez `openssl` (klucz przechodzi w
  `env:GALERA_BACKUP_ENCRYPTION_KEY`, nie w argv). Wyprowadzenie klucza
  MAC robimy w czystym Pythonie (stdlib `hashlib`), aby sekret nie
  trafial do linii polecenia; PBKDF2 jest bajt w bajt zgodny z
  `openssl kdf -keylen 32 -kdfopt digest:SHA256 -kdfopt iter:200000`
  (por. test `test_kat_matches_openssl_kdf` w `test_galera_backup_etm.py`).
- Format: nowe backupy nosza w `metadata.json` pola `hmac_sha256` i
  `hmac_salt`; rozszerzenie `encryption_method` o `+etm-hmac-sha256`.
  `format_version` zostaje 1 — enumeracja (filesystem/s3) i retencja
  filtruja po `format_version == 1`, a wiec "wersja 2" uczynilaby nowe
  backupy niewidoczne. Starsze backupy bez pola `hmac_sha256` przechodza
  niezmieniona sciezka (backward compat); rozrznikuje obecno pola.
- Fail-closed: przy odtwarzaniu MAC jest weryfikowany PRZED deszyfrowaniem;
  nieodpowiedni salt hex lub tag = E_INTEGRITY, payload nie opuszcza stagingu.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

# Ilosc iteracji PBKDF2 spójna z kluczem szyfrujacym (openssl -iter 200000).
ETM_PBKDF2_ITERATIONS = 200_000

# Separator domeny: odroznia wyprowadzenie klucza MAC od klucza CBC i od
# jakiegokolwiek innego użycia passphrase w systemie.
ETM_DOMAIN = b"galera-backup-etm-v1"

_CHUNK = 1024 * 1024


def derive_etm_key(passphrase: str, salt: bytes, iterations: int = ETM_PBKDF2_ITERATIONS) -> bytes:
    """Wyprowadza 256-bitowy klucz HMAC-SHA256 (PBKDF2-SHA256).

    Salt jest losowy per backup i przechowywany w `metadata.json`
    (`hmac_salt`) — tak jak salt `openssl enc` jest zakodowany w naglowku
    ciphertextu, wiec plik sam w sobie jest autonomiczny.
    """
    return hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, iterations, 32
    )


def _mac_key_and_domain(passphrase: str, salt: bytes, iterations: int = ETM_PBKDF2_ITERATIONS) -> tuple[bytes, bytes]:
    key = derive_etm_key(passphrase, salt, iterations)
    return key, ETM_DOMAIN + salt


def compute_etm_tag(
    passphrase: str,
    salt: bytes,
    ciphertext_path: Path,
    iterations: int = ETM_PBKDF2_ITERATIONS,
) -> str:
    """Nalicza HMAC-SHA256 na calym pliku ciphertext (chunkami, bez RAM-u)."""
    key, prefix = _mac_key_and_domain(passphrase, salt, iterations)
    mac = hmac.new(key, prefix, hashlib.sha256)
    with open(ciphertext_path, "rb") as f:
        while chunk := f.read(_CHUNK):
            mac.update(chunk)
    return mac.hexdigest()


def verify_etm(
    passphrase: str,
    salt_hex: str,
    expected_tag: str,
    ciphertext_path: Path,
    iterations: int = ETM_PBKDF2_ITERATIONS,
) -> None:
    """Weryfikuje tag fail-closed. Rzuca `ValueError` przy uszkodzonym
    salt/tag (format) — wylacznik konwersji na E_INTEGRITY jest w callerze,
    bo tam mieszka `BackupError` (ten modul nie zalezy od reszty pakietu).
    """
    try:
        salt = bytes.fromhex(salt_hex)
        if len(salt) != 16:
            raise ValueError(f"hmac_salt must be 16 bytes, got {len(salt)}")
    except ValueError as exc:
        raise ValueError(f"Invalid hmac_salt: {exc}") from exc
    if not isinstance(expected_tag, str) or len(expected_tag) != 64:
        raise ValueError("Invalid hmac_sha256 tag format")
    actual = compute_etm_tag(passphrase, salt, ciphertext_path, iterations)
    if not hmac.compare_digest(actual, expected_tag.lower()):
        raise ValueError("HMAC-SHA256 mismatch: encrypted payload was modified")
