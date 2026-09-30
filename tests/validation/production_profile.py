"""Required production policy over the existing cluster/platform declarations."""

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft7Validator, FormatChecker, SchemaError

from production_backup_host import normalized_address, off_host_errors

PROFILE = Path(__file__).resolve().parents[2] / "profiles" / "production.yml"


def group_hosts(inventory, target):
    hosts = {}

    def visit(node, selected=False):
        if not isinstance(node, dict):
            return
        if selected and isinstance(node.get("hosts"), dict):
            hosts.update(node["hosts"])
        for group, child in (node.get("children") or {}).items():
            visit(child, selected or group == target)

    visit(inventory.get("all", inventory) if isinstance(inventory, dict) else {})
    return hosts


def database_identities(inventory):
    hosts = group_hosts(inventory, "galera")
    addresses = {
        str(variables[key])
        for variables in hosts.values() if isinstance(variables, dict)
        for key in ("ansible_host", "galera_node_address") if variables.get(key)
    }
    return set(hosts), addresses


def production_errors(configuration, kind, inventory=None):
    if kind not in ("cluster", "platform"):
        raise ValueError("production policy kind must be cluster or platform")
    if (configuration.get(kind) or {}).get("environment") != "production":
        return []
    try:
        policy = yaml.safe_load(PROFILE.read_text(encoding="utf-8"))[kind]
        Draft7Validator.check_schema(policy)
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError, SchemaError):
        return ["required production profile is missing or invalid"]
    errors = [
        f"production {kind} {'/'.join(str(part) for part in error.absolute_path) or '(root)'} "
        f"violates required profile constraint {error.validator}"
        for error in Draft7Validator(policy, format_checker=FormatChecker()).iter_errors(configuration)
    ]
    monitoring = configuration.get("monitoring") or {}
    email = str((monitoring.get("alerts") or {}).get("email", "")).strip().lower()
    domain = email.rpartition("@")[2]
    if domain in ("localhost", "example.com", "example.net", "example.org") or domain.endswith(
        (".invalid", ".localhost", ".test", ".example", ".local", ".example.com", ".example.net", ".example.org")
    ):
        errors.append("production monitoring.alerts.email must be a non-placeholder production recipient")
    pmm = monitoring.get("pmm") or {}
    try:
        pmm_url = urlsplit(str(pmm.get("server_url", "")))
        if pmm_url.scheme != "https" or not pmm_url.hostname or pmm_url.username is not None or pmm_url.password is not None:
            errors.append("production PMM requires an HTTPS server identity without embedded credentials")
        pmm_host = (pmm_url.hostname or "").lower().rstrip(".")
        pmm_address = normalized_address(pmm_host)
        if pmm_host == "localhost" or pmm_host.endswith(".localhost") or (
            pmm_address is not None and (pmm_address.is_loopback or pmm_address.is_unspecified)
        ):
            errors.append("production PMM server identity must be reachable outside loopback")
    except ValueError:
        errors.append("production PMM server URL is malformed")
    if group_hosts(inventory, "infra"):
        errors.append("production inventory must not contain managed laboratory infra hosts")
    if kind == "platform" and not group_hosts(inventory, "app"):
        errors.append("production platform requires an app host to verify frontend TLS from the client")
    if kind == "cluster":
        if not isinstance(inventory, dict):
            errors.append("production backup location requires the sibling inventory.yml")
        else:
            names, addresses = database_identities(inventory)
            backup = configuration.get("backup") or {}
            destination = backup.get("destination")
            endpoint = ((backup.get("s3") or {}).get("endpoint", "") if destination == "s3"
                        else (backup.get("smb") or {}).get("source", ""))
            errors.extend(off_host_errors(destination, endpoint, names, addresses))
    return errors


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("cluster", "platform"):
        print("usage: production_profile.py cluster|platform", file=sys.stderr)
        return 2
    kind = sys.argv[1]
    try:
        data = json.load(sys.stdin)
        configuration = data["configuration"]
        inventory = data["inventory"]
        if not isinstance(configuration, dict) or not isinstance(inventory, dict):
            raise ValueError("configuration and inventory must be mappings")
        declared = data.get("declared_environment")
        effective = (configuration.get(kind) or {}).get("environment")
        if declared == "production" and effective != "production":
            print("FAIL: a production declaration cannot be downgraded by effective variables")
            return 1
        if effective != "production":
            print("SKIP: declaration and effective variables are not production")
            return 0
        schema_path = PROFILE.parent.parent / f"{'clusters' if kind == 'cluster' else 'platform'}/schema/{kind}.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        errors = [
            f"effective {kind} {'/'.join(str(part) for part in error.absolute_path) or '(root)'} "
            f"violates schema constraint {error.validator}"
            for error in Draft7Validator(schema).iter_errors(configuration)
        ]
        if not errors:
            errors = production_errors(configuration, kind, inventory)
    except (OSError, KeyError, TypeError, ValueError, AttributeError, yaml.YAMLError, SchemaError):
        print("FAIL: production policy input or required schema is missing or malformed")
        return 1
    if errors:
        for error in errors:
            print("FAIL: " + error)
        return 1
    print("PASS: effective production configuration satisfies the required profile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
