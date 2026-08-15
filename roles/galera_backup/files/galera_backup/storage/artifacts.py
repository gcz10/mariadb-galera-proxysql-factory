"""Modele artefaktu kopii i odczyt znacznika czasu z metadanych."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import BackupError


@dataclass
class ArtifactSet:
    backup_name: str
    payload_path: Path
    checksum_path: Path
    metadata_path: Path


@dataclass
class PublishedArtifact:
    backup_name: str
    prefix: str
    encrypted_sha256: str
    encrypted_size: int
    unixtime: int


def metadata_unixtime(meta: dict[str, Any], object_name: str) -> int:
    created_ts = meta.get("created_unixtime")
    if type(created_ts) is not int or created_ts < 0:
        raise BackupError(
            "E_STORAGE",
            f"Invalid integer created_unixtime in metadata '{object_name}'",
        )
    return created_ts
