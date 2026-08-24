"""Modele konfiguracji uruchomienia i wczytywanie sekretow z pliku env."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BackupError

@dataclass(frozen=True)
class Paths:
    install_root: Path
    cluster_dir: Path
    staging_root: Path
    datadir: Path
    socket: Path
    metric_file: Path

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Paths:
        return cls(
            install_root=Path(data["install_root"]),
            cluster_dir=Path(data["cluster_dir"]),
            staging_root=Path(data["staging_root"]),
            datadir=Path(data["datadir"]),
            socket=Path(data["socket"]),
            metric_file=Path(data["metric_file"]),
        )


@dataclass(frozen=True)
class RunConfig:
    cluster_name: str
    metric_cluster_label: str
    local_role: str
    scheduler_system_hostname: str
    scheduler_system_address: str
    galera_nodes_expected: int
    galera_nodes: list[str]
    mariadb_version: str
    retention_days: int
    flow_control_threshold_ns: int
    proxysql: dict[str, Any]
    backend: dict[str, Any]
    paths: Paths


def load_run_config(config_path: Path, expected_cluster_name: str) -> RunConfig:
    if not config_path.exists():
        raise BackupError("E_CONFIG", f"Configuration file not found: {config_path}")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise BackupError("E_CONFIG", f"Malformed JSON in config file {config_path}: {exc}")

    if data.get("format_version") != 1:
        raise BackupError("E_CONFIG", f"Unsupported format_version {data.get('format_version')} in {config_path}")

    cluster_name = data.get("cluster_name", "")
    if cluster_name != expected_cluster_name:
        raise BackupError(
            "E_CONFIG",
            f"Config cluster_name '{cluster_name}' does not match expected '{expected_cluster_name}'"
        )

    try:
        paths = Paths.from_dict(data["paths"])
        return RunConfig(
            cluster_name=cluster_name,
            metric_cluster_label=data.get("metric_cluster_label", cluster_name),
            local_role=data.get("local_role", "scheduler"),
            scheduler_system_hostname=data.get("scheduler_system_hostname", ""),
            scheduler_system_address=data.get("scheduler_system_address", ""),
            galera_nodes_expected=int(data.get("galera_nodes_expected", 3)),
            galera_nodes=list(data.get("galera_nodes", [])),
            mariadb_version=str(data.get("mariadb_version", "")),
            retention_days=int(data.get("retention_days", 14)),
            flow_control_threshold_ns=int(data.get("flow_control_threshold_ns", 1000000000)),
            proxysql=data.get("proxysql", {}),
            backend=data.get("backend", {}),
            paths=paths,
        )
    except KeyError as e:
        raise BackupError("E_CONFIG", f"Missing required configuration key in {config_path}: {e}")


def load_secrets(
    env_path: Path,
    backend_type: str,
    enforce_permissions: bool = True,
    require_writer_credentials: bool = False,
) -> dict[str, str]:
    if not env_path.exists():
        raise BackupError("E_SECRETS", f"Secrets file not found: {env_path}")

    if enforce_permissions:
        st = env_path.stat()
        if (st.st_mode & 0o077) != 0:
            raise BackupError(
                "E_SECRETS_PERM",
                f"Secrets file {env_path} has unsafe permissions ({oct(st.st_mode & 0o777)}); must be mode 0600"
            )

    secrets: dict[str, str] = {}
    with open(env_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise BackupError("E_SECRETS", f"Malformed line {line_num} in {env_path}")
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            secrets[k] = v

    if "GALERA_BACKUP_ENCRYPTION_KEY" not in secrets or not secrets["GALERA_BACKUP_ENCRYPTION_KEY"]:
        raise BackupError("E_SECRETS", f"Missing required GALERA_BACKUP_ENCRYPTION_KEY in {env_path}")

    if backend_type == "s3":
        if "GALERA_BACKUP_S3_ACCESS_KEY" not in secrets or not secrets["GALERA_BACKUP_S3_ACCESS_KEY"]:
            raise BackupError("E_SECRETS", f"Missing required GALERA_BACKUP_S3_ACCESS_KEY in {env_path}")
        if "GALERA_BACKUP_S3_SECRET_KEY" not in secrets or not secrets["GALERA_BACKUP_S3_SECRET_KEY"]:
            raise BackupError("E_SECRETS", f"Missing required GALERA_BACKUP_S3_SECRET_KEY in {env_path}")
    elif backend_type == "smb":
        if "GALERA_BACKUP_SMB_USERNAME" not in secrets or not secrets["GALERA_BACKUP_SMB_USERNAME"]:
            raise BackupError("E_SECRETS", f"Missing required GALERA_BACKUP_SMB_USERNAME in {env_path}")
        if "GALERA_BACKUP_SMB_PASSWORD" not in secrets or not secrets["GALERA_BACKUP_SMB_PASSWORD"]:
            raise BackupError("E_SECRETS", f"Missing required GALERA_BACKUP_SMB_PASSWORD in {env_path}")

    if require_writer_credentials:
        if not secrets.get("GALERA_BACKUP_PROXYSQL_STATS_USER"):
            raise BackupError("E_SECRETS", f"Missing required GALERA_BACKUP_PROXYSQL_STATS_USER in {env_path}")
        if not secrets.get("GALERA_BACKUP_PROXYSQL_STATS_PASSWORD"):
            raise BackupError("E_SECRETS", f"Missing required GALERA_BACKUP_PROXYSQL_STATS_PASSWORD in {env_path}")

    return secrets
