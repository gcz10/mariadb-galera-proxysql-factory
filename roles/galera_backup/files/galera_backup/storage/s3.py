"""Backend S3 / MinIO."""

from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..errors import BackupError, combine_failures
from ..fsutil import file_sha256_and_size
from .artifacts import (
    ArtifactSet,
    DRILL_MARKER_S3_PREFIX,
    PublishedArtifact,
    metadata_unixtime,
)


class S3Backend:
    def __init__(
        self,
        endpoint: str,
        bucket: str,
        secure: bool,
        access_key: str,
        secret_key: str,
        cluster_name: str,
        client: Optional[Any] = None,
    ):
        self.endpoint = endpoint
        self.bucket = bucket
        self.secure = secure
        self.access_key = access_key
        self.secret_key = secret_key
        self.cluster_name = cluster_name
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import minio
            except ImportError:
                raise BackupError("E_STORAGE", "MinIO Python SDK ('minio') is not installed")
            try:
                self._client = minio.Minio(
                    endpoint=self.endpoint,
                    access_key=self.access_key,
                    secret_key=self.secret_key,
                    secure=self.secure,
                )
            except Exception as exc:
                raise BackupError("E_STORAGE_AUTH", f"Failed to initialize MinIO client: {exc}")
        return self._client

    def preflight(self) -> None:
        try:
            if not self.client.bucket_exists(self.bucket):
                raise BackupError("E_STORAGE", f"S3 bucket '{self.bucket}' does not exist")
        except BackupError:
            raise
        except Exception as exc:
            raise BackupError("E_STORAGE_AUTH", f"S3 bucket preflight error on '{self.bucket}': {exc}")

        owner_key = "galera-backup-owner.json"

        try:
            objs = list(self.client.list_objects(self.bucket, prefix=owner_key, recursive=False))
            owner_objs = [o for o in objs if getattr(o, "object_name", "") == owner_key]
        except Exception as exc:
            raise BackupError("E_STORAGE_AUTH", f"Failed to list S3 objects in bucket '{self.bucket}': {exc}")

        if owner_objs:
            try:
                resp = self.client.get_object(self.bucket, owner_key)
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("cluster_name") != self.cluster_name or data.get("format_version") != 1:
                    raise BackupError(
                        "E_OWNER_CONFLICT",
                        f"S3 bucket '{self.bucket}' is owned by another cluster '{data.get('cluster_name')}'"
                    )
            except BackupError:
                raise
            except Exception as exc:
                raise BackupError("E_OWNER_CONFLICT", f"Failed to read owner marker from bucket '{self.bucket}': {exc}")
        else:
            raise BackupError(
                "E_OWNER_CONFLICT",
                f"S3 bucket '{self.bucket}' has no owner marker '{owner_key}'; "
                "a storage administrator must provision it before scoped credentials are used",
            )

    def publish(self, artifact: ArtifactSet) -> PublishedArtifact:
        prefix = f"{artifact.backup_name}/"
        payload_key = f"{prefix}backup.tar.enc"
        checksum_key = f"{prefix}backup.sha256"
        metadata_key = f"{prefix}metadata.json"

        try:
            existing = list(
                self.client.list_objects(
                    self.bucket,
                    prefix=prefix,
                    recursive=True,
                )
            )
        except Exception as exc:
            raise BackupError(
                "E_STORAGE",
                f"Failed to check S3 destination prefix '{prefix}': {exc}",
            ) from exc
        if existing:
            raise BackupError(
                "E_STORAGE",
                f"S3 destination prefix '{prefix}' already exists; refusing to overwrite it",
            )

        try:
            with open(artifact.metadata_path, "r", encoding="utf-8") as metadata_file:
                meta = json.load(metadata_file)
            expected_size = int(meta["encrypted_size_bytes"])
            expected_sha = str(meta["encrypted_sha256"])
            unixtime = int(meta.get("created_unixtime", int(time.time())))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BackupError(
                "E_INTEGRITY",
                f"Invalid local metadata for S3 publication '{prefix}': {exc}",
            ) from exc

        uploaded_keys: list[str] = []
        readback_path: Optional[Path] = None
        try:
            try:
                self.client.fput_object(
                    self.bucket,
                    payload_key,
                    str(artifact.payload_path),
                )
                uploaded_keys.append(payload_key)
                self.client.fput_object(
                    self.bucket,
                    checksum_key,
                    str(artifact.checksum_path),
                )
                uploaded_keys.append(checksum_key)
            except Exception as exc:
                raise BackupError(
                    "E_STORAGE",
                    f"Failed to upload S3 backup objects under '{prefix}': {exc}",
                ) from exc

            try:
                with tempfile.NamedTemporaryFile("wb", delete=False) as readback_file:
                    readback_path = Path(readback_file.name)
                self.client.fget_object(
                    self.bucket,
                    payload_key,
                    str(readback_path),
                )
                readback_sha, readback_size = file_sha256_and_size(readback_path)
            except Exception as exc:
                raise BackupError(
                    "E_INTEGRITY",
                    f"Failed read-back verification of '{payload_key}': {exc}",
                ) from exc

            if readback_size != expected_size:
                raise BackupError(
                    "E_INTEGRITY",
                    f"Read-back size mismatch for '{payload_key}': "
                    f"expected {expected_size}, got {readback_size}",
                )
            if readback_sha != expected_sha:
                raise BackupError(
                    "E_INTEGRITY",
                    f"Read-back SHA-256 mismatch for '{payload_key}': "
                    f"expected {expected_sha}, got {readback_sha}",
                )

            try:
                readback_path.unlink()
                readback_path = None
            except OSError as exc:
                raise BackupError(
                    "E_STORAGE",
                    f"Failed to remove S3 read-back file '{readback_path}': {exc}",
                ) from exc

            try:
                self.client.fput_object(
                    self.bucket,
                    metadata_key,
                    str(artifact.metadata_path),
                )
                uploaded_keys.append(metadata_key)
            except Exception as exc:
                raise BackupError(
                    "E_STORAGE",
                    f"Failed to publish S3 completion metadata '{metadata_key}': {exc}",
                ) from exc
        except Exception as exc:
            failure = (
                exc
                if isinstance(exc, BackupError)
                else BackupError(
                    "E_STORAGE",
                    f"S3 publication failed under '{prefix}': {exc}",
                )
            )
            cleanup_failures: list[str] = []

            if readback_path is not None and readback_path.exists():
                try:
                    readback_path.unlink()
                except OSError as cleanup_exc:
                    cleanup_failures.append(
                        f"read-back file cleanup failed: {cleanup_exc}"
                    )

            object_cleanup_errors: list[str] = []
            for object_key in reversed(uploaded_keys):
                try:
                    self.client.remove_object(self.bucket, object_key)
                except Exception as cleanup_exc:
                    object_cleanup_errors.append(
                        f"object cleanup failed for '{object_key}': {cleanup_exc}"
                    )

            # An S3 upload can commit an object and still raise before the SDK
            # returns. Discover and remove every residual key owned by this
            # failed, previously-empty prefix rather than trusting the local
            # uploaded_keys journal alone.
            try:
                remaining = list(
                    self.client.list_objects(
                        self.bucket,
                        prefix=prefix,
                        recursive=True,
                    )
                )
                for item in remaining:
                    object_key = getattr(item, "object_name", "")
                    if not object_key.startswith(prefix):
                        object_cleanup_errors.append(
                            f"refused to delete unexpected key '{object_key}'"
                        )
                        continue
                    try:
                        self.client.remove_object(self.bucket, object_key)
                    except Exception as cleanup_exc:
                        object_cleanup_errors.append(
                            f"object cleanup failed for '{object_key}': {cleanup_exc}"
                        )

                residual = list(
                    self.client.list_objects(
                        self.bucket,
                        prefix=prefix,
                        recursive=True,
                    )
                )
                if residual:
                    residual_names = [
                        getattr(item, "object_name", "<unknown>")
                        for item in residual
                    ]
                    cleanup_failures.extend(object_cleanup_errors)
                    cleanup_failures.append(
                        f"objects remain under failed prefix: {residual_names}"
                    )
            except Exception as cleanup_exc:
                cleanup_failures.extend(object_cleanup_errors)
                cleanup_failures.append(
                    f"failed-prefix cleanup or verification failed: {cleanup_exc}"
                )

            if cleanup_failures:
                failure = combine_failures(
                    failure,
                    BackupError(
                        "E_STORAGE",
                        "; ".join(cleanup_failures),
                    ),
                    "E_STORAGE",
                )
            raise failure

        return PublishedArtifact(
            backup_name=artifact.backup_name,
            prefix=prefix,
            encrypted_sha256=expected_sha,
            encrypted_size=expected_size,
            unixtime=unixtime,
        )

    def fetch_latest(self, work_dir: Path) -> ArtifactSet:
        cluster_prefix = f"galera-{self.cluster_name}-"
        all_objs = list(self.client.list_objects(self.bucket, prefix=cluster_prefix, recursive=True))

        prefixes: dict[str, dict[str, Any]] = {}
        for o in all_objs:
            obj_name = getattr(o, "object_name", "")
            parts = obj_name.split("/")
            if len(parts) >= 2:
                b_prefix = parts[0]
                fname = parts[1]
                prefixes.setdefault(b_prefix, {})[fname] = o

        candidates = []
        for b_prefix, files in prefixes.items():
            if "backup.tar.enc" in files and "backup.sha256" in files and "metadata.json" in files:
                meta_key = f"{b_prefix}/metadata.json"
                try:
                    resp = self.client.get_object(self.bucket, meta_key)
                    meta = json.loads(resp.read().decode("utf-8"))
                    if meta.get("cluster_name") == self.cluster_name and meta.get("format_version") == 1:
                        candidates.append((metadata_unixtime(meta, meta_key), b_prefix, meta))
                except BackupError:
                    raise
                except Exception as exc:
                    raise BackupError(
                        "E_STORAGE",
                        f"Unreadable metadata in '{meta_key}'; refusing to silently fall back to an older backup: {exc}",
                    )

        if not candidates:
            raise BackupError("E_STORAGE", f"No complete backups found in S3 bucket '{self.bucket}' for cluster '{self.cluster_name}'")

        candidates.sort(key=lambda x: x[0], reverse=True)
        best_ts, best_prefix, best_meta = candidates[0]

        work_dir.mkdir(parents=True, exist_ok=True)
        payload_path = work_dir / "backup.tar.enc"
        checksum_path = work_dir / "backup.sha256"
        metadata_path = work_dir / "metadata.json"

        self.client.fget_object(self.bucket, f"{best_prefix}/backup.tar.enc", str(payload_path))
        self.client.fget_object(self.bucket, f"{best_prefix}/backup.sha256", str(checksum_path))
        self.client.fget_object(self.bucket, f"{best_prefix}/metadata.json", str(metadata_path))

        return ArtifactSet(
            backup_name=best_prefix,
            payload_path=payload_path,
            checksum_path=checksum_path,
            metadata_path=metadata_path,
        )

    def prune(self, now: datetime, retention_days: int) -> int:
        from datetime import timedelta
        cluster_prefix = f"galera-{self.cluster_name}-"
        all_objs = list(self.client.list_objects(self.bucket, prefix=cluster_prefix, recursive=True))

        prefixes: dict[str, list[Any]] = {}
        for o in all_objs:
            obj_name = getattr(o, "object_name", "")
            parts = obj_name.split("/")
            if len(parts) >= 2:
                b_prefix = parts[0]
                prefixes.setdefault(b_prefix, []).append(o)

        cutoff_ts = (now - timedelta(days=retention_days)).timestamp()
        deleted_count = 0

        for b_prefix, obj_list in prefixes.items():
            meta_key = f"{b_prefix}/metadata.json"
            meta_objs = [o for o in obj_list if getattr(o, "object_name", "") == meta_key]
            if not meta_objs:
                continue
            try:
                resp = self.client.get_object(self.bucket, meta_key)
                meta = json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                raise BackupError("E_STORAGE", f"Malformed metadata in '{meta_key}' during retention: {exc}")

            created_ts = metadata_unixtime(meta, meta_key)
            if created_ts < cutoff_ts:
                del_list = [getattr(o, "object_name", "") for o in obj_list]
                for item_name in del_list:
                    if item_name:
                        self.client.remove_object(self.bucket, item_name)
                deleted_count += 1

        return deleted_count

    # === Znacznik restore drill (patrz storage/artifacts.py) ===
    # Klucz lezy POZA prefiksem `galera-<cluster>-`, ktory skanuja `fetch_latest`
    # i `prune`, wiec retencja go nie usuwa.
    def _drill_marker_key(self) -> str:
        return f"{DRILL_MARKER_S3_PREFIX}/{self.cluster_name}.json"

    def write_drill_marker(self, marker: dict[str, Any]) -> None:
        key = self._drill_marker_key()
        payload = json.dumps(marker, indent=2).encode("utf-8")
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        try:
            self.client.fput_object(
                self.bucket,
                key,
                str(tmp_path),
                content_type="application/json",
            )
        except Exception as exc:
            raise BackupError(
                "E_STORAGE",
                f"Failed to write restore drill marker '{key}': {exc}",
            ) from exc
        finally:
            tmp_path.unlink(missing_ok=True)

    def read_drill_marker(self) -> Optional[dict[str, Any]]:
        key = self._drill_marker_key()
        try:
            resp = self.client.get_object(self.bucket, key)
            return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            # Brak znacznika to normalny stan (klaster bez ani jednego drillu),
            # a nie blad backupu. Rozrozniamy go po nazwie wyjatku SDK, zeby
            # awaria uwierzytelnienia albo sieci NIE udawala pustego znacznika.
            if type(exc).__name__ == "S3Error" and getattr(exc, "code", "") == "NoSuchKey":
                return None
            raise BackupError(
                "E_STORAGE",
                f"Failed to read restore drill marker '{key}': {exc}",
            ) from exc

    def close(self) -> None:
        pass
