#!/usr/bin/env python3
"""
Cross-cluster backup configuration validator.
Validates cluster.yml and inventory.yml pairs against schema and cross-cluster invariants.
Usage: validate-backup-config.py <clusters-root>
"""
import sys
import json
import re
import yaml
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from jsonschema import validate, ValidationError


@dataclass
class BackupRecord:
    cluster_name: str
    pmm_cluster_name: str
    destination: str
    endpoint: str
    bucket: str
    secure: bool
    cluster_path: Path


def normalize_s3_endpoint(value: str, secure: bool) -> str:
    val = value.strip().lower()
    if val.startswith("https://"):
        val = val[8:]
    elif val.startswith("http://"):
        val = val[7:]
    val = val.rstrip("/")
    if secure:
        if val.endswith(":443"):
            val = val[:-4]
    else:
        if val.endswith(":80"):
            val = val[:-3]
    return val


def validate_cron(value: str) -> list[str]:
    errors = []
    fields = value.strip().split()
    if len(fields) != 5:
        errors.append(f"Cron schedule must have exactly 5 whitespace-separated fields, got {len(fields)}: '{value}'")
        return errors
    # Basic field syntax check
    allowed_chars = set("0123456789*,-/")
    for i, field in enumerate(fields):
        if not field or not set(field).issubset(allowed_chars):
            errors.append(f"Cron schedule field {i+1} ('{field}') contains invalid characters in '{value}'")
    return errors


def validate_smb_options(options: list[str]) -> list[str]:
    errors = []
    opt_set = {str(opt) for opt in options}

    # Forbidden options check
    for opt in options:
        opt_lower = opt.lower()
        if opt != opt_lower:
            errors.append(f"SMB option must use canonical lowercase spelling: '{opt}'")
        if opt_lower.startswith("username=") or opt_lower == "username":
            errors.append("Unsafe SMB option 'username=' specified (credentials must be stored in root-only secrets)")
        if opt_lower.startswith("password=") or opt_lower == "password":
            errors.append("Unsafe SMB option 'password=' specified (credentials must be stored in root-only secrets)")
        if opt_lower.startswith("credentials=") or opt_lower == "credentials":
            errors.append("Unsafe SMB option 'credentials=' specified")
        if opt_lower == "noseal":
            errors.append("Unsafe SMB option 'noseal' specified")
        if opt_lower.startswith("vers=1") or opt_lower.startswith("vers=2"):
            errors.append(f"Unsafe SMB dialect specified: '{opt}' (SMB 3.x required)")

    # Required options check
    required_opts = ["seal", "nosuid", "nodev", "noexec"]
    for req in required_opts:
        if req not in opt_set:
            errors.append(f"Missing required SMB option: '{req}'")

    if "vers=3.1.1" not in opt_set:
        errors.append("Missing required SMB option: 'vers=3.1.1'")

    return errors


def get_galera_hosts(inv: dict) -> set[str]:
    hosts = set()
    all_block = inv.get("all", {}) if isinstance(inv, dict) else {}
    children = all_block.get("children", {}) if isinstance(all_block, dict) else {}
    if "galera" in children and isinstance(children["galera"], dict):
        g_hosts = children["galera"].get("hosts", {})
        if isinstance(g_hosts, dict):
            hosts.update(g_hosts.keys())
        elif isinstance(g_hosts, list):
            hosts.update(g_hosts)

    top_children = inv.get("children", {}) if isinstance(inv, dict) else {}
    if "galera" in top_children and isinstance(top_children["galera"], dict):
        g_hosts = top_children["galera"].get("hosts", {})
        if isinstance(g_hosts, dict):
            hosts.update(g_hosts.keys())
        elif isinstance(g_hosts, list):
            hosts.update(g_hosts)

    if "galera" in inv and isinstance(inv["galera"], dict):
        g_hosts = inv["galera"].get("hosts", {})
        if isinstance(g_hosts, dict):
            hosts.update(g_hosts.keys())
        elif isinstance(g_hosts, list):
            hosts.update(g_hosts)

    return hosts


def validate_pair(cluster_path: Path, inventory_path: Path) -> list[str]:
    errors = []
    schema_path = cluster_path.parents[1] / "schema" / "cluster.schema.json"
    if not schema_path.exists():
        # Fall back if relative path structure is different
        schema_path = cluster_path.parent / "schema" / "cluster.schema.json"

    with open(cluster_path) as f:
        cluster = yaml.safe_load(f)

    with open(inventory_path) as f:
        inventory = yaml.safe_load(f)

    # JSON Schema check if schema file found
    if schema_path.exists():
        with open(schema_path) as f:
            schema = json.load(f)
        try:
            validate(instance=cluster, schema=schema)
        except ValidationError as e:
            errors.append(f"JSON Schema error in {cluster_path}: {e.message}")
            return errors

    backup = cluster.get("backup", {})
    env = cluster.get("cluster", {}).get("environment", "")

    # Klaster ze swiadomie wylaczonym backupem nie planuje niczego, wiec pola
    # harmonogramu niosa wtedy sentinel `disabled`, a nie wyrazenie cron.
    # Bez tego wyjatku walidator zadal poprawnego crona od klastra, ktory z
    # zalozenia nie robi kopii — i blokowal cala bramke repozytorium.
    backup_enabled = backup.get("enabled", True) is True

    # Scheduler check
    scheduler = backup.get("scheduler")
    if not scheduler:
        errors.append(f"Missing required 'backup.scheduler' block in {cluster_path}")
    else:
        scheduler_host = scheduler.get("host")
        galera_hosts = get_galera_hosts(inventory)
        if scheduler_host not in galera_hosts:
            errors.append(
                f"Scheduler host '{scheduler_host}' not in inventory group 'galera' ({sorted(galera_hosts)})"
            )

    # Cron schedule check
    if backup_enabled:
        errors.extend(validate_cron(backup.get("full_backup_schedule", "")))

    # Encryption check
    if backup.get("encryption_enabled") is not True:
        errors.append("encryption_enabled must be true")

    # Freshness SLA check. Duplikuje `minimum: 1` ze schematu swiadomie:
    # walidacja schematem wyzej jest warunkowa (`if schema_path.exists()`),
    # wiec bez tego kontrola znika, gdy root nie zawiera schema/.
    sla = backup.get("freshness_sla_hours")
    if not isinstance(sla, int) or isinstance(sla, bool) or sla < 1:
        errors.append(f"freshness_sla_hours must be a positive integer, got '{sla}'")

    dest = backup.get("destination")
    s3_block = backup.get("s3")
    smb_block = backup.get("smb")
    fs_block = backup.get("filesystem")

    # Destination block isolation and mixed blocks check
    if dest == "s3":
        if smb_block or fs_block:
            errors.append("Mixed destination blocks: destination is 's3' but 'smb' or 'filesystem' block is present")
        if not s3_block:
            errors.append("Destination is 's3' but missing required 's3' block")
        else:
            secure = s3_block.get("secure", False)
            if env == "production" and not secure:
                errors.append("S3 backup in production environment requires secure=true")
    elif dest == "smb":
        if s3_block or fs_block:
            errors.append("Mixed destination blocks: destination is 'smb' but 's3' or 'filesystem' block is present")
        if not smb_block:
            errors.append("Destination is 'smb' but missing required 'smb' block")
        else:
            source = str(smb_block.get("source", ""))
            if not re.fullmatch(r"//[^/]+/[^/]+", source):
                errors.append(
                    f"SMB source must identify exactly one UNC share as '//server/share', got '{source}'"
                )
            mount_point = smb_block.get("mount_point", "")
            if not mount_point.startswith("/"):
                errors.append(f"SMB mount_point must be an absolute path, got '{mount_point}'")
            options = smb_block.get("options", [])
            errors.extend(validate_smb_options(options))
    elif dest == "filesystem":
        if s3_block or smb_block:
            errors.append("Mixed destination blocks: destination is 'filesystem' but 's3' or 'smb' block is present")
        if not fs_block:
            errors.append("Destination is 'filesystem' but missing required 'filesystem' block")
        else:
            mount_point = fs_block.get("mount_point", "")
            if not mount_point.startswith("/"):
                errors.append(f"Filesystem mount_point must be an absolute path, got '{mount_point}'")

    return errors


def parse_backup_record(cluster_path: Path) -> Optional[BackupRecord]:
    with open(cluster_path) as f:
        cluster = yaml.safe_load(f)
    name = cluster.get("cluster", {}).get("name", "")
    pmm_name = cluster.get("monitoring", {}).get("pmm", {}).get("cluster_name", "")
    backup = cluster.get("backup", {})
    dest = backup.get("destination", "")
    s3_block = backup.get("s3", {})
    endpoint = s3_block.get("endpoint", "") if s3_block else ""
    bucket = s3_block.get("bucket", "") if s3_block else ""
    secure = s3_block.get("secure", False) if s3_block else False

    return BackupRecord(
        cluster_name=name,
        pmm_cluster_name=pmm_name,
        destination=dest,
        endpoint=endpoint,
        bucket=bucket,
        secure=secure,
        cluster_path=cluster_path,
    )


def validate_unique_s3_owners(records: list[BackupRecord]) -> list[str]:
    errors = []
    # Check duplicate normalized S3 endpoint + bucket
    s3_map: dict[tuple[str, str], list[str]] = {}
    for r in records:
        if r.destination == "s3" and r.endpoint and r.bucket:
            norm_ep = normalize_s3_endpoint(r.endpoint, r.secure)
            key = (norm_ep, r.bucket)
            s3_map.setdefault(key, []).append(r.cluster_name)

    for (ep, bucket), clusters in s3_map.items():
        if len(clusters) > 1:
            errors.append(
                f"duplicate S3 ownership: endpoint='{ep}' bucket='{bucket}' shared by clusters {clusters}"
            )

    # Check duplicate cluster names
    name_map: dict[str, list[str]] = {}
    for r in records:
        if r.cluster_name:
            name_map.setdefault(r.cluster_name, []).append(str(r.cluster_path))
    for cname, paths in name_map.items():
        if len(paths) > 1:
            errors.append(f"duplicate cluster name '{cname}' in paths {paths}")

    # Check duplicate PMM cluster names
    pmm_map: dict[str, list[str]] = {}
    for r in records:
        if r.pmm_cluster_name:
            pmm_map.setdefault(r.pmm_cluster_name, []).append(r.cluster_name)
    for pmm_name, clusters in pmm_map.items():
        if len(clusters) > 1:
            errors.append(f"duplicate PMM cluster name '{pmm_name}' shared by clusters {clusters}")

    return errors


def main():
    clusters_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("clusters")
    if not clusters_root.exists():
        print(f"FAIL: Clusters root directory does not exist: {clusters_root}", file=sys.stderr)
        return 1

    cluster_dirs = []
    for p in clusters_root.iterdir():
        if p.is_dir() and (p / "cluster.yml").exists() and (p / "inventory.yml").exists():
            cluster_dirs.append(p)

    if not cluster_dirs:
        # Fail-closed: pusta lista katalogow klastrow to zle wskazane drzewo,
        # a nie "repo bez problemow" — walidator nie mial czego sprawdzic.
        print(
            f"FAIL: No cluster directories containing cluster.yml and inventory.yml found in {clusters_root}",
            file=sys.stderr,
        )
        return 1

    all_errors = []
    records = []
    for cdir in sorted(cluster_dirs):
        cpath = cdir / "cluster.yml"
        ipath = cdir / "inventory.yml"
        errs = validate_pair(cpath, ipath)
        if errs:
            all_errors.extend(errs)
        else:
            print(f"OK: {cdir.name} backup configuration is valid")
        rec = parse_backup_record(cpath)
        if rec:
            records.append(rec)

    unique_errs = validate_unique_s3_owners(records)
    all_errors.extend(unique_errs)

    if all_errors:
        print("FAIL: Backup configuration validation errors:", file=sys.stderr)
        for e in all_errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("OK: Cross-cluster S3 ownership and logical names are unique")
    return 0


if __name__ == "__main__":
    sys.exit(main())
