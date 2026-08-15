#!/usr/bin/env python3
"""Live probe for Galera Backup storage backends (SMBBackend and FilesystemBackend).

Resolves the isolated restore host from the selected cluster inventory and
supports preflight, publication, restore, and failure-path verification.

Usage:
    Local controller mode:
        CLUSTER=<name> python3 tests/live/probe-galera-backup-backends.py --mode preflight

    Direct on-node execution:
        python3 tests/live/probe-galera-backup-backends.py --cluster <name> --on-node --mode preflight
        python3 tests/live/probe-galera-backup-backends.py --cluster <name> --on-node --mode full
"""

import argparse
import importlib.machinery
import importlib.util
import json
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import yaml
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

RUNNER_PATH = Path("/opt/galera-backup/galera-backup")


def resolve_remote_connection(
    cluster: str,
    inventory_path: Optional[str] = None,
) -> Dict[str, Any]:
    inventory_file = Path(
        inventory_path or f"clusters/{cluster}/inventory.yml"
    )
    try:
        inventory = yaml.safe_load(inventory_file.read_text(encoding="utf-8"))
        all_group = inventory["all"]
        restore_hosts = all_group["children"]["restore"]["hosts"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise RuntimeError(
            f"Cannot resolve restore connection from {inventory_file}: {exc}"
        ) from exc

    if len(restore_hosts) != 1:
        raise RuntimeError(
            f"Inventory {inventory_file} must define exactly one restore host"
        )

    host_name, raw_host_vars = next(iter(restore_hosts.items()))
    host_vars = raw_host_vars or {}
    all_vars = all_group.get("vars", {}) or {}
    host = (
        host_vars.get("ansible_host")
        or host_vars.get("restore_node_address")
        or host_name
    )
    return {
        "host": str(host),
        "port": int(host_vars.get("ansible_port", all_vars.get("ansible_port", 22))),
        "user": str(host_vars.get("ansible_user", all_vars.get("ansible_user", "root"))),
        "ssh_key": host_vars.get(
            "ansible_ssh_private_key_file",
            all_vars.get("ansible_ssh_private_key_file"),
        ),
        "known_hosts": str(inventory_file.parent / "known_hosts"),
    }


def load_installed_runner(path: Path = RUNNER_PATH):
    """Zaimportuj wdrozony runner jako modul pakietu.

    Wczesniej ta funkcja czytala `path` przez `SourceFileLoader`, bo caly runner
    byl jednym plikiem. Po dekompozycji `/opt/galera-backup/galera-backup` jest
    21-liniowym wrapperem eksponujacym wylacznie `main`, wiec ladowanie go dalej
    dawaloby `AttributeError` na `mod.SMBBackend`, `mod.run_restore` i reszcie
    API, ktorego ta sonda uzywa. API mieszka teraz w `galera_backup.pipeline`,
    ktory lezy w tym samym katalogu co wrapper.
    """
    if not path.exists():
        raise RuntimeError(f"Installed runner not found at {path}")
    package_root = str(path.resolve().parent)
    if package_root not in sys.path:
        sys.path.insert(0, package_root)
    if importlib.util.find_spec("galera_backup") is None:
        raise ImportError(f"Cannot import package 'galera_backup' from {package_root}")
    return importlib.import_module("galera_backup.pipeline")


def parse_cifs_diagnostic(msg: str) -> Dict[str, str]:
    """Parse running and installed kernel versions from E_CIFS_MODULE public message."""
    res = {}
    m_running = re.search(r"running kernel\s+([^\s\.]+[\w\.\-]+)", msg)
    if m_running:
        res["running_kernel"] = m_running.group(1).rstrip(".")
    m_installed = re.search(r"Installed kernel with CIFS:\s+([^\s\.]+[\w\.\-]+)", msg)
    if m_installed:
        res["installed_kernel"] = m_installed.group(1).rstrip(".")
    return res


def verify_no_mount_performed(smb_backend) -> bool:
    """Verify credentials file was not left over and no mount state is set."""
    cred_ok = smb_backend._credentials_file is None or not smb_backend._credentials_file.exists()
    mounted_ok = not getattr(smb_backend, "_is_mounted", False)
    return cred_ok and mounted_ok

def verify_backend_round_trip(
    backend,
    artifact,
    fetch_dir: Path,
    retention_days: int,
) -> Dict[str, Any]:
    """Publish and read back one real encrypted artifact through a backend."""
    if not artifact.payload_path.is_file() or artifact.payload_path.stat().st_size == 0:
        raise RuntimeError("Source encrypted payload is missing or empty")
    if not artifact.checksum_path.is_file() or not artifact.metadata_path.is_file():
        raise RuntimeError("Source checksum or metadata is missing")

    metadata = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))
    expected_sha = metadata.get("encrypted_sha256") or metadata.get("sha256_encrypted")
    expected_size = metadata.get("encrypted_size_bytes") or metadata.get("size_bytes")
    if not isinstance(expected_sha, str) or not expected_sha:
        raise RuntimeError("Metadata has no encrypted SHA-256")
    if not isinstance(expected_size, int) or expected_size <= 0:
        raise RuntimeError("Metadata has no positive encrypted size")
    if not metadata.get("plaintext_sha256") and not metadata.get("sha256_plaintext"):
        raise RuntimeError("Metadata has no plaintext SHA-256")

    source_bytes = artifact.payload_path.read_bytes()
    if len(source_bytes) != expected_size:
        raise RuntimeError("Source encrypted size does not match metadata")
    if hashlib.sha256(source_bytes).hexdigest() != expected_sha:
        raise RuntimeError("Source encrypted SHA-256 does not match metadata")
    checksum_value = artifact.checksum_path.read_text(encoding="utf-8").split()[0]
    if checksum_value != expected_sha:
        raise RuntimeError("Source checksum file does not match metadata")

    backend.preflight()
    published = backend.publish(artifact)
    if published.backup_name != artifact.backup_name:
        raise RuntimeError("Published backup name changed")
    if published.encrypted_sha256 != expected_sha or published.encrypted_size != expected_size:
        raise RuntimeError("Published artifact identity does not match source metadata")

    fetched = backend.fetch_latest(fetch_dir)
    if fetched.backup_name != artifact.backup_name:
        raise RuntimeError("Fetched backup is not the artifact just published")
    fetched_bytes = fetched.payload_path.read_bytes()
    if len(fetched_bytes) != expected_size:
        raise RuntimeError("Fetched encrypted size does not match metadata")
    if hashlib.sha256(fetched_bytes).hexdigest() != expected_sha:
        raise RuntimeError("Fetched encrypted SHA-256 does not match metadata")
    fetched_checksum = fetched.checksum_path.read_text(encoding="utf-8").split()[0]
    if fetched_checksum != expected_sha:
        raise RuntimeError("Fetched checksum file does not match metadata")
    fetched_metadata = json.loads(fetched.metadata_path.read_text(encoding="utf-8"))
    if fetched_metadata.get("cluster_name") != metadata.get("cluster_name"):
        raise RuntimeError("Fetched metadata cluster does not match source metadata")

    backend.prune(datetime.now(timezone.utc), max(retention_days, 36500))
    return {
        "backup_name": artifact.backup_name,
        "encrypted_sha256": expected_sha,
        "encrypted_size_bytes": expected_size,
    }


def run_on_node_preflight(cluster_name: str = "claude-r10b") -> Tuple[bool, Dict[str, Any]]:
    """Exercise the production CIFS preflight path without attempting a network mount."""
    mod = load_installed_runner()
    smb = mod.SMBBackend(
        source="//invalid/preflight-only",
        mount_point="/mnt/galera-backup-preflight-only",
        options=["vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
        username="preflight-only",
        password="password",
        domain=None,
        cluster_name=cluster_name,
    )
    details: Dict[str, Any] = {
        "cifs_module_error": False,
        "error_code": None,
        "message": None,
        "running_kernel": None,
        "installed_kernel": None,
        "no_mount_performed": False,
    }
    try:
        cifs_ok, running_kernel, installed_kernel = smb._check_cifs_available()
        details["running_kernel"] = running_kernel
        details["installed_kernel"] = installed_kernel
        if cifs_ok:
            details["no_mount_performed"] = verify_no_mount_performed(smb)
            return bool(details["no_mount_performed"]), details

        try:
            smb.preflight()
        except mod.BackupError as exc:
            details["error_code"] = exc.code
            details["message"] = exc.public_message
            details.update(parse_cifs_diagnostic(exc.public_message))
            details["cifs_module_error"] = exc.code == "E_CIFS_MODULE"
            details["no_mount_performed"] = verify_no_mount_performed(smb)
            return bool(details["cifs_module_error"] and details["no_mount_performed"]), details
        else:
            smb.close()
            details["message"] = "CIFS preflight unexpectedly succeeded after the prerequisite check failed"
            details["no_mount_performed"] = verify_no_mount_performed(smb)
            return False, details
    except Exception as exc:
        details["error_code"] = getattr(exc, "code", "EXCEPTION")
        details["message"] = str(exc)
        details["no_mount_performed"] = verify_no_mount_performed(smb)
        return False, details


def read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def download_real_s3_artifact(mod, cluster_name: str, target_dir: Path):
    config_file = Path(f"/opt/galera-backup/clusters/{cluster_name}/config.json")
    secrets_file = Path(f"/opt/galera-backup/clusters/{cluster_name}/secrets.env")
    if not config_file.is_file() or not secrets_file.is_file():
        raise RuntimeError("Installed backup configuration or secrets file is missing")

    cfg_data = json.loads(config_file.read_text(encoding="utf-8"))
    backend_cfg = cfg_data.get("backend", {})
    if backend_cfg.get("type") != "s3":
        raise RuntimeError("Installed source backend is not S3")
    secrets = read_env_file(secrets_file)
    access_key = secrets.get("GALERA_BACKUP_S3_ACCESS_KEY")
    secret_key = secrets.get("GALERA_BACKUP_S3_SECRET_KEY")
    if not access_key or not secret_key:
        raise RuntimeError("Installed S3 credentials are missing")

    from minio import Minio

    bucket = backend_cfg["bucket"]
    client = Minio(
        backend_cfg["endpoint"],
        access_key=access_key,
        secret_key=secret_key,
        secure=bool(backend_cfg.get("secure", True)),
    )
    object_prefix = f"galera-{cluster_name}-"
    metadata_objects = sorted(
        obj.object_name
        for obj in client.list_objects(bucket, prefix=object_prefix, recursive=True)
        if obj.object_name.endswith("/metadata.json")
    )
    if not metadata_objects:
        raise RuntimeError(f"No real backup artifact exists in configured bucket '{bucket}'")

    storage_prefix = metadata_objects[-1].rsplit("/", 1)[0]
    backup_name = storage_prefix.rsplit("/", 1)[-1]
    payload_path = target_dir / "backup.tar.enc"
    checksum_path = target_dir / "backup.sha256"
    metadata_path = target_dir / "metadata.json"
    client.fget_object(bucket, f"{storage_prefix}/backup.tar.enc", str(payload_path))
    client.fget_object(bucket, f"{storage_prefix}/backup.sha256", str(checksum_path))
    client.fget_object(bucket, f"{storage_prefix}/metadata.json", str(metadata_path))

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("cluster_name") != cluster_name:
        raise RuntimeError("Downloaded S3 metadata belongs to another cluster")
    return mod.ArtifactSet(
        backup_name=backup_name,
        payload_path=payload_path,
        checksum_path=checksum_path,
        metadata_path=metadata_path,
    )


def run_backend_restore(
    mod,
    cluster_name: str,
    backend_type: str,
    source: Optional[str],
    mount_point: Path,
    expected_fstype: str,
    smb_secrets_path: Path,
) -> Dict[str, Any]:
    """Restore through a temporary backend config without replacing production config."""
    production_dir = Path(f"/opt/galera-backup/clusters/{cluster_name}")
    production_config = json.loads(
        (production_dir / "config.json").read_text(encoding="utf-8")
    )
    production_secrets = read_env_file(production_dir / "secrets.env")
    encryption_key = production_secrets.get("GALERA_BACKUP_ENCRYPTION_KEY")
    if not encryption_key:
        raise RuntimeError("Installed encryption key is missing")

    if backend_type == "smb":
        if not source:
            raise RuntimeError("--source is required for managed SMB restore")
        smb_secrets = read_env_file(smb_secrets_path)
        username = smb_secrets.get("GALERA_BACKUP_TEST_SMB_USERNAME")
        password = smb_secrets.get("GALERA_BACKUP_TEST_SMB_PASSWORD")
        if not username or not password:
            raise RuntimeError("Temporary SMB credentials are missing")
        backend_config = {
            "type": "smb",
            "source": source,
            "mount_point": str(mount_point),
            "options": ["vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
        }
        secret_values = {
            "GALERA_BACKUP_ENCRYPTION_KEY": encryption_key,
            "GALERA_BACKUP_SMB_USERNAME": username,
            "GALERA_BACKUP_SMB_PASSWORD": password,
        }
    else:
        backend_config = {
            "type": "filesystem",
            "mount_point": str(mount_point),
            "expected_fstype": expected_fstype,
        }
        secret_values = {"GALERA_BACKUP_ENCRYPTION_KEY": encryption_key}

    temp_root = Path(tempfile.mkdtemp(prefix=f"restore-{backend_type}-proof-"))
    os.chmod(temp_root, 0o711)
    secrets_path = temp_root / "secrets.env"
    try:
        test_config = json.loads(json.dumps(production_config))
        test_config["backend"] = backend_config
        test_config["local_role"] = "restore"
        test_config["paths"]["cluster_dir"] = str(temp_root / "cluster")
        test_config["paths"]["staging_root"] = str(temp_root / "staging")
        test_config["paths"]["metric_file"] = str(temp_root / "metrics.prom")
        config_path = temp_root / "config.json"
        config_fd = os.open(
            str(config_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o640,
        )
        with os.fdopen(config_fd, "w", encoding="utf-8") as config_file:
            config_file.write(json.dumps(test_config, indent=2))
        secrets_fd = os.open(
            str(secrets_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(secrets_fd, "w", encoding="utf-8") as secrets_file:
            secrets_file.write(
                "".join(f"{key}={value}\n" for key, value in secret_values.items())
            )
        os.chmod(secrets_path, 0o600)

        mod.run_restore(
            config_path=config_path,
            secrets_path=secrets_path,
            cluster_name=cluster_name,
            confirm=True,
        )
        state = json.loads((temp_root / "cluster" / "state.json").read_text(encoding="utf-8"))
        success = state.get("last_success", {})
        artifact = success.get("artifact", {})
        if success.get("command") != "restore" or not artifact.get("rows_verified"):
            raise RuntimeError("Restore completed without a successful verification state")

        if backend_type == "smb":
            mounted = subprocess.run(
                ["findmnt", "--mountpoint", str(mount_point)],
                capture_output=True,
                text=True,
            )
            credential_glob = Path("/run/galera-backup").glob(
                f"smb-credentials-{cluster_name}-*.key"
            )
            if mounted.returncode == 0 or any(credential_glob):
                raise RuntimeError("Managed SMB restore left mount or credential residue")
        else:
            mounted = subprocess.run(
                ["findmnt", "--mountpoint", str(mount_point)],
                capture_output=True,
                text=True,
            )
            if mounted.returncode != 0:
                raise RuntimeError("Pre-mounted filesystem disappeared during restore")

        return {
            "backend": backend_type,
            "backup_name": artifact.get("backup_name"),
            "rows_verified": artifact.get("rows_verified"),
        }
    finally:
        cleanup_failures = []
        try:
            secrets_path.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_failures.append(f"temporary secrets cleanup failed: {exc}")
        try:
            shutil.rmtree(temp_root)
        except OSError as exc:
            cleanup_failures.append(f"temporary restore directory cleanup failed: {exc}")
        if cleanup_failures:
            cleanup_message = "; ".join(cleanup_failures)
            active_error = sys.exc_info()[1]
            if active_error is not None:
                raise RuntimeError(f"{active_error}; cleanup also failed: {cleanup_message}") from active_error
            raise RuntimeError(cleanup_message)


def run_on_node_full(
    cluster_name: str,
    mode: str,
    source: Optional[str] = None,
    mount_point: Optional[str] = None,
    expected_fstype: str = "cifs",
    smb_secrets_path: Path = Path("/run/galera-backup-live-smb.env"),
) -> Tuple[bool, str]:
    """Exercise a real backend contract using the newest production S3 artifact."""
    mod = load_installed_runner()
    target = Path(mount_point).resolve() if mount_point else None

    if mode in {"smb-restore", "filesystem-restore"}:
        if target is None:
            return False, "--mount-point is required"
        try:
            result = run_backend_restore(
                mod=mod,
                cluster_name=cluster_name,
                backend_type="smb" if mode == "smb-restore" else "filesystem",
                source=source,
                mount_point=target,
                expected_fstype=expected_fstype,
                smb_secrets_path=smb_secrets_path,
            )
            return True, json.dumps(result, sort_keys=True)
        except Exception as exc:
            return False, str(exc)

    if mode == "mount-loss":
        if target is None:
            return False, "--mount-point is required"
        target.mkdir(parents=True, exist_ok=True)
        before = sorted(str(path.relative_to(target)) for path in target.rglob("*"))
        backend = mod.FilesystemBackend(target, expected_fstype, cluster_name)
        try:
            backend.preflight()
            return False, "Unmounted path unexpectedly passed filesystem preflight"
        except mod.BackupError as exc:
            after = sorted(str(path.relative_to(target)) for path in target.rglob("*"))
            if exc.code != "E_STORAGE" or before != after:
                return False, f"Mount-loss fail-closed check failed with {exc.code}"
            return True, "Mount loss rejected before any storage mutation"

    if mode == "foreign-owner":
        if target is None:
            return False, "--mount-point is required"
        backend = mod.FilesystemBackend(target, expected_fstype, cluster_name)
        try:
            backend.preflight()
            return False, "Foreign owner marker unexpectedly passed preflight"
        except mod.BackupError as exc:
            if exc.code != "E_OWNER_CONFLICT":
                return False, f"Foreign owner marker returned {exc.code}, not E_OWNER_CONFLICT"
            return True, "Foreign owner marker rejected before publication"

    if mode in {"smb", "wrong-password"}:
        if not source or target is None:
            return False, "--source and --mount-point are required"
        if not smb_secrets_path.is_file():
            return False, f"SMB secrets file is missing: {smb_secrets_path}"
        smb_secrets = read_env_file(smb_secrets_path)
        username = smb_secrets.get("GALERA_BACKUP_TEST_SMB_USERNAME")
        password = smb_secrets.get("GALERA_BACKUP_TEST_SMB_PASSWORD")
        if not username or not password:
            return False, "Temporary SMB username or password is missing"
        backend = mod.SMBBackend(
            source=source,
            mount_point=target,
            options=["vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
            username=username,
            password=password if mode == "smb" else f"{password}-deliberately-wrong",
            domain=None,
            cluster_name=cluster_name,
        )
        if mode == "wrong-password":
            failure = None
            try:
                backend.preflight()
            except mod.BackupError as exc:
                if exc.code != "E_STORAGE" or not verify_no_mount_performed(backend):
                    failure = f"Wrong-password preflight was not fail-closed ({exc.code})"
            else:
                failure = "Wrong SMB password unexpectedly mounted the share"

            try:
                backend.close()
            except Exception as exc:
                failure = f"{failure + '; ' if failure else ''}SMB cleanup failed: {exc}"
            if not verify_no_mount_performed(backend):
                failure = f"{failure + '; ' if failure else ''}SMB backend left mount or credential state behind"
            if failure:
                return False, failure
            return True, "Wrong SMB password rejected without mount or credential residue"

    elif mode == "filesystem":
        if target is None:
            return False, "--mount-point is required"
        backend = mod.FilesystemBackend(target, expected_fstype, cluster_name)
    elif mode == "full":
        backend = None
    else:
        return False, f"Unsupported full-probe mode: {mode}"

    tmp_dir = Path(tempfile.mkdtemp(prefix="probe-real-artifact-"))
    result: Optional[Dict[str, Any]] = None
    failure: Optional[str] = None
    try:
        artifact = download_real_s3_artifact(mod, cluster_name, tmp_dir)
        if mode == "full":
            metadata = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))
            result = {
                "backup_name": artifact.backup_name,
                "encrypted_sha256": metadata.get("encrypted_sha256"),
                "encrypted_size_bytes": artifact.payload_path.stat().st_size,
            }
        else:
            result = verify_backend_round_trip(
                backend=backend,
                artifact=artifact,
                fetch_dir=tmp_dir / "fetched",
                retention_days=14,
            )
    except Exception as exc:
        failure = str(exc)

    if mode == "smb":
        try:
            backend.close()
        except Exception as exc:
            failure = f"SMB cleanup failed: {exc}"
        if failure is None and not verify_no_mount_performed(backend):
            failure = "SMB backend left mount or credential state behind"
    elif mode == "filesystem":
        mounted = subprocess.run(
            ["findmnt", "--mountpoint", str(target)],
            capture_output=True,
            text=True,
        )
        if mounted.returncode != 0:
            failure = "Pre-mounted filesystem disappeared during backend round trip"

    shutil.rmtree(tmp_dir, ignore_errors=True)
    if failure:
        return False, failure
    return True, json.dumps(result, sort_keys=True)


def run_remote(
    mode: str,
    cluster: str,
    host: str,
    port: int,
    user: str,
    ssh_key: Optional[str],
    known_hosts: str,
    source: Optional[str],
    mount_point: Optional[str],
    expected_fstype: str,
    smb_secrets: str,
) -> int:
    """Stream this probe to the inventory restore host and execute it there."""
    self_code = Path(__file__).read_text(encoding="utf-8")
    remote_args = [
        "python3",
        "-",
        "--on-node",
        "--mode",
        mode,
        "--cluster",
        cluster,
        "--expected-fstype",
        expected_fstype,
        "--smb-secrets",
        smb_secrets,
    ]
    if source:
        remote_args.extend(["--source", source])
    if mount_point:
        remote_args.extend(["--mount-point", mount_point])
    if user != "root":
        remote_args = ["sudo", "-n", *remote_args]
    remote_command = " ".join(shlex.quote(arg) for arg in remote_args)

    cmd = ["ssh"]
    if ssh_key:
        cmd.extend(["-i", str(ssh_key)])
    cmd.extend(
        [
            "-p",
            str(port),
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            f"{user}@{host}",
            remote_command,
        ]
    )
    proc = subprocess.run(cmd, input=self_code, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


def main():
    parser = argparse.ArgumentParser(description="Live probe for Galera Backup storage backends")
    parser.add_argument(
        "--mode",
        choices=[
            "preflight",
            "full",
            "smb",
            "filesystem",
            "smb-restore",
            "filesystem-restore",
            "wrong-password",
            "mount-loss",
            "foreign-owner",
        ],
        default="preflight",
    )
    parser.add_argument("--cluster", default=os.environ.get("CLUSTER"))
    parser.add_argument("--source")
    parser.add_argument("--mount-point")
    parser.add_argument("--expected-fstype", default="cifs")
    parser.add_argument("--smb-secrets", default="/run/galera-backup-live-smb.env")
    parser.add_argument("--on-node", action="store_true", help="Execute directly on node")
    parser.add_argument("--inventory")
    parser.add_argument("--host")
    parser.add_argument("--ssh-port", type=int)
    parser.add_argument("--ssh-user")
    parser.add_argument("--ssh-key")
    parser.add_argument("--known-hosts")

    args = parser.parse_args()
    if not args.cluster:
        parser.error("--cluster or CLUSTER is required")

    if not args.on_node:
        try:
            connection = resolve_remote_connection(args.cluster, args.inventory)
        except RuntimeError as exc:
            parser.error(str(exc))
        return run_remote(
            args.mode,
            args.cluster,
            args.host or connection["host"],
            args.ssh_port or connection["port"],
            args.ssh_user or connection["user"],
            args.ssh_key or connection["ssh_key"],
            args.known_hosts or connection["known_hosts"],
            args.source,
            args.mount_point,
            args.expected_fstype,
            args.smb_secrets,
        )

    if args.mode == "preflight":
        ok, details = run_on_node_preflight(args.cluster)
        if ok and details.get("cifs_module_error"):
            print("PASS: Pre-reboot SMB preflight diagnostic verified cleanly")
            print(f"  - Error Code: {details.get('error_code')}")
            print(f"  - Message: {details.get('message')}")
            print(f"  - Running Kernel: {details.get('running_kernel')}")
            print(f"  - Installed Kernel: {details.get('installed_kernel')}")
            print(f"  - No Mount Attempted: {details.get('no_mount_performed')}")
            return 0
        if ok:
            print("PASS: SMB/CIFS runtime prerequisites are available")
            return 0
        print("FAIL: Preflight probe encountered error:", details)
        return 1

    ok, message = run_on_node_full(
        cluster_name=args.cluster,
        mode=args.mode,
        source=args.source,
        mount_point=args.mount_point,
        expected_fstype=args.expected_fstype,
        smb_secrets_path=Path(args.smb_secrets),
    )
    print(f"{'PASS' if ok else 'FAIL'}: {message}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
