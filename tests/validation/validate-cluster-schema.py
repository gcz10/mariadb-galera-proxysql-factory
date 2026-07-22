#!/usr/bin/env python3
"""
Walidator schema cluster.yml — sprawdza konfigurację klastra proti JSON Schema.
Uruchomienie: python3 tests/validation/validate-cluster-schema.py <cluster.yml> [schema.json]
Exit: 0 = PASS, 1 = FAIL.

Satisfies (partial): ISC-58 (config poprawny), ISC-3 (wersje policy).
Nie zastępuje sond hostowych — weryfikuje tylko strukturę konfiguracji.
"""
import sys
import json
import yaml
from pathlib import Path
from jsonschema import validate, ValidationError


def main():
    if len(sys.argv) < 2:
        print("Usage: validate-cluster-schema.py <cluster.yml> [schema.json]", file=sys.stderr)
        return 2

    cluster_path = Path(sys.argv[1])
    schema_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("clusters/schema/cluster.schema.json")

    if not cluster_path.exists():
        print(f"FAIL: cluster file not found: {cluster_path}", file=sys.stderr)
        return 1
    if not schema_path.exists():
        print(f"FAIL: schema file not found: {schema_path}", file=sys.stderr)
        return 1

    # Load schema
    with open(schema_path) as f:
        schema = json.load(f)

    # Load cluster.yml
    with open(cluster_path) as f:
        cluster = yaml.safe_load(f)

    # Validate
    try:
        validate(instance=cluster, schema=schema)
    except ValidationError as e:
        print(f"FAIL: {cluster_path}")
        print(f"  path: {'/'.join(str(p) for p in e.absolute_path) or '(root)'}")
        print(f"  error: {e.message}")
        return 1

    # Additional semantic checks beyond JSON Schema
    errors = []

    # Check: versions.policy must be locked for production
    env = cluster.get("cluster", {}).get("environment", "")
    policy = cluster.get("versions", {}).get("policy", "")
    if env == "production" and policy != "locked":
        errors.append(f"production environment requires versions.policy=locked, got '{policy}'")

    # Check: tls.mode=disabled in production requires risk acceptance (ISC-45)
    tls_mode = cluster.get("tls", {}).get("mode", "")
    if env == "production" and tls_mode == "disabled":
        print(f"WARN: tls.mode=disabled in production — ISC-45 requires documented risk acceptance in Decisions")

    # Check: read_write_split must be false (ISC-23)
    rws = cluster.get("proxysql", {}).get("read_write_split_enabled", None)
    if rws is True:
        errors.append("proxysql.read_write_split_enabled=true violates ISC-23 (split requires app analysis)")

    # Check: max_writers must be 1
    mw = cluster.get("proxysql", {}).get("max_writers", None)
    if mw is not None and mw != 1:
        errors.append(f"proxysql.max_writers={mw} violates constraint (must be 1)")

    # Check: galera.nodes_expected must be 3 (v1 scope)
    nodes = cluster.get("galera", {}).get("nodes_expected", None)
    if nodes is not None and nodes != 3:
        errors.append(f"galera.nodes_expected={nodes} — v1 scope requires 3 (2+garbd/5/multi-DC needs ADR)")

    # Check: proxysql.nodes_expected must be 2
    pnodes = cluster.get("proxysql", {}).get("nodes_expected", None)
    if pnodes is not None and pnodes != 2:
        errors.append(f"proxysql.nodes_expected={pnodes} — v1 scope requires 2")

    # Check: endpoint.type must match Interview decision (keepalived_vip)
    ep_type = cluster.get("proxysql", {}).get("endpoint", {}).get("type", "")
    if ep_type and ep_type != "keepalived_vip":
        print(f"WARN: endpoint.type='{ep_type}' — Interview decision was keepalived_vip; verify this is intentional")

    if errors:
        print(f"FAIL: {cluster_path}")
        for e in errors:
            print(f"  {e}")
        return 1

    print(f"PASS: {cluster_path} — schema valid, semantic checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
