#!/usr/bin/env python3
"""Pure evidence contract for the P2 quorum-loss measurement."""
from __future__ import annotations

import json
import re

NO_ERROR = "brak"
OUTCOME_DEGRADED = "degraded"
OUTCOME_CLEAN = "clean"
OUTCOME_UNRESOLVED = "unresolved"

PROTOCOL_CODES = {"2026", "2027"}
CLEAN_CONNECTION_CODES = {"2002", "2003"}
BACKEND_CODE = "1047"
BACKEND_SQLSTATE = "08S01"

LOCAL_ACCEPTANCE = (
    "target_guard",
    "baseline_complete",
    "processes_dead",
    "survivor_non_primary",
    "vip_write_rejected",
    "direct_write_rejected",
    "backend_error_exact",
    "same_window_monitor",
    "exact_runtime_placement",
    "bounded_correlated_log",
    "dropins_absent",
    "restart_policy_restored",
    "nodes_primary_synced",
    "app_recovered",
    "credential_profile_absent",
    "classification_resolved",
)
FINAL_ACCEPTANCE = LOCAL_ACCEPTANCE + ("platform_verify", "post_build_gate")

_ERROR_RE = re.compile(r"ERROR\s+(?P<code>\d+)(?:\s+\((?P<state>[0-9A-Za-z]{5})\))?")


def parse_client_error(text):
    match = _ERROR_RE.search(text or "")
    if not match:
        return NO_ERROR, NO_ERROR
    return match.group("code"), (match.group("state") or NO_ERROR)


def classify_outcome(app_code, app_sqlstate, node_code, node_sqlstate):
    if (node_code, node_sqlstate) != (BACKEND_CODE, BACKEND_SQLSTATE):
        return OUTCOME_UNRESOLVED
    if app_code in PROTOCOL_CODES:
        return OUTCOME_DEGRADED
    if (app_code, app_sqlstate) == (BACKEND_CODE, BACKEND_SQLSTATE):
        return OUTCOME_CLEAN
    if app_code in CLEAN_CONNECTION_CODES:
        return OUTCOME_CLEAN
    return OUTCOME_UNRESOLVED


def option_file_quote(value):
    escapes = {
        "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t",
        "\b": "\\b", " ": "\\s", '"': '\\"', "'": "\\'",
    }
    return '"' + "".join(escapes.get(char, char) for char in value) + '"'


def parse_tsv(text, columns):
    rows = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        values = line.split("\t")
        if len(values) != len(columns):
            raise ValueError(f"expected {len(columns)} TSV columns, got {len(values)}: {line!r}")
        rows.append(dict(zip(columns, values)))
    return rows


def recovery_complete(states, nodes_expected):
    if len(states) != nodes_expected:
        return False
    return all(state == f"Primary/{nodes_expected}/4" for state in states.values())


def proxy_log_proves_backend_error(text, survivor_address):
    error = re.search(r"\b1047\b|WSREP has not yet prepared node", text or "", re.I)
    return bool(error and survivor_address in (text or ""))


def acceptance_failures(record, final=False):
    required = FINAL_ACCEPTANCE if final else LOCAL_ACCEPTANCE
    failed = [name for name in required if record.get("acceptance", {}).get(name) is not True]
    return failed + list(record.get("errors") or [])


def _symbol_map(record):
    topology = record["topology"]
    mapping = {
        record["cluster"]: "galera-tenant",
        topology["vip"]: "PROXY-VIP",
        topology["app_user"]: "app_user",
    }
    for index, (host, address) in enumerate(sorted(topology["galera"].items()), start=1):
        mapping[host] = f"db{index}"
        mapping[address] = f"db{index}"
    for index, (host, address) in enumerate(sorted(topology["proxysql"].items()), start=1):
        mapping[host] = f"proxy{index}"
        mapping[address] = f"proxy{index}"
    return mapping


def _symbolise(text, record):
    for needle, replacement in sorted(_symbol_map(record).items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(needle, replacement)
    return text


def render_record(record):
    acceptance_rows = ["| kryterium | wynik |", "| --- | --- |"]
    for name in FINAL_ACCEPTANCE:
        value = record.get("acceptance", {}).get(name)
        acceptance_rows.append(f"| `{name}` | `{value}` |")
    errors = record.get("errors") or []
    error_lines = [f"- {error}" for error in errors] or ["- brak"]
    sections = [
        f"# P2 — pomiar zachowania aplikacji przy utracie kworum ({record['cluster']})",
        "",
        f"**Run ID:** `{record['run_id']}`  ",
        f"**Data UTC:** `{record['timestamp_utc']}`  ",
        f"**Wynik:** `{record['outcome']}`  ",
        f"**Kontrakt:** `{record['contract']}`",
        "",
        "## Kryteria akceptacji",
        "",
        *acceptance_rows,
        "",
        "## Bledy / nierozstrzygniete warunki",
        "",
        *error_lines,
    ]
    for title, key in (
        ("Wersje", "versions"),
        ("Topologia", "topology"),
        ("Baseline", "baseline"),
        ("Okno awarii", "failure"),
        ("Cleanup", "cleanup"),
        ("Recovery", "recovery"),
        ("Bramki zewnetrzne", "external_gates"),
    ):
        sections.extend(["", f"## {title}", "", "```json", json.dumps(record.get(key), indent=2, ensure_ascii=False, sort_keys=True), "```"])
    sections.append("")
    return "\n".join(sections)


def render_issue_draft(record):
    problems = acceptance_failures(record, final=True)
    if record.get("outcome") != OUTCOME_DEGRADED:
        raise ValueError(f"issue draft requires degraded outcome, got {record.get('outcome')!r}")
    if problems:
        raise ValueError(f"issue draft requires complete accepted evidence: {problems}")

    failure = record["failure"]
    versions = record["versions"]
    runtime = failure["runtime_survivor"]
    monitor = failure["monitor_row"]
    gates = record["external_gates"]
    body = f"""# Client receives ERROR {failure['app_code']} while Galera backend returns ERROR 1047 (08S01)

## Summary
A client connected through ProxySQL receives `{failure['app_error']}` after a three-node Galera tenant loses quorum. The corresponding INSERT sent directly to the surviving backend returns `{failure['node_error']}`.

## Environment
- ProxySQL: {json.dumps(versions['proxysql'], sort_keys=True)}
- ProxySQL OS: {json.dumps(versions['os_proxysql'], sort_keys=True)}
- MariaDB: {versions['mariadb']}
- Backend OS: {versions['os_backend']}
- Client: {versions['client']}

## Topology
- one ProxySQL pair with a shared VIP
- one three-node Galera tenant
- one application client through the VIP
- tenant writer/offline hostgroups: {record['topology']['writer_hostgroup']}/{record['topology']['offline_hostgroup']}

## Steps to reproduce
1. Start a healthy three-node Galera tenant behind ProxySQL and verify an INSERT through the VIP.
2. Override `Restart=no` on two database services and verify the effective systemd policy.
3. SIGKILL both database processes and verify they are absent.
4. Wait for the survivor to report `wsrep_cluster_status=non-Primary`.
5. Run the same INSERT through ProxySQL and directly on the survivor.

## Expected result
ProxySQL preserves backend `ERROR 1047 (08S01)` or returns a clean connection failure.

## Actual result
- through ProxySQL: `{failure['app_error']}`
- direct backend: `{failure['node_error']}`

## ProxySQL monitor state
Failure window: `{failure['window_started_utc']}` to `{failure['window_ended_utc']}`.
Newest survivor sample: `{json.dumps(monitor, sort_keys=True)}`.

## Runtime placement
The survivor was `{runtime['status']}` in hostgroup {runtime['hostgroup_id']}. This placement is measured, not documented.

## ProxySQL log
~~~text
{failure['proxysql_log']}
~~~

## Recovery
- credential profile removed: {record['cleanup']['credential_profile_absent']}
- all nodes Primary/Synced: {record['acceptance']['nodes_primary_synced']}
- application write recovered: {record['acceptance']['app_recovered']}
- platform verify: {gates['platform_verify']}
- full tenant gate: {gates['post_build_gate']}

## Related
Issue #1596 is historical context for a backend 1047 reaching ProxySQL result handling; sameness with #1596 is not established.
"""
    return _symbolise(body, record)
