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
from ipaddress import ip_network


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

    # Firewalld rules emitted by this repository use family="ipv4".
    for field in (
        "application_cidrs",
        "database_cluster_cidrs",
        "administration_cidrs",
        "monitoring_cidrs",
    ):
        for cidr in cluster.get("network", {}).get(field, []):
            try:
                network = ip_network(cidr, strict=False)
            except ValueError as exc:
                errors.append(f"network.{field} contains invalid CIDR '{cidr}': {exc}")
                continue
            if network.version != 4:
                errors.append(
                    f"network.{field} contains unsupported IPv6 CIDR '{cidr}'; "
                    "current firewalld policy is IPv4-only"
                )

    # Check: versions.policy must be locked for production
    env = cluster.get("cluster", {}).get("environment", "")
    policy = cluster.get("versions", {}).get("policy", "")
    if env == "production" and policy != "locked":
        errors.append(f"production environment requires versions.policy=locked, got '{policy}'")

    # Bezpieczny default to 1. Wartosci 0/2 moga utracic ostatnie zatwierdzone
    # transakcje przy crashu hosta; w production wymagaja jawnej, maszynowo
    # sprawdzalnej akceptacji ryzyka zamiast komentarza/ADR poza configiem.
    tuning = cluster.get("mariadb_tuning", {})
    flush_at_commit = int(tuning.get("innodb_flush_log_at_trx_commit", 1))
    durability_risk_accepted = tuning.get("durability_risk_accepted", False)
    if env == "production" and flush_at_commit != 1 and not durability_risk_accepted:
        errors.append(
            "production with mariadb_tuning.innodb_flush_log_at_trx_commit "
            f"{flush_at_commit} requires mariadb_tuning.durability_risk_accepted=true"
        )

    # Check: tls.mode=disabled in production requires risk acceptance (ISC-45)
    tls_mode = cluster.get("tls", {}).get("mode", "")
    if env == "production" and tls_mode == "disabled":
        print("WARN: tls.mode=disabled in production — ISC-45 requires documented risk acceptance in Decisions")


    # Check: galera.nodes_expected must be 3 (v1 scope)
    nodes = cluster.get("galera", {}).get("nodes_expected", None)
    if nodes is not None and nodes != 3:
        errors.append(f"galera.nodes_expected={nodes} — v1 scope requires 3 (2+garbd/5/multi-DC needs ADR)")

    # Check: proxysql.nodes_expected must be 2
    pnodes = cluster.get("proxysql", {}).get("nodes_expected", None)
    if pnodes is not None and pnodes != 2:
        errors.append(f"proxysql.nodes_expected={pnodes} — v1 scope requires 2")

    # Check: wlaczony slow log musi miec WLASCICIELA rotacji.
    # MariaDB pisze slow log do datadir jako `<host>-slow.log`, a logrotate z
    # f11_log_lifecycle.yml obejmuje wylacznie /var/log/mariadb/*.log — ten plik
    # sie tam nie lapie. Jedyne, co go rotuje, to pmm-agent w trybie slowlog
    # (`--size-slow-logs`, przy pominieciu pola serwer uzywa swojej domyslnej
    # wartosci). Bez tej pary plik rosnie bez ograniczen na partycji bazy.
    slow_log = str(cluster.get("mariadb_tuning", {}).get("slow_query_log", "OFF")).upper()
    qan_source = cluster.get("monitoring", {}).get("qan_source", "perfschema")
    if slow_log == "ON" and qan_source != "slowlog":
        errors.append(
            f"mariadb_tuning.slow_query_log=ON wymaga monitoring.qan_source=slowlog "
            f"(jest '{qan_source}') — inaczej slow log w datadir nie ma czym byc rotowany"
        )

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
