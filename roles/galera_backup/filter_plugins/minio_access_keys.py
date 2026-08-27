"""Fail-closed filters for MinIO service-account rotation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any


def minio_service_account_name(candidate: str, max_len: int = 32) -> str:
    """Return `candidate` bounded to `max_len` characters, deterministically.

    MinIO odrzuca nazwy kont serwisowych dluzsze niz 32 znaki ("name must not
    be longer than 32 characters" — zmierzone 2026-08-27: najemca o
    15-znakowej nazwie daje 35-znakowego kandydata). Uzycie w playbooku jest
    potokowe: `('galera-backup-prune-' ~ cluster.name) |
    minio_service_account_name` — filtr dostaje CALA zlozona nazwe i sam ja
    skraca. Nazwa musi byc DETERMINISTYCZNA: ta sama funkcja nadaje ja przy
    provision (`--name`) i odnajduje przy derejestracji
    (`minio_access_keys_named`). Skrot: pierwsze `max_len-13` znakow kandydata
    + `-` + 12 hex sha256 pelnej nazwy — stabilne miedzy przebiegami, a
    kolizja wymaga zderzenia nazw roznych klasterow w jednym 12-znakowym
    skrocie.
    """
    if not isinstance(candidate, str) or not candidate:
        raise ValueError("MinIO service-account name must be a non-empty string")
    if not isinstance(max_len, int) or max_len < 14:
        raise ValueError("max_len must leave room for '-' and a 12-char digest")
    if len(candidate) <= max_len:
        return candidate
    keep = max_len - 13
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
    return f"{candidate[:keep]}-{digest}"


def _json_object(raw: str, source: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{source} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{source} must be a JSON object")
    if value.get("status") != "success":
        raise ValueError(f"{source} did not report success")
    return value


def minio_service_account_keys(output: str) -> list[str]:
    """Return every service-account access key from `mc ... list --json`."""
    if not isinstance(output, str):
        raise ValueError("MinIO access-key list output must be text")

    keys: list[str] = []
    for line_number, raw_line in enumerate(output.splitlines(), start=1):
        if not raw_line.strip():
            continue
        record = _json_object(raw_line, f"line {line_number}")
        accounts = record.get("svcaccs")
        if accounts is None:
            # Swiezo postawione MinIO zwraca dla principala bez kont serwisowych:
            #   {"status":"success","user":"...","stsKeys":null,"svcaccs":null}
            # `null` to legalne "zero kont", nie uszkodzone wyjscie — inaczej
            # pierwszy backup na nowej instancji nie moze przejsc. Wartosc
            # OBECNA, ale zlego typu nadal jest bledem: to znak, ze `mc`
            # zmienil kontrakt i cicha akceptacja gubilaby istniejace klucze.
            continue
        if not isinstance(accounts, list):
            raise ValueError(f"line {line_number} svcaccs must be a list")
        for account in accounts:
            if not isinstance(account, dict):
                raise ValueError(f"line {line_number} contains an invalid service account")
            access_key = account.get("accessKey")
            if not isinstance(access_key, str) or not access_key:
                raise ValueError(f"line {line_number} service account has no accessKey")
            if access_key not in keys:
                keys.append(access_key)
    return keys


def minio_access_keys_named(info_outputs: Iterable[str], target_name: str) -> list[str]:
    """Select actual access keys whose `mc ... info --json` name matches exactly."""
    if not isinstance(target_name, str) or not target_name:
        raise ValueError("MinIO service-account target name must be non-empty")

    keys: list[str] = []
    for item_number, raw_info in enumerate(info_outputs, start=1):
        info = _json_object(raw_info, f"info item {item_number}")
        if info.get("name") != target_name:
            continue
        access_key = info.get("accessKey")
        if not isinstance(access_key, str) or not access_key:
            raise ValueError("matching service account has no accessKey")
        if access_key not in keys:
            keys.append(access_key)
    return keys


class FilterModule:
    """Expose filters to Ansible."""

    def filters(self) -> dict[str, object]:
        return {
            "minio_service_account_keys": minio_service_account_keys,
            "minio_service_account_name": minio_service_account_name,
            "minio_access_keys_named": minio_access_keys_named,
        }
