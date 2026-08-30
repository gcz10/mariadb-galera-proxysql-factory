"""Fabryka backendow kopii (s3/smb/filesystem) z rozdzieleniem poswiadczen write/retention.

Wydzielone z pipeline.py (refaktor strukturalny, zachowanie 1:1).
pipeline.py pozostaje facade: re-eksportuje te nazwy, zeby testy
(patch.object(pipeline, ...)) i tests/live dalej rozwiazywaly je w jednym
namespace — to jest kontrakt udokumentowany w docstringu pipeline.py.
"""

from __future__ import annotations

from typing import Any, Optional

from .errors import BackupError
from .config import RunConfig
from .runner import CommandRunner
from .storage.filesystem import FilesystemBackend, SMBBackend
from .storage.s3 import S3Backend


def get_storage_backend(
    cfg: RunConfig,
    secrets: dict[str, str],
    runner: Optional["CommandRunner"] = None,
    purpose: str = "write",
) -> Any:
    """Zbuduj backend kopii; `purpose="retention"` bierze poswiadczenie z delete.

    Rozdzielenie jest bezpieczenstwem, nie kosmetyka. Donora wybiera runner przy
    starcie, wiec poswiadczenie ZAPISU lezy na kazdym wezle Galery i nie moze
    miec prawa kasowania — inaczej kompromitacja dowolnego wezla bazy kasuje
    historie off-cluster. Klucz retencji dostaje wylacznie koordynator
    (`backup.scheduler.host`), patrz roles/galera_backup/templates/minio-policy-prune.json.j2.
    """
    from . import pipeline  # late binding: testy patchuja pipeline.S3Backend (kontrakt w docstringu pipeline)
    b_type = str(cfg.backend.get("type", cfg.backend.get("destination", ""))).lower()
    if b_type == "s3":
        if purpose == "retention":
            access_key = secrets.get("GALERA_BACKUP_S3_PRUNE_ACCESS_KEY", "")
            secret_key = secrets.get("GALERA_BACKUP_S3_PRUNE_SECRET_KEY", "")
            if not access_key or not secret_key:
                raise BackupError(
                    "E_SECRETS",
                    "Retention credentials are absent: this host is not the retention coordinator",
                )
        else:
            access_key = secrets["GALERA_BACKUP_S3_ACCESS_KEY"]
            secret_key = secrets["GALERA_BACKUP_S3_SECRET_KEY"]
        return pipeline.S3Backend(
            endpoint=cfg.backend["endpoint"],
            bucket=cfg.backend["bucket"],
            secure=cfg.backend.get("secure", False),
            access_key=access_key,
            secret_key=secret_key,
            cluster_name=cfg.cluster_name,
        )
    elif b_type == "smb":
        return pipeline.SMBBackend(
            source=cfg.backend["source"],
            mount_point=cfg.backend["mount_point"],
            options=cfg.backend.get("options", []),
            username=secrets["GALERA_BACKUP_SMB_USERNAME"],
            password=secrets["GALERA_BACKUP_SMB_PASSWORD"],
            domain=secrets.get("GALERA_BACKUP_SMB_DOMAIN"),
            cluster_name=cfg.cluster_name,
            runner=runner,
        )
    elif b_type == "filesystem":
        return pipeline.FilesystemBackend(
            mount_point=cfg.backend["mount_point"],
            expected_fstype=cfg.backend.get("expected_fstype", ""),
            cluster_name=cfg.cluster_name,
        )
    else:
        raise BackupError("E_CONFIG", f"Unknown backend type '{b_type}'")
