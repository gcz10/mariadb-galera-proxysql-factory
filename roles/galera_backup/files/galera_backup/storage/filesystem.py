"""Backend lokalnego systemu plikow oraz SMB/CIFS."""

# Patrz komentarz w s3.py — adnotacje leniwe, `CommandRunner` zostaje w entrypoincie.
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..errors import BackupError, combine_failures
from ..fsutil import file_sha256_and_size, remove_tree_or_raise
from ..textutil import normalize_smb_source, sanitize_cluster_name, validate_smb_options
from .artifacts import ArtifactSet, PublishedArtifact, metadata_unixtime


class FilesystemBackend:
    def __init__(self, mount_point: Path | str, expected_fstype: str, cluster_name: str):
        self.mount_point = Path(mount_point).resolve()
        self.expected_fstype = expected_fstype.strip().lower() if expected_fstype else ""
        self.cluster_name = cluster_name
        self._initial_mount_info: Optional[dict[str, str]] = None

    def _get_mount_info(self) -> dict[str, str]:
        # Try findmnt CLI first
        target_str = str(self.mount_point)
        try:
            p = subprocess.run(
                ["findmnt", "--json", "--target", target_str, "--output", "TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN,FSROOT"],
                capture_output=True,
                text=True,
            )
            if p.returncode == 0 and p.stdout:
                data = json.loads(p.stdout)
                filesystems = data.get("filesystems", [])
                if filesystems:
                    fs = filesystems[0]
                    target = fs.get("target", "")
                    if target and Path(target).resolve() == self.mount_point:
                        return {
                            "target": target,
                            "source": fs.get("source", ""),
                            "fstype": fs.get("fstype", "").lower(),
                            "options": fs.get("options", ""),
                            "majmin": fs.get("maj:min", fs.get("majmin", "")),
                            "fsroot": fs.get("fsroot", ""),
                        }
        except Exception:
            pass

        # Fallback to /proc/mounts if findmnt failed or returned different target
        if Path("/proc/mounts").exists():
            try:
                with open("/proc/mounts", "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 4:
                            src, tgt, fst, opts = parts[0], parts[1], parts[2], parts[3]
                            if Path(tgt).resolve() == self.mount_point:
                                return {
                                    "target": tgt,
                                    "source": src,
                                    "fstype": fst.lower(),
                                    "options": opts,
                                    "majmin": "",
                                    "fsroot": "/",
                                }
            except Exception:
                pass

        raise BackupError("E_STORAGE", f"Path '{self.mount_point}' is not an active mount point")

    def preflight(
        self,
        pre_write_validator: Optional[Callable[[dict[str, str]], None]] = None,
    ) -> None:
        info = self._get_mount_info()
        self._initial_mount_info = info

        target = info.get("target", "")
        if target == "/" or self.mount_point == Path("/"):
            raise BackupError("E_STORAGE", "Backup mount point cannot be root filesystem '/'")

        fstype = info.get("fstype", "").lower()
        if self.expected_fstype and fstype != self.expected_fstype:
            raise BackupError(
                "E_STORAGE",
                f"Mount fstype '{fstype}' does not match expected '{self.expected_fstype}'"
            )

        if pre_write_validator is not None:
            pre_write_validator(info)

        cluster_dir = self.mount_point / self.cluster_name
        cluster_dir.mkdir(parents=True, exist_ok=True)

        owner_file = cluster_dir / "galera-backup-owner.json"
        owner_content = json.dumps({"format_version": 1, "cluster_name": self.cluster_name})

        if owner_file.exists():
            try:
                data = json.loads(owner_file.read_text(encoding="utf-8"))
                if data.get("cluster_name") != self.cluster_name or data.get("format_version") != 1:
                    raise BackupError(
                        "E_OWNER_CONFLICT",
                        f"Filesystem storage '{cluster_dir}' is owned by another cluster '{data.get('cluster_name')}'"
                    )
            except BackupError:
                raise
            except Exception as exc:
                raise BackupError("E_OWNER_CONFLICT", f"Failed to read owner marker from '{owner_file}': {exc}")
        else:
            try:
                fd = os.open(str(owner_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(owner_content)
            except FileExistsError:
                pass
            except Exception as exc:
                raise BackupError("E_STORAGE", f"Failed to write owner marker to '{owner_file}': {exc}")

    def _verify_mount_identity(self) -> None:
        if self._initial_mount_info is None:
            return
        curr = self._get_mount_info()
        for field in ("target", "source", "fstype"):
            if curr.get(field) != self._initial_mount_info.get(field):
                raise BackupError("E_STORAGE", f"Mount identity changed during execution (field {field} mismatch)")

    def publish(self, artifact: ArtifactSet) -> PublishedArtifact:
        self._verify_mount_identity()

        cluster_dir = self.mount_point / self.cluster_name
        partial_dir = cluster_dir / f".partial-{artifact.backup_name}"
        final_dir = cluster_dir / artifact.backup_name

        if partial_dir.exists():
            remove_tree_or_raise(
                partial_dir,
                "E_STORAGE",
                "stale partial backup directory",
            )
        if final_dir.exists():
            raise BackupError(
                "E_STORAGE",
                f"Backup destination '{final_dir}' already exists; refusing to overwrite it",
            )
        partial_dir.mkdir(parents=True)

        try:
            dest_payload = partial_dir / "backup.tar.enc"
            dest_checksum = partial_dir / "backup.sha256"
            dest_metadata = partial_dir / "metadata.json"

            shutil.copy2(artifact.payload_path, dest_payload)
            shutil.copy2(artifact.checksum_path, dest_checksum)
            shutil.copy2(artifact.metadata_path, dest_metadata)

            with open(artifact.metadata_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

            expected_size = int(meta["encrypted_size_bytes"])
            expected_sha = str(meta["encrypted_sha256"])
            unixtime = int(meta.get("created_unixtime", int(time.time())))

            copied_sha, copied_size = file_sha256_and_size(dest_payload)
            if copied_size != expected_size:
                raise BackupError(
                    "E_INTEGRITY",
                    f"Copied payload size mismatch: expected {expected_size}, got {copied_size}",
                )
            if copied_sha != expected_sha:
                raise BackupError(
                    "E_INTEGRITY",
                    f"Copied payload SHA-256 mismatch: expected {expected_sha}, got {copied_sha}"
                )

            self._verify_mount_identity()

            os.replace(partial_dir, final_dir)

            self._verify_mount_identity()

            return PublishedArtifact(
                backup_name=artifact.backup_name,
                prefix=str(final_dir),
                encrypted_sha256=expected_sha,
                encrypted_size=expected_size,
                unixtime=unixtime,
            )
        except Exception as exc:
            failure = exc
            if partial_dir.exists():
                try:
                    remove_tree_or_raise(
                        partial_dir,
                        "E_STORAGE",
                        "failed partial backup directory",
                    )
                except BackupError as cleanup_exc:
                    failure = combine_failures(
                        failure,
                        cleanup_exc,
                        "E_STORAGE",
                    )
            if failure is exc:
                raise
            raise failure

    def fetch_latest(self, work_dir: Path) -> ArtifactSet:
        self._verify_mount_identity()

        cluster_dir = self.mount_point / self.cluster_name
        if not cluster_dir.exists():
            raise BackupError("E_STORAGE", f"Cluster directory '{cluster_dir}' does not exist")

        candidates = []
        for child in cluster_dir.iterdir():
            if child.is_dir() and child.name.startswith(f"galera-{self.cluster_name}-") and not child.name.startswith(".partial-"):
                payload = child / "backup.tar.enc"
                checksum = child / "backup.sha256"
                metadata = child / "metadata.json"
                if payload.exists() and checksum.exists() and metadata.exists():
                    try:
                        meta = json.loads(metadata.read_text(encoding="utf-8"))
                        if meta.get("cluster_name") == self.cluster_name and meta.get("format_version") == 1:
                            candidates.append((metadata_unixtime(meta, str(metadata)), child, meta))
                    except BackupError:
                        raise
                    except Exception as exc:
                        raise BackupError(
                            "E_STORAGE",
                            f"Unreadable metadata in '{metadata}'; refusing to silently fall back to an older backup: {exc}",
                        )

        if not candidates:
            raise BackupError("E_STORAGE", f"No complete backups found in filesystem mount '{self.mount_point}' for cluster '{self.cluster_name}'")

        candidates.sort(key=lambda x: x[0], reverse=True)
        best_ts, best_dir, best_meta = candidates[0]

        work_dir.mkdir(parents=True, exist_ok=True)
        dest_payload = work_dir / "backup.tar.enc"
        dest_checksum = work_dir / "backup.sha256"
        dest_metadata = work_dir / "metadata.json"

        shutil.copy2(best_dir / "backup.tar.enc", dest_payload)
        shutil.copy2(best_dir / "backup.sha256", dest_checksum)
        shutil.copy2(best_dir / "metadata.json", dest_metadata)

        return ArtifactSet(
            backup_name=best_dir.name,
            payload_path=dest_payload,
            checksum_path=dest_checksum,
            metadata_path=dest_metadata,
        )

    def prune(self, now: datetime, retention_days: int) -> int:
        from datetime import timedelta
        self._verify_mount_identity()

        cluster_dir = self.mount_point / self.cluster_name
        if not cluster_dir.exists():
            return 0

        cutoff_ts = (now - timedelta(days=retention_days)).timestamp()
        deleted_count = 0

        for child in cluster_dir.iterdir():
            if child.is_dir() and child.name.startswith(f"galera-{self.cluster_name}-") and not child.name.startswith(".partial-"):
                metadata = child / "metadata.json"
                if not metadata.exists():
                    continue
                try:
                    meta = json.loads(metadata.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise BackupError("E_STORAGE", f"Malformed metadata in '{metadata}' during retention: {exc}")

                created_ts = metadata_unixtime(meta, str(metadata))
                if created_ts < cutoff_ts:
                    remove_tree_or_raise(
                        child,
                        "E_STORAGE",
                        "expired backup directory",
                    )
                    deleted_count += 1

        return deleted_count

    def close(self) -> None:
        pass
class SMBBackend:
    def __init__(
        self,
        source: str,
        mount_point: Path | str,
        options: list[str],
        username: str,
        password: str,
        domain: Optional[str],
        cluster_name: str,
        # Typ realny: Optional[CommandRunner]. Klasa zostaje w entrypoincie do
        # czasu wydzielenia runner.py, a `Any` unika cyklu import<->fasada.
        runner: Optional[Any] = None,
    ):
        self.source = source
        self.mount_point = Path(mount_point).resolve()
        self.options = list(options)
        self.username = username
        self.password = password
        self.domain = domain
        self.cluster_name = sanitize_cluster_name(cluster_name)
        self.fs_backend = FilesystemBackend(self.mount_point, expected_fstype="cifs", cluster_name=self.cluster_name)
        self._credentials_file: Optional[Path] = None
        self._is_mounted = False
        self._cifs_unavailable_reason = ""
        # Mount/umount go through the shared CommandRunner so the argv guard and
        # redactor cover them; credentials stay in the 0600 credentials file and
        # never enter argv. Tests construct SMBBackend without a runner, in which
        # case the raw subprocess fallback preserves the historical behaviour.
        self._runner = runner

    def _check_cifs_available(self) -> tuple[bool, str, str]:
        running_k = ""
        installed_k = ""
        try:
            running_k = subprocess.run(
                ["uname", "-r"],
                capture_output=True,
                text=True,
            ).stdout.strip()
        except Exception:
            pass

        mount_helper_available = shutil.which("mount.cifs") is not None
        if not mount_helper_available:
            self._cifs_unavailable_reason = "userspace"
        else:
            self._cifs_unavailable_reason = "kernel"
            try:
                probe = subprocess.run(
                    ["modprobe", "-n", "-v", "cifs"],
                    capture_output=True,
                    text=True,
                )
                if probe.returncode == 0:
                    return True, running_k, running_k
            except Exception:
                pass

        try:
            modules_dir = Path("/lib/modules")
            if modules_dir.exists():
                for kernel_dir in modules_dir.iterdir():
                    if kernel_dir.is_dir() and list(kernel_dir.glob("**/cifs.ko*")):
                        installed_k = kernel_dir.name
                        break
        except Exception:
            pass

        return False, running_k, installed_k

    def _check_target_not_mounted(self) -> None:
        try:
            self.fs_backend._get_mount_info()
            raise BackupError("E_STORAGE", f"Target mount_point '{self.mount_point}' is already mounted")
        except BackupError as e:
            if e.code == "E_STORAGE" and "not an active mount point" in e.public_message:
                return
            raise

    def _credentials_directory(self) -> Path:
        effective_uid = os.geteuid()
        if effective_uid == 0:
            run_dir = Path("/run/galera-backup")
        else:
            run_dir = Path(tempfile.gettempdir()) / f"galera-backup-{effective_uid}"

        try:
            run_dir.mkdir(mode=0o700, exist_ok=True)
            dir_stat = os.lstat(run_dir)
            if not stat.S_ISDIR(dir_stat.st_mode):
                raise BackupError("E_STORAGE", f"SMB credential path '{run_dir}' is not a directory")
            if dir_stat.st_uid != effective_uid:
                raise BackupError(
                    "E_STORAGE",
                    f"SMB credential directory '{run_dir}' is not owned by uid {effective_uid}",
                )
            if stat.S_IMODE(dir_stat.st_mode) != 0o700:
                os.chmod(run_dir, 0o700)
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError("E_STORAGE", f"Failed to prepare SMB credential directory '{run_dir}': {exc}")

        return run_dir

    def _create_credentials_file(self) -> Path:
        run_dir = self._credentials_directory()
        lines = [f"username={self.username}\n", f"password={self.password}\n"]
        if self.domain:
            lines.append(f"domain={self.domain}\n")

        fd: Optional[int] = None
        raw_path: Optional[str] = None
        try:
            fd, raw_path = tempfile.mkstemp(
                prefix=f"smb-credentials-{self.cluster_name}-",
                suffix=".key",
                dir=run_dir,
                text=True,
            )
            os.fchmod(fd, 0o600)
            credential_file = os.fdopen(fd, "w", encoding="utf-8")
            fd = None
            with credential_file:
                credential_file.write("".join(lines))
            return Path(raw_path)
        except OSError as exc:
            cleanup_failures: list[str] = []
            if fd is not None:
                try:
                    os.close(fd)
                except OSError as cleanup_exc:
                    cleanup_failures.append(f"descriptor close failed: {cleanup_exc}")
            if raw_path is not None:
                try:
                    Path(raw_path).unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    cleanup_failures.append(f"credential unlink failed: {cleanup_exc}")

            message = f"Failed to create SMB credential file: {exc}"
            if cleanup_failures:
                message += f"; cleanup also failed: {'; '.join(cleanup_failures)}"
            raise BackupError("E_STORAGE", message) from exc

    def _exec_mount(self, cmd: list[str]) -> tuple[int, str, str]:
        if self._runner is not None:
            return self._runner.run(cmd)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    def _exec_umount(self) -> tuple[int, str, str]:
        if self._runner is not None:
            return self._runner.run(["umount", str(self.mount_point)])
        proc = subprocess.run(["umount", str(self.mount_point)], capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is None:
            self.cleanup()
            return False
        self._reraise_after_cleanup(exc_val)

    def cleanup(self, raise_on_failure: bool = True) -> Optional[BackupError]:
        failures: list[str] = []

        if self._is_mounted:
            try:
                code, out, err = self._exec_umount()
                if code == 0:
                    self._is_mounted = False
                else:
                    failures.append(f"unmount failed: {err or out or f'exit {code}'}")
            except Exception as exc:
                failures.append(f"unmount failed: {exc}")

        if self._credentials_file and self._credentials_file.exists():
            try:
                self._credentials_file.unlink()
                self._credentials_file = None
            except OSError as exc:
                failures.append(f"credential cleanup failed: {exc}")
        elif self._credentials_file:
            self._credentials_file = None

        if not failures:
            return None

        failure = BackupError("E_STORAGE", f"SMB cleanup failed: {'; '.join(failures)}")
        if raise_on_failure:
            raise failure
        return failure

    def _reraise_after_cleanup(self, exc: Exception) -> None:
        cleanup_failure = self.cleanup(raise_on_failure=False)
        if cleanup_failure is None:
            raise exc

        if isinstance(exc, BackupError):
            error_code = exc.code
            error_message = exc.public_message
        else:
            error_code = "E_STORAGE"
            error_message = str(exc)
        raise BackupError(
            error_code,
            f"{error_message}; cleanup also failed: {cleanup_failure.public_message}",
        ) from exc

    def _validate_observed_mount(self, observed: dict[str, str]) -> None:
        observed_source = observed.get("source", "")
        if normalize_smb_source(observed_source) != normalize_smb_source(self.source):
            raise BackupError(
                "E_STORAGE",
                f"Observed SMB source '{observed_source}' does not match configured source '{self.source}'",
            )

        observed_options = {
            option.strip().lower()
            for option in observed.get("options", "").split(",")
            if option.strip()
        }
        required_options = {"vers=3.1.1", "seal", "nosuid", "nodev", "noexec"}
        missing_options = sorted(required_options - observed_options)
        if missing_options:
            raise BackupError(
                "E_STORAGE",
                f"Required observed mount options are missing: {', '.join(missing_options)}",
            )

    def preflight(self) -> None:
        cifs_ok, running_k, installed_k = self._check_cifs_available()
        if not cifs_ok:
            if self._cifs_unavailable_reason == "userspace":
                raise BackupError(
                    "E_CIFS_MODULE",
                    "CIFS userspace helper 'mount.cifs' is unavailable; install the "
                    "configured cifs-utils package before retrying. No mount was attempted.",
                )
            raise BackupError(
                "E_CIFS_MODULE",
                f"CIFS kernel module ('cifs.ko') is unavailable for running kernel {running_k}. "
                f"Installed kernel with CIFS: {installed_k or 'none'}. Database host reboot required to boot matching kernel."
            )

        opt_errs = validate_smb_options(self.options)
        if opt_errs:
            raise BackupError("E_STORAGE", f"SMB options validation failed: {'; '.join(opt_errs)}")

        if not re.fullmatch(r"//[^/]+/[^/]+", self.source):
            raise BackupError(
                "E_STORAGE",
                f"SMB source '{self.source}' must identify exactly one UNC share as '//server/share'",
            )

        try:
            if not self.mount_point.exists():
                if not self.mount_point.parent.is_dir():
                    raise BackupError(
                        "E_STORAGE",
                        f"SMB mount_point parent '{self.mount_point.parent}' does not exist",
                    )
                self.mount_point.mkdir(mode=0o750)
                os.chmod(self.mount_point, 0o750)
            elif not self.mount_point.is_dir():
                raise BackupError("E_STORAGE", f"SMB mount_point '{self.mount_point}' is not a directory")
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError("E_STORAGE", f"Failed to prepare SMB mount_point '{self.mount_point}': {exc}")

        self._check_target_not_mounted()
        self._credentials_file = self._create_credentials_file()

        opts_with_cred = self.options + [f"credentials={self._credentials_file}"]
        cmd = ["mount", "-t", "cifs", self.source, str(self.mount_point), "-o", ",".join(opts_with_cred)]

        try:
            code, out, err = self._exec_mount(cmd)
            if code != 0:
                detail = err or out
                if self._runner is not None:
                    detail = self._runner.redactor.redact(detail)
                raise BackupError("E_STORAGE", f"SMB mount command failed: {detail}")
            self._is_mounted = True
        except Exception as exc:
            self._reraise_after_cleanup(exc)

        try:
            self.fs_backend.preflight(self._validate_observed_mount)
        except Exception as exc:
            self._reraise_after_cleanup(exc)

    def publish(self, artifact: ArtifactSet) -> PublishedArtifact:
        try:
            return self.fs_backend.publish(artifact)
        except Exception as exc:
            self._reraise_after_cleanup(exc)

    def fetch_latest(self, work_dir: Path) -> ArtifactSet:
        try:
            return self.fs_backend.fetch_latest(work_dir)
        except Exception as exc:
            self._reraise_after_cleanup(exc)

    def prune(self, now: datetime, retention_days: int) -> int:
        try:
            return self.fs_backend.prune(now, retention_days)
        except Exception as exc:
            self._reraise_after_cleanup(exc)

    def close(self) -> None:
        self.cleanup()
