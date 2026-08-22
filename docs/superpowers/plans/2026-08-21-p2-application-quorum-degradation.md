# P2 Application Quorum Degradation Measurement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing quorum-loss chaos probe into a fail-closed evidence producer, execute exactly one pinned run against `newclaude16-r9`, prove complete local and external recovery, and classify the result as `degraded`, `clean`, or `unresolved` without reusing or inferring evidence.

**Architecture:** `tests/lab/_quorum_evidence.py` owns pure parsing, classification, acceptance evaluation, symbolisation, and rendering. `tests/lab/chaos-app-degradation.py` owns remote I/O and one lifecycle: canonical target guard → protected client profile → fail-closed baseline → checked `Restart=no` arm → SIGKILL → same-window evidence → per-host unconditional cleanup → whole-cluster recovery → verified credential removal → one exact JSON artifact. External `platform-verify` and n16 post-build results are appended atomically to that same artifact before any record, contract cutover, or issue draft is produced.

**Tech Stack:** Python 3 standard library + PyYAML, Ansible ad-hoc (`ansible.builtin.shell` and `ansible.builtin.copy`), MariaDB client, ProxySQL admin interface on `127.0.0.1:6032`, Python `unittest`, GNU Make.

**Approved design:** `docs/superpowers/specs/2026-08-21-p2-application-quorum-degradation-design.md`.

---

## Research basis

- Existing harness: `tests/lab/chaos-app-degradation.py:1-279`. It already models abrupt quorum loss and has the right high-level ordering, but it prints only to stdout, embeds `MYSQL_PWD` in the Ansible argv, returns from inside the mutation block, checks recovery only on the survivor, and does not capture ProxySQL evidence.
- Existing ProxySQL admin credential pattern: `mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf -h127.0.0.1 -P6032 -uadmin -N -B` (`tests/lab/probe-proxysql.py:25-29`); the profile is deployed as root-only by `playbooks/platform_proxysql.yml:144-153`.
- ProxySQL `admin-version` is read-only and displays the version ([ProxySQL Admin Variables](https://proxysql.com/documentation/global-variables/admin-variables/#admin-version)).
- `mysql_server_galera_log.time_start_us` is the check-start timestamp in microseconds since Unix epoch ([ProxySQL monitor schema](https://proxysql.com/documentation/the-admin-schemas/monitor-schema)); this permits a strict `time_start_us >= window_start_us` check.
- Active ProxySQL error log: `/var/lib/proxysql/proxysql.log`; the repository logrotate policy covers `/var/lib/proxysql/*.log` (`playbooks/f11_log_lifecycle.yml:26`). A usable log delta therefore requires a successful `(inode, size)` mark before the client request and the same inode with non-decreasing size afterward.
- MariaDB option files support double-quoted values and escapes `\n`, `\r`, `\t`, `\b`, `\s`, `\"`, `\'`, and `\\` ([MariaDB option-file syntax](https://mariadb.com/docs/server/server-management/install-and-upgrade-mariadb/configuring-mariadb/configuring-mariadb-with-option-files.md#option-file-syntax)).
- `newclaude16-r9` canonical identity: config `clusters/newclaude16-r9/cluster.yml`, inventory `clusters/newclaude16-r9/inventory.yml`, Galera hosts `n16g1/n16g2/n16g3`, ProxySQL hosts `fcp1/fcp2`, app host `fcapp`, writer/offline hostgroups `810/840`, endpoint `192.168.1.139:6033`.
- Unit tests use stdlib `unittest`; CI command: `python3 -m unittest discover -s tests/unit -p 'test_*.py' -v` (`.github/workflows/ci.yml:45`).
- The secret guard scans tracked and untracked non-ignored files and current process argv (`tests/validation/probe-no-secrets-leak.sh`). Raw artifacts therefore remain outside the repository.

## Global constraints

1. The destructive target is pinned to canonical `newclaude16-r9`: exact cluster name, resolved config path, resolved inventory path, and exact Galera/ProxySQL/app host sets. A different laboratory cluster is refused.
2. The operator must pass both `CLUSTER=newclaude16-r9` and `CONFIRM=yes`; Python independently revalidates the target before any remote credential copy or mutation.
3. One invocation gets one caller-generated 32-hex `QUORUM_RUN_ID`. The exact artifact path is `/var/tmp/quorum-evidence-newclaude16-r9-${QUORUM_RUN_ID}.json`; later tasks read that path from `/var/tmp/p2-current-artifact-path`. Directory-wide “latest file” discovery is prohibited.
4. The app password appears only in the Python environment and `0600` option files. It never appears in local or remote process argv. Remote profile removal is retried and independently verified before the artifact is written.
5. The cleanup set is both intended stopped hosts and is established before the first drop-in write. Drop-in removal, daemon reload, prior restart-policy restoration, and `systemctl start --no-block` are attempted independently for every cleanup host even when another operation fails.
6. A node is killed only after the drop-in write succeeds, daemon-reload succeeds, and `systemctl show mariadb -p Restart --value` proves effective `Restart=no`.
7. No return or uncaught exception is allowed between the first remote mutation and whole-fleet recovery evaluation.
8. A write accepted through either path without quorum is a critical failure. A run without two confirmed-dead processes or survivor `non-Primary` is invalid.
9. Evidence is accepted only when all sources are structured and correlated: exact UTC window, stable VIP holder, post-window-start monitor row for the survivor, exact survivor `(hostgroup_id, status)` runtime row, and a bounded error-log byte delta from the same ProxySQL node containing both backend `1047`/WSREP text and the survivor address.
10. Contract mismatch is separate from acceptance failure. A measured `clean` outcome under the old `degraded` default may exit with a distinct mismatch code, but cutover is allowed only when every local and external acceptance flag is true and `errors` is empty.
11. No issue body is rendered by the destructive harness. The sanitized draft is generated only after `platform-verify` and the full n16 post-build gate are recorded as passed in the same artifact.
12. No local scheduler/controller, hostgroup mutation, client retry change, or production/staging test is in scope.
13. Push, PR creation, and upstream issue publication require explicit operator instruction after local commits. The plan stops before each remote/public action otherwise.

## Final file map

**Create**
- `tests/lab/_quorum_evidence.py` — pure evidence contract and rendering.
- `tests/unit/test_quorum_evidence.py` — parsing/classification/rendering contract.
- `tests/unit/test_quorum_degradation_harness.py` — target guards, secret transport, log/window collectors, arm/cleanup fault injection.
- `docs/records/2026-08-21-p2-quorum-degradation-measurement.md` — generated from the final artifact.

**Modify**
- `tests/lab/chaos-app-degradation.py` — structured collectors and safe lifecycle.
- `Makefile` target `lab-app-degradation-test` — confirmation, run ID, exact environment.
- `README.md` — exact operator invocation and artifact pointer.
- Conditional on measured `clean`: the default contract in `tests/lab/chaos-app-degradation.py` and `Makefile`, plus stale historical prose in `tests/lab/probe-app-conformance.py` and `tests/lab/probe-proxysql.py`.

**Delete:** nothing.

---

### Task 1: Pure evidence contract

**Files:**
- Create: `tests/lab/_quorum_evidence.py`
- Test: `tests/unit/test_quorum_evidence.py`

**Interfaces:**
- Produces: `parse_client_error`, `classify_outcome`, `option_file_quote`, `parse_tsv`, `recovery_complete`, `proxy_log_proves_backend_error`, `acceptance_failures`, `render_record`, `render_issue_draft`.
- Later tasks use the constants `OUTCOME_DEGRADED`, `OUTCOME_CLEAN`, `OUTCOME_UNRESOLVED`, `NO_ERROR`, `LOCAL_ACCEPTANCE`, and `FINAL_ACCEPTANCE` unchanged.

- [ ] **Step 1: Write the failing unit contract**

Create `tests/unit/test_quorum_evidence.py`:

```python
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "lab"))

import _quorum_evidence as qe  # noqa: E402


def sample_record():
    acceptance = {name: True for name in qe.FINAL_ACCEPTANCE}
    return {
        "run_id": "a" * 32,
        "cluster": "newclaude16-r9",
        "timestamp_utc": "2026-08-21T12:00:00Z",
        "outcome": qe.OUTCOME_DEGRADED,
        "contract": {"expected": "degraded", "observed": "degraded", "match": True},
        "acceptance": acceptance,
        "errors": [],
        "versions": {
            "proxysql": {"fcp1": "3.0.10", "fcp2": "3.0.10"},
            "mariadb": "11.4.12-MariaDB-log",
            "client": "mariadb 11.4.12",
            "os_proxysql": {"fcp1": "Rocky Linux 10", "fcp2": "Rocky Linux 10"},
            "os_backend": "Rocky Linux 9.6",
        },
        "topology": {
            "vip": "192.168.1.139",
            "app_user": "app_user_n16",
            "galera": {"n16g1": "192.168.1.172", "n16g2": "192.168.1.173", "n16g3": "192.168.1.174"},
            "proxysql": {"fcp1": "192.168.1.131", "fcp2": "192.168.1.132"},
            "writer_hostgroup": 810,
            "offline_hostgroup": 840,
        },
        "baseline": {
            "vip_holder": "fcp1",
            "galera": {"n16g1": "Primary/3/4", "n16g2": "Primary/3/4", "n16g3": "Primary/3/4"},
            "runtime_writer": {"fcp1": {"hostgroup_id": 810, "hostname": "192.168.1.172", "status": "ONLINE"}},
            "app_write_ok": True,
        },
        "failure": {
            "window_started_utc": "2026-08-21T12:01:00Z",
            "window_ended_utc": "2026-08-21T12:01:03Z",
            "survivor": "n16g1",
            "stopped": ["n16g2", "n16g3"],
            "survivor_status": "non-Primary",
            "app_code": "2027",
            "app_sqlstate": "HY000",
            "app_error": "ERROR 2027 (HY000): Received malformed packet",
            "node_code": "1047",
            "node_sqlstate": "08S01",
            "node_error": "ERROR 1047 (08S01): WSREP has not yet prepared node for application use",
            "monitor_row": {
                "hostname": "192.168.1.172", "time_start_us": 1787313661000000,
                "primary_partition": "NO", "wsrep_local_state": 0, "error": "",
            },
            "runtime_survivor": {"hostgroup_id": 810, "hostname": "192.168.1.172", "status": "ONLINE"},
            "proxysql_node": "fcp1",
            "proxysql_log": "2026-08-21 error 1047 backend 192.168.1.172 WSREP has not yet prepared node",
        },
        "cleanup": {
            "nodes": {"n16g2": {"dropin_absent": True, "restart_policy_restored": True, "start_enqueued": True},
                      "n16g3": {"dropin_absent": True, "restart_policy_restored": True, "start_enqueued": True}},
            "credential_profile_absent": True,
        },
        "recovery": {
            "nodes": {"n16g1": "Primary/3/4", "n16g2": "Primary/3/4", "n16g3": "Primary/3/4"},
            "app_write_ok": True,
        },
        "external_gates": {
            "platform_verify": {"ok": True, "command": "make platform-verify", "rc": 0},
            "post_build_gate": {"ok": True, "command": "make lab-post-build-gate CLUSTER=newclaude16-r9", "rc": 0},
        },
    }


class ParsingTests(unittest.TestCase):
    def test_error_code_and_sqlstate(self):
        self.assertEqual(
            qe.parse_client_error("ERROR 1047 (08S01): WSREP has not yet prepared node"),
            ("1047", "08S01"),
        )

    def test_missing_sqlstate_is_not_inferred(self):
        self.assertEqual(qe.parse_client_error("ERROR 2027: malformed"), ("2027", qe.NO_ERROR))

    def test_tsv_rows_are_named_and_malformed_rows_fail(self):
        self.assertEqual(
            qe.parse_tsv("810\t192.0.2.1\tONLINE", ("hostgroup_id", "hostname", "status")),
            [{"hostgroup_id": "810", "hostname": "192.0.2.1", "status": "ONLINE"}],
        )
        with self.assertRaises(ValueError):
            qe.parse_tsv("810\tmissing", ("hostgroup_id", "hostname", "status"))


class ClassificationTests(unittest.TestCase):
    def test_degraded_clean_and_unresolved_are_disjoint(self):
        self.assertEqual(qe.classify_outcome("2027", "HY000", "1047", "08S01"), qe.OUTCOME_DEGRADED)
        self.assertEqual(qe.classify_outcome("1047", "08S01", "1047", "08S01"), qe.OUTCOME_CLEAN)
        self.assertEqual(qe.classify_outcome("2003", "HY000", "1047", "08S01"), qe.OUTCOME_CLEAN)
        self.assertEqual(qe.classify_outcome("2013", "HY000", "1047", "08S01"), qe.OUTCOME_UNRESOLVED)
        self.assertEqual(qe.classify_outcome("2027", "HY000", "1047", qe.NO_ERROR), qe.OUTCOME_UNRESOLVED)


class SafetyHelpersTests(unittest.TestCase):
    def test_option_file_quote_uses_documented_escapes(self):
        self.assertEqual(qe.option_file_quote('a b#c"\\\n\t'), '"a\\sb#c\\"\\\\\\n\\t"')

    def test_whole_cluster_recovery_is_exact(self):
        self.assertTrue(qe.recovery_complete({"a": "Primary/3/4", "b": "Primary/3/4", "c": "Primary/3/4"}, 3))
        self.assertFalse(qe.recovery_complete({"a": "Primary/2/4", "b": "Primary/2/4", "c": "?/?/?"}, 3))

    def test_log_proof_requires_error_and_survivor_address(self):
        self.assertTrue(qe.proxy_log_proves_backend_error("error 1047 backend 192.0.2.7", "192.0.2.7"))
        self.assertFalse(qe.proxy_log_proves_backend_error("error 1047 backend 192.0.2.8", "192.0.2.7"))
        self.assertFalse(qe.proxy_log_proves_backend_error("backend 192.0.2.7 timeout", "192.0.2.7"))


class AcceptanceAndRenderingTests(unittest.TestCase):
    def test_final_acceptance_is_fail_closed_and_lists_errors(self):
        record = sample_record()
        self.assertEqual(qe.acceptance_failures(record, final=True), [])
        record["acceptance"]["platform_verify"] = False
        record["errors"].append("platform gate failed")
        self.assertEqual(
            qe.acceptance_failures(record, final=True),
            ["platform_verify", "platform gate failed"],
        )
        rendered = qe.render_record(record)
        self.assertIn("platform gate failed", rendered)
        self.assertIn("platform_verify", rendered)

    def test_issue_requires_final_acceptance(self):
        record = sample_record()
        record["acceptance"]["post_build_gate"] = None
        with self.assertRaises(ValueError):
            qe.render_issue_draft(record)

    def test_issue_is_symbolic_and_uses_measured_runtime_row(self):
        draft = qe.render_issue_draft(sample_record())
        for leaked in ("192.168.1.", "n16g", "fcp", "app_user_n16", "newclaude16-r9"):
            self.assertNotIn(leaked, draft)
        self.assertIn("hostgroup 810", draft)
        self.assertIn("ONLINE", draft)
        self.assertIn("measured, not documented", draft)
        self.assertIn("sameness with #1596 is not established", draft)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and prove red**

Run: `python3 -m unittest tests.unit.test_quorum_evidence -v`
Expected: `ModuleNotFoundError: No module named '_quorum_evidence'`.

- [ ] **Step 3: Implement the pure module**

Create `tests/lab/_quorum_evidence.py`:

```python
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
```

- [ ] **Step 4: Prove green and run the existing unit suite**

Run:

```bash
python3 -m unittest tests.unit.test_quorum_evidence -v
python3 -m unittest discover -s tests/unit -p 'test_*.py' -v
python3 -m pyflakes tests/lab/_quorum_evidence.py tests/unit/test_quorum_evidence.py
```

Expected: all cases `ok`; no pyflakes output.

- [ ] **Step 5: Commit**

```bash
git add tests/lab/_quorum_evidence.py tests/unit/test_quorum_evidence.py
git commit -m "test(lab): define fail-closed quorum evidence contract"
```

---

### Task 2: Canonical target, protected credential, and structured collectors

**Files:**
- Modify: `tests/lab/chaos-app-degradation.py:44-125`
- Create: `tests/unit/test_quorum_degradation_harness.py`

**Interfaces:**
- Produces in the harness: `validate_target`, `safe_sh`, `install_app_profile`, `remove_app_profile`, `app_setup`, `app_write`, `collect_versions`, `runtime_snapshot`, `galera_state`, `log_mark`, `log_delta`, `monitor_row`.
- `safe_sh` always returns `{ok, rc, output, error}` and never raises; mutation cleanup and recovery polling rely on that invariant.

- [ ] **Step 1: Write failing wiring/collector tests**

Create `tests/unit/test_quorum_degradation_harness.py`:

```python
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "tests" / "lab" / "chaos-app-degradation.py"


def load_probe(tmp):
    cluster = {
        "cluster": {"name": "newclaude16-r9", "environment": "laboratory"},
        "galera": {"nodes_expected": 3},
        "proxysql": {"app_user": "app_user_n16", "hostgroup_base": 810,
                     "endpoint": {"address": "192.0.2.10", "port": 6033}},
    }
    inventory = {"all": {"children": {
        "galera": {"hosts": {"n16g1": {"ansible_host": "192.0.2.1"},
                              "n16g2": {"ansible_host": "192.0.2.2"},
                              "n16g3": {"ansible_host": "192.0.2.3"}}},
        "proxysql": {"hosts": {"fcp1": {"ansible_host": "192.0.2.11"},
                                "fcp2": {"ansible_host": "192.0.2.12"}}},
        "app": {"hosts": {"fcapp": {"ansible_host": "192.0.2.20"}}},
    }}}
    config = tmp / "cluster.yml"
    inv = tmp / "inventory.yml"
    config.write_text(yaml.safe_dump(cluster), encoding="utf-8")
    inv.write_text(yaml.safe_dump(inventory), encoding="utf-8")
    env = {
        "CLUSTER": "newclaude16-r9",
        "CLUSTER_CONFIG": str(config),
        "CLUSTER_INVENTORY": str(inv),
        "APP_DB_PASSWORD": "hunter2",
        "QUORUM_RUN_ID": "a" * 32,
    }
    sys.path.insert(0, str(PROBE.parent))
    try:
        with patch.dict(os.environ, env, clear=False):
            return runpy.run_path(str(PROBE))
    finally:
        sys.path.remove(str(PROBE.parent))


class TargetGuardTests(unittest.TestCase):
    def test_other_laboratory_cluster_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        errors = ns["validate_target"](
            "other-lab", ns["EXPECTED_CONFIG"], ns["EXPECTED_INVENTORY"],
            {"n16g1", "n16g2", "n16g3"}, {"fcp1", "fcp2"}, {"fcapp"},
        )
        self.assertIn("cluster name", " ".join(errors))

    def test_noncanonical_paths_are_refused_even_with_spoofed_name(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        errors = ns["validate_target"](
            "newclaude16-r9", Path("/tmp/spoof.yml"), Path("/tmp/spoof-inventory.yml"),
            {"n16g1", "n16g2", "n16g3"}, {"fcp1", "fcp2"}, {"fcapp"},
        )
        self.assertTrue(any("config path" in error for error in errors))
        self.assertTrue(any("inventory path" in error for error in errors))


class SafeRunnerTests(unittest.TestCase):
    def test_timeout_becomes_structured_failure(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ansible", 1)):
            result = ns["safe_sh"]("n16g1", "true", timeout=1)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["rc"])
        self.assertIn("timeout", result["error"])


class SecretTransportTests(unittest.TestCase):
    def test_app_write_uses_profile_not_password_or_setup(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        calls = []

        def fake(host, script, timeout=120):
            calls.append((host, script))
            return {"ok": True, "rc": 0, "output": "", "error": ""}

        with patch.dict(ns["app_write"].__globals__, {"safe_sh": fake}):
            ns["app_write"]()
        command = calls[-1][1]
        self.assertIn("--defaults-extra-file=/run/isa-app-degradation.cnf", command)
        self.assertNotIn("MYSQL_PWD", command)
        self.assertNotIn("hunter2", command)
        self.assertNotIn("CREATE TABLE", command)

    def test_profile_removal_retries_and_verifies_absence(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        responses = iter([
            {"ok": True, "rc": 0, "output": "", "error": ""},
            {"ok": True, "rc": 0, "output": "PRESENT", "error": ""},
            {"ok": True, "rc": 0, "output": "", "error": ""},
            {"ok": True, "rc": 0, "output": "ABSENT", "error": ""},
        ])
        globals_ = ns["remove_app_profile"].__globals__
        with patch.dict(globals_, {"safe_sh": lambda *args, **kwargs: next(responses)}), \
                patch.object(globals_["time"], "sleep", return_value=None):
            result = ns["remove_app_profile"]()
        self.assertTrue(result["absent"])
        self.assertEqual(len(result["history"]), 2)


class StructuredCollectorTests(unittest.TestCase):
    def test_missing_log_mark_is_not_offset_zero(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        failed = {"ok": False, "rc": 1, "output": "", "error": "stat failed"}
        with patch.dict(ns["log_mark"].__globals__, {"safe_sh": lambda *args, **kwargs: failed}):
            with self.assertRaises(ns["EvidenceError"]):
                ns["log_mark"]("fcp1")

    def test_log_delta_rejects_inode_change(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        outputs = iter([
            {"ok": True, "rc": 0, "output": "222\t120", "error": ""},
        ])
        with patch.dict(ns["log_delta"].__globals__, {"safe_sh": lambda *args, **kwargs: next(outputs)}):
            with self.assertRaises(ns["EvidenceError"]):
                ns["log_delta"]("fcp1", {"inode": 111, "size": 100})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and prove red**

Run: `python3 -m unittest tests.unit.test_quorum_degradation_harness -v`
Expected: failures for missing `validate_target`, `safe_sh`, `log_mark`, and `EvidenceError`.

- [ ] **Step 3: Add imports, exact constants, and canonical guard**

In `tests/lab/chaos-app-degradation.py`, add `import datetime`, `import json`, `import tempfile`, `from pathlib import Path`, and this explicit Task 1 import:

```python
from _quorum_evidence import (
    LOCAL_ACCEPTANCE,
    OUTCOME_CLEAN,
    OUTCOME_DEGRADED,
    OUTCOME_UNRESOLVED,
    acceptance_failures,
    classify_outcome,
    option_file_quote,
    parse_client_error,
    parse_tsv,
    proxy_log_proves_backend_error,
    recovery_complete,
)

CLUSTER_NAME = CLUSTER["cluster"]["name"]
NODES_EXPECTED = int(CLUSTER["galera"]["nodes_expected"])
PROXYSQL_HOSTS = list(((INV["all"]["children"].get("proxysql") or {}).get("hosts") or {}))
GALERA_ADDR = {
    host: (values or {}).get("ansible_host", host)
    for host, values in INV["all"]["children"]["galera"]["hosts"].items()
}
PROXYSQL_ADDR = {
    host: (values or {}).get("ansible_host", host)
    for host, values in ((INV["all"]["children"].get("proxysql") or {}).get("hosts") or {}).items()
}
HG_BASE = int(CLUSTER["proxysql"]["hostgroup_base"])
WRITER_HG = HG_BASE
OFFLINE_HG = HG_BASE + 30

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_CLUSTER = "newclaude16-r9"
EXPECTED_CONFIG = (REPO_ROOT / "clusters/newclaude16-r9/cluster.yml").resolve()
EXPECTED_INVENTORY = (REPO_ROOT / "clusters/newclaude16-r9/inventory.yml").resolve()
EXPECTED_GALERA = {"n16g1", "n16g2", "n16g3"}
EXPECTED_PROXYSQL = {"fcp1", "fcp2"}
EXPECTED_APP = {"fcapp"}
APP_CNF = "/run/isa-app-degradation.cnf"
PROXYSQL_LOG = "/var/lib/proxysql/proxysql.log"
ADMIN_CLIENT = ("mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf "
                "-h127.0.0.1 -P6032 -uadmin -N -B")
RUN_ID = os.environ.get("QUORUM_RUN_ID", "")


class EvidenceError(RuntimeError):
    pass


def validate_target(cluster_name, config_path, inventory_path, galera_hosts, proxy_hosts, app_hosts):
    errors = []
    if cluster_name != EXPECTED_CLUSTER:
        errors.append(f"cluster name must be {EXPECTED_CLUSTER}, got {cluster_name}")
    if Path(config_path).resolve() != EXPECTED_CONFIG:
        errors.append(f"config path must resolve to {EXPECTED_CONFIG}")
    if Path(inventory_path).resolve() != EXPECTED_INVENTORY:
        errors.append(f"inventory path must resolve to {EXPECTED_INVENTORY}")
    if set(galera_hosts) != EXPECTED_GALERA:
        errors.append(f"Galera hosts must be {sorted(EXPECTED_GALERA)}")
    if set(proxy_hosts) != EXPECTED_PROXYSQL:
        errors.append(f"ProxySQL hosts must be {sorted(EXPECTED_PROXYSQL)}")
    if set(app_hosts) != EXPECTED_APP:
        errors.append(f"app hosts must be {sorted(EXPECTED_APP)}")
    if ENVIRONMENT != "laboratory":
        errors.append(f"environment must be laboratory, got {ENVIRONMENT!r}")
    if not re.fullmatch(r"[0-9a-f]{32}", RUN_ID):
        errors.append("QUORUM_RUN_ID must be exactly 32 lowercase hex characters")
    return errors
```

- [ ] **Step 4: Replace exception-throwing remote calls with a structured safe runner**

Keep existing `sh()` for parsing compatibility, then add:

```python
def safe_sh(host, script, timeout=120):
    try:
        rc, output = sh(host, script, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "rc": None, "output": "", "error": f"timeout after {exc.timeout}s"}
    return {
        "ok": rc == 0,
        "rc": rc,
        "output": output.strip(),
        "error": "" if rc == 0 else output.strip()[:300],
    }


def must_output(host, script, label, timeout=120):
    result = safe_sh(host, script, timeout=timeout)
    if not result["ok"]:
        raise EvidenceError(f"{label} on {host}: {result['error']}")
    return result["output"]
```

- [ ] **Step 5: Implement protected profile and app queries**

```python
def install_app_profile():
    fd, local_path = tempfile.mkstemp(prefix="isa-app-degradation-", suffix=".cnf", text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                "[client]\n"
                f"user={option_file_quote(APP_USER)}\n"
                f"password={option_file_quote(APP_PW)}\n"
            )
        command = [
            ANSIBLE, APP_HOST, "-i", INVENTORY,
            "-m", "ansible.builtin.copy",
            "-a", f"src={local_path} dest={APP_CNF} owner=root group=root mode=0600",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise EvidenceError((result.stdout + result.stderr).strip()[:300])
    finally:
        if os.path.exists(local_path):
            os.unlink(local_path)


def remove_app_profile(attempts=3):
    history = []
    for attempt in range(1, attempts + 1):
        remove = safe_sh(APP_HOST, f"rm -f {APP_CNF}", timeout=60)
        verify = safe_sh(APP_HOST, f"test ! -e {APP_CNF} && echo ABSENT || echo PRESENT", timeout=60)
        history.append({"attempt": attempt, "remove": remove, "verify": verify})
        if verify["ok"] and verify["output"] == "ABSENT":
            return {"absent": True, "history": history}
        time.sleep(2)
    return {"absent": False, "history": history}


def app_query(sql):
    return safe_sh(
        APP_HOST,
        f"timeout 25 mariadb --defaults-extra-file={APP_CNF} "
        f"-h {VIP} -P {VIP_PORT} --ssl-verify-server-cert=0 --connect-timeout=5 "
        f"isa_test -e \"{sql}\" 2>&1",
        timeout=40,
    )


def app_setup():
    return app_query(
        "CREATE TABLE IF NOT EXISTS app_degradation "
        "(id BIGINT AUTO_INCREMENT PRIMARY KEY, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )


def app_write():
    return app_query("INSERT INTO app_degradation () VALUES ()")
```

- [ ] **Step 6: Implement structured state, version, runtime, monitor, and log collectors**

```python
def admin_rows(host, sql, columns):
    output = must_output(host, f'{ADMIN_CLIENT} -e "{sql}" 2>&1', "ProxySQL admin query", timeout=60)
    return parse_tsv(output, columns)


def galera_state(host):
    output = must_output(
        host,
        "timeout 15 mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e \""
        "SHOW STATUS WHERE Variable_name IN ('wsrep_cluster_status',"
        "'wsrep_cluster_size','wsrep_local_state')\" 2>&1",
        "Galera state",
        timeout=30,
    )
    values = {}
    for row in parse_tsv(output, ("name", "value")):
        values[row["name"]] = row["value"]
    return (f"{values.get('wsrep_cluster_status', '?')}/"
            f"{values.get('wsrep_cluster_size', '?')}/"
            f"{values.get('wsrep_local_state', '?')}")


def os_version(host):
    return must_output(
        host,
        '. /etc/os-release && printf "%s; kernel %s" "$PRETTY_NAME" "$(uname -r)"',
        "OS version",
        timeout=30,
    )


def vip_holder():
    holders = []
    for host in PROXYSQL_HOSTS:
        result = safe_sh(host, f"ip -br addr show | grep -q '{VIP}/' && echo HOLDER || echo NO", timeout=30)
        if not result["ok"]:
            raise EvidenceError(f"VIP probe failed on {host}: {result['error']}")
        if result["output"] == "HOLDER":
            holders.append(host)
    if len(holders) != 1:
        raise EvidenceError(f"expected exactly one VIP holder, got {holders}")
    return holders[0]


def runtime_snapshot(host):
    rows = admin_rows(
        host,
        f"SELECT hostgroup_id, hostname, status FROM runtime_mysql_servers "
        f"WHERE hostgroup_id IN ({WRITER_HG}, {OFFLINE_HG}) ORDER BY hostgroup_id, hostname",
        ("hostgroup_id", "hostname", "status"),
    )
    for row in rows:
        row["hostgroup_id"] = int(row["hostgroup_id"])
    return rows


def collect_versions():
    proxy_versions = {}
    proxy_os = {}
    for host in PROXYSQL_HOSTS:
        rows = admin_rows(
            host,
            "SELECT variable_value FROM global_variables WHERE variable_name='admin-version'",
            ("version",),
        )
        if len(rows) != 1 or not rows[0]["version"]:
            raise EvidenceError(f"missing admin-version on {host}")
        proxy_versions[host] = rows[0]["version"]
        proxy_os[host] = os_version(host)
    mariadb = must_output(
        SURVIVOR,
        "timeout 15 mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e \"SELECT VERSION()\"",
        "MariaDB version",
        timeout=30,
    )
    client = must_output(APP_HOST, "mariadb --version", "client version", timeout=30)
    return {
        "proxysql": proxy_versions,
        "mariadb": mariadb,
        "client": client,
        "os_proxysql": proxy_os,
        "os_backend": os_version(SURVIVOR),
    }


def log_mark(host):
    output = must_output(host, f"stat -Lc '%i\\t%s' {PROXYSQL_LOG}", "ProxySQL log mark", timeout=30)
    rows = parse_tsv(output, ("inode", "size"))
    if len(rows) != 1:
        raise EvidenceError(f"invalid log mark on {host}: {output!r}")
    return {"inode": int(rows[0]["inode"]), "size": int(rows[0]["size"])}


def log_delta(host, mark):
    output = must_output(host, f"stat -Lc '%i\\t%s' {PROXYSQL_LOG}", "ProxySQL log recheck", timeout=30)
    rows = parse_tsv(output, ("inode", "size"))
    if len(rows) != 1:
        raise EvidenceError(f"invalid log recheck on {host}: {output!r}")
    inode, size = int(rows[0]["inode"]), int(rows[0]["size"])
    if inode != mark["inode"]:
        raise EvidenceError(f"ProxySQL log rotated on {host}: inode {mark['inode']} -> {inode}")
    if size < mark["size"]:
        raise EvidenceError(f"ProxySQL log shrank on {host}: {mark['size']} -> {size}")
    delta = must_output(
        host,
        f"tail -c +{mark['size'] + 1} {PROXYSQL_LOG} 2>/dev/null",
        "ProxySQL log delta",
        timeout=60,
    )
    lines = delta.splitlines()
    if len(lines) > 200:
        lines = [f"[pominieto {len(lines) - 200} wczesniejszych linii]"] + lines[-200:]
    return "\n".join(lines)


def monitor_row(host, survivor_address):
    rows = admin_rows(
        host,
        "SELECT hostname, time_start_us, primary_partition, wsrep_local_state, "
        "COALESCE(error, '') FROM mysql_server_galera_log "
        f"WHERE hostname='{survivor_address}' ORDER BY time_start_us DESC LIMIT 1",
        ("hostname", "time_start_us", "primary_partition", "wsrep_local_state", "error"),
    )
    if len(rows) != 1:
        raise EvidenceError(f"missing latest monitor row for {survivor_address} on {host}")
    row = rows[0]
    row["time_start_us"] = int(row["time_start_us"])
    row["wsrep_local_state"] = int(row["wsrep_local_state"])
    return row
```

- [ ] **Step 7: Prove green and commit**

Run:

```bash
python3 -m unittest tests.unit.test_quorum_degradation_harness -v
python3 -m pyflakes tests/lab/chaos-app-degradation.py tests/unit/test_quorum_degradation_harness.py
bash tests/validation/probe-no-secrets-leak.sh
```

Expected: all tests `ok`; no pyflakes output; secret guard PASS.

```bash
git add tests/lab/chaos-app-degradation.py tests/unit/test_quorum_degradation_harness.py
git commit -m "feat(lab): add pinned target and structured quorum evidence collectors"
```

---

### Task 3: Checked mutation, unconditional cleanup, and final local artifact

**Files:**
- Modify: `tests/lab/chaos-app-degradation.py:127-279`
- Modify: `tests/unit/test_quorum_degradation_harness.py`

**Interfaces:**
- Produces: `arm_node`, `cleanup_nodes`, `new_record`, `run_measurement`, `write_artifact`, and a `main()` that writes the artifact only after credential removal verification.
- `run_measurement(record)` never writes a file and never renders Markdown; it returns the updated record after local recovery checks.

- [ ] **Step 1: Add failing fault-injection tests**

Append to `tests/unit/test_quorum_degradation_harness.py`:

```python
class MutationLifecycleTests(unittest.TestCase):
    def test_arm_never_kills_after_reload_failure(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        calls = []

        def run(host, script, timeout=120):
            calls.append(script)
            if "daemon-reload" in script:
                return {"ok": False, "rc": 1, "output": "", "error": "reload failed"}
            return {"ok": True, "rc": 0, "output": "no" if "show mariadb" in script else "", "error": ""}

        with self.assertRaises(ns["EvidenceError"]):
            ns["arm_node"]("n16g2", run=run)
        self.assertFalse(any("pkill" in command for command in calls))

    def test_arm_rejects_kill_failure_and_still_alive_process(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))

        def kill_fails(host, script, timeout=120):
            if "show mariadb" in script:
                return {"ok": True, "rc": 0, "output": "no", "error": ""}
            if "pkill" in script:
                return {"ok": False, "rc": 1, "output": "", "error": "kill failed"}
            return {"ok": True, "rc": 0, "output": "", "error": ""}

        with self.assertRaises(ns["EvidenceError"]):
            ns["arm_node"]("n16g2", run=kill_fails)

        def still_alive(host, script, timeout=120):
            if "show mariadb" in script:
                output = "no"
            elif "pgrep" in script:
                output = "ALIVE"
            else:
                output = ""
            return {"ok": True, "rc": 0, "output": output, "error": ""}

        with self.assertRaises(ns["EvidenceError"]):
            ns["arm_node"]("n16g2", run=still_alive)

    def test_cleanup_attempts_every_operation_on_every_host(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        calls = []

        def run(host, script, timeout=120):
            calls.append((host, script))
            if host == "n16g2" and script.startswith("rm -f"):
                return {"ok": False, "rc": None, "output": "", "error": "timeout"}
            if "test ! -e" in script:
                return {"ok": True, "rc": 0, "output": "ABSENT", "error": ""}
            if "show mariadb" in script:
                return {"ok": True, "rc": 0, "output": "on-abnormal", "error": ""}
            return {"ok": True, "rc": 0, "output": "", "error": ""}

        result = ns["cleanup_nodes"](
            ("n16g2", "n16g3"), {"n16g2": "on-abnormal", "n16g3": "on-abnormal"}, run=run
        )
        for host in ("n16g2", "n16g3"):
            host_commands = [command for called_host, command in calls if called_host == host]
            self.assertTrue(any("daemon-reload" in command for command in host_commands))
            self.assertTrue(any("start --no-block" in command for command in host_commands))
            self.assertTrue(any("test ! -e" in command for command in host_commands))
            self.assertTrue(any("show mariadb" in command for command in host_commands))
        self.assertFalse(result["n16g2"]["remove_ok"])
        self.assertTrue(result["n16g3"]["dropin_absent"])

    def test_cleanup_set_is_both_intended_hosts_before_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            ns = load_probe(Path(td))
        self.assertEqual(tuple(ns["STOPPED"]), ("n16g2", "n16g3"))
```

- [ ] **Step 2: Run and prove red**

Run: `python3 -m unittest tests.unit.test_quorum_degradation_harness.MutationLifecycleTests -v`
Expected: missing `arm_node` and `cleanup_nodes` failures.

- [ ] **Step 3: Implement checked arm and independent cleanup**

```python
def require_ok(result, label):
    if not result["ok"]:
        raise EvidenceError(f"{label}: {result['error']}")
    return result["output"]


def arm_node(host, run=safe_sh):
    require_ok(run(host, f"mkdir -p {DROPIN_DIR} && printf '[Service]\\nRestart=no\\n' > {DROPIN}"),
               f"write Restart=no on {host}")
    require_ok(run(host, "systemctl daemon-reload"), f"daemon-reload on {host}")
    policy = require_ok(run(host, "systemctl show mariadb -p Restart --value"),
                        f"read effective Restart on {host}")
    if policy.strip() != "no":
        raise EvidenceError(f"effective Restart on {host} is {policy!r}, expected 'no'")
    require_ok(run(host, "pkill -9 -x mariadbd"), f"SIGKILL mariadbd on {host}")
    alive = require_ok(
        run(host, "pgrep -x mariadbd >/dev/null && echo ALIVE || echo DEAD"),
        f"verify mariadbd death on {host}",
    )
    if alive.strip() != "DEAD":
        raise EvidenceError(f"mariadbd still alive on {host}")
    return True


def cleanup_nodes(hosts, restart_before, run=safe_sh):
    results = {}
    for host in hosts:
        remove = run(host, f"rm -f {DROPIN}", timeout=60)
        reload_result = run(host, "systemctl daemon-reload", timeout=60)
        start = run(host, "systemctl start --no-block mariadb", timeout=60)
        absent = run(host, f"test ! -e {DROPIN} && echo ABSENT || echo PRESENT", timeout=60)
        policy = run(host, "systemctl show mariadb -p Restart --value", timeout=60)
        results[host] = {
            "remove_ok": remove["ok"],
            "reload_ok": reload_result["ok"],
            "start_enqueued": start["ok"],
            "dropin_absent": absent["ok"] and absent["output"] == "ABSENT",
            "restart_policy_before": restart_before.get(host),
            "restart_policy_after": policy["output"] if policy["ok"] else None,
            "restart_policy_restored": (
                policy["ok"] and policy["output"] == restart_before.get(host)
            ),
            "errors": [
                item["error"] for item in (remove, reload_result, start, absent, policy)
                if not item["ok"]
            ],
        }
    return results
```

- [ ] **Step 4: Add record construction and artifact writing**

```python
def new_record():
    acceptance = {name: False for name in LOCAL_ACCEPTANCE}
    acceptance.update({"platform_verify": None, "post_build_gate": None})
    return {
        "run_id": RUN_ID,
        "cluster": CLUSTER_NAME,
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outcome": OUTCOME_UNRESOLVED,
        "contract": {"expected": CONTRACT, "observed": OUTCOME_UNRESOLVED, "match": False},
        "acceptance": acceptance,
        "errors": [],
        "versions": {},
        "topology": {
            "vip": VIP, "app_user": APP_USER, "galera": GALERA_ADDR, "proxysql": PROXYSQL_ADDR,
            "writer_hostgroup": WRITER_HG, "offline_hostgroup": OFFLINE_HG,
        },
        "baseline": {},
        "failure": {},
        "cleanup": {"nodes": {}, "credential_profile_absent": False},
        "recovery": {},
        "external_gates": {
            "platform_verify": {"ok": None, "command": "make platform-verify", "rc": None},
            "post_build_gate": {
                "ok": None,
                "command": "make lab-post-build-gate CLUSTER=newclaude16-r9",
                "rc": None,
            },
        },
    }


def write_artifact(record):
    path = Path(f"/var/tmp/quorum-evidence-{EXPECTED_CLUSTER}-{RUN_ID}.json")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path
```

- [ ] **Step 5: Replace the old mutation body with a single recoverable lifecycle**

Implement `run_measurement(record)` with this exact control flow; all remote readers use Task 2 collectors, which raise `EvidenceError`, while the `finally` block uses only non-raising `safe_sh` via `cleanup_nodes`:

```python
def run_measurement(record):
    errors = record["errors"]
    cleanup_hosts = tuple(STOPPED)  # established before the first mutation
    restart_before = {}
    confirmed_dead = []

    # Fail-closed baseline: every item must exist before SIGKILL.
    try:
        record["versions"] = collect_versions()
        baseline_states = {host: galera_state(host) for host in GALERA}
        if not recovery_complete(baseline_states, NODES_EXPECTED):
            raise EvidenceError(f"baseline Galera state is not Primary/3/Synced: {baseline_states}")
        holder = vip_holder()
        runtime_by_proxy = {host: runtime_snapshot(host) for host in PROXYSQL_HOSTS}
        writer_rows = {
            host: [row for row in rows if row["hostgroup_id"] == WRITER_HG and row["status"] == "ONLINE"]
            for host, rows in runtime_by_proxy.items()
        }
        if any(len(rows) != 1 for rows in writer_rows.values()):
            raise EvidenceError(f"baseline writer placement is not exactly one per proxy: {writer_rows}")
        writer_addresses = {rows[0]["hostname"] for rows in writer_rows.values()}
        if len(writer_addresses) != 1 or not writer_addresses.issubset(set(GALERA_ADDR.values())):
            raise EvidenceError(f"ProxySQL pair disagrees on a canonical Galera writer: {writer_rows}")
        for host in cleanup_hosts:
            restart_before[host] = must_output(
                host, "systemctl show mariadb -p Restart --value", "baseline Restart policy", timeout=30
            )
        setup = app_setup()
        if not setup["ok"]:
            raise EvidenceError(f"app table setup failed: {setup['error']}")
        baseline_write = app_write()
        if not baseline_write["ok"]:
            raise EvidenceError(f"baseline app write failed: {baseline_write['error']}")
        record["baseline"] = {
            "vip_holder": holder,
            "galera": baseline_states,
            "runtime_writer": {host: rows[0] for host, rows in writer_rows.items()},
            "restart_policy": restart_before,
            "app_write_ok": True,
        }
        record["acceptance"]["baseline_complete"] = True
    except (EvidenceError, ValueError) as exc:
        errors.append(f"baseline: {exc}")
        return record

    try:
        for host in cleanup_hosts:
            arm_node(host)
            confirmed_dead.append(host)
        record["acceptance"]["processes_dead"] = confirmed_dead == list(cleanup_hosts)

        deadline = time.monotonic() + 120
        survivor_status = "?"
        while time.monotonic() < deadline:
            try:
                survivor_status = galera_state(SURVIVOR).split("/", 1)[0]
            except (EvidenceError, ValueError):
                survivor_status = "?"
            if survivor_status == "non-Primary":
                break
            time.sleep(3)
        if survivor_status != "non-Primary":
            raise EvidenceError(f"survivor did not reach non-Primary: {survivor_status}")
        record["acceptance"]["survivor_non_primary"] = True

        holder_before = vip_holder()
        mark = log_mark(holder_before)
        window_start_us = int(time.time() * 1_000_000)
        window_started = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        app_result = app_write()
        direct_result = safe_sh(
            SURVIVOR,
            "timeout 20 mariadb --socket=/var/lib/mysql/mysql.sock isa_test "
            "-e \"INSERT INTO app_degradation () VALUES ()\" 2>&1",
            timeout=30,
        )

        # Zachowaj oba wyniki ZANIM kolejne zrodlo dowodu zdazy zawiesc.
        app_code, app_state = parse_client_error(app_result["output"] or app_result["error"])
        node_code, node_state = parse_client_error(direct_result["output"] or direct_result["error"])
        outcome = classify_outcome(app_code, app_state, node_code, node_state)
        record["outcome"] = outcome
        record["contract"] = {"expected": CONTRACT, "observed": outcome, "match": outcome == CONTRACT}
        record["acceptance"].update({
            "vip_write_rejected": not app_result["ok"],
            "direct_write_rejected": not direct_result["ok"],
            "backend_error_exact": (node_code, node_state) == ("1047", "08S01"),
            "classification_resolved": outcome in (OUTCOME_DEGRADED, OUTCOME_CLEAN),
        })
        record["failure"] = {
            "window_started_utc": window_started,
            "window_ended_utc": None,
            "window_start_us": window_start_us,
            "survivor": SURVIVOR,
            "stopped": confirmed_dead,
            "survivor_status": survivor_status,
            "app_code": app_code,
            "app_sqlstate": app_state,
            "app_error": app_result["output"] or app_result["error"],
            "node_code": node_code,
            "node_sqlstate": node_state,
            "node_error": direct_result["output"] or direct_result["error"],
            "monitor_row": None,
            "runtime_survivor": None,
            "proxysql_node": holder_before,
            "proxysql_log": None,
            "log_mark": mark,
        }
        if app_result["ok"]:
            errors.append("critical: application write succeeded without quorum")
        if direct_result["ok"]:
            errors.append("critical: direct backend write succeeded without quorum")
        if outcome == OUTCOME_UNRESOLVED:
            errors.append(f"classification unresolved: app={app_code}/{app_state}, backend={node_code}/{node_state}")

        # Monitor moze odswiezac sie po zapytaniu z opoznieniem. Czekamy na
        # pierwsza najnowsza probke, ktorej epoch-us nalezy do TEGO okna.
        monitor = None
        monitor_error = ""
        monitor_deadline = time.monotonic() + 30
        while time.monotonic() < monitor_deadline:
            try:
                candidate = monitor_row(holder_before, GALERA_ADDR[SURVIVOR])
                if candidate["time_start_us"] >= window_start_us:
                    monitor = candidate
                    break
                monitor_error = (
                    f"latest row still predates window: {candidate['time_start_us']} < {window_start_us}"
                )
            except (EvidenceError, ValueError) as exc:
                monitor_error = str(exc)
            time.sleep(1)
        if monitor is None:
            raise EvidenceError(f"no post-window-start monitor row: {monitor_error}")
        if monitor["primary_partition"] != "NO":
            raise EvidenceError(f"monitor row does not prove quorum loss: {monitor}")

        runtime_rows = runtime_snapshot(holder_before)
        survivor_rows = [row for row in runtime_rows if row["hostname"] == GALERA_ADDR[SURVIVOR]]
        if len(survivor_rows) != 1:
            raise EvidenceError(f"expected one runtime row for survivor, got {survivor_rows}")
        runtime_survivor = survivor_rows[0]
        if runtime_survivor["hostgroup_id"] not in (WRITER_HG, OFFLINE_HG):
            raise EvidenceError(f"survivor is outside tenant writer/offline groups: {runtime_survivor}")

        log_text = log_delta(holder_before, mark)
        if not proxy_log_proves_backend_error(log_text, GALERA_ADDR[SURVIVOR]):
            raise EvidenceError("bounded ProxySQL log delta lacks survivor-correlated backend 1047/WSREP")
        holder_after = vip_holder()
        if holder_after != holder_before:
            raise EvidenceError(f"VIP holder changed inside evidence window: {holder_before} -> {holder_after}")

        window_ended = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record["failure"].update({
            "window_ended_utc": window_ended,
            "monitor_row": monitor,
            "runtime_survivor": runtime_survivor,
            "proxysql_log": log_text,
        })
        record["acceptance"].update({
            "same_window_monitor": True,
            "exact_runtime_placement": True,
            "bounded_correlated_log": True,
        })
    except (EvidenceError, ValueError) as exc:
        errors.append(f"measurement: {exc}")
    except Exception as exc:  # noqa: BLE001 — cleanup/recovery precede failure return.
        errors.append(f"unexpected measurement error {type(exc).__name__}: {exc}")
    except BaseException as exc:  # KeyboardInterrupt/SystemExit: najpierw cleanup + recovery.
        errors.append(f"measurement interrupted by {type(exc).__name__}: {exc}")
    finally:
        cleanup = cleanup_nodes(cleanup_hosts, restart_before)
        record["cleanup"]["nodes"] = cleanup
        record["acceptance"]["dropins_absent"] = all(
            item["dropin_absent"] for item in cleanup.values()
        )
        record["acceptance"]["restart_policy_restored"] = all(
            item["restart_policy_restored"] for item in cleanup.values()
        )
        for host, item in cleanup.items():
            errors.extend(f"cleanup {host}: {error}" for error in item["errors"])

    # Recovery readers are fail-soft: unknown state is recorded and retried to deadline.
    deadline = time.monotonic() + 240
    recovered_states = {}
    app_recovered = False
    while time.monotonic() < deadline:
        recovered_states = {}
        for host in GALERA:
            try:
                recovered_states[host] = galera_state(host)
            except (EvidenceError, ValueError) as exc:
                recovered_states[host] = f"UNKNOWN: {exc}"
        if recovery_complete(recovered_states, NODES_EXPECTED):
            app_recovered = app_write()["ok"]
            if app_recovered:
                break
        time.sleep(5)
    record["recovery"] = {"nodes": recovered_states, "app_write_ok": app_recovered}
    record["acceptance"]["nodes_primary_synced"] = recovery_complete(recovered_states, NODES_EXPECTED)
    record["acceptance"]["app_recovered"] = app_recovered
    if not record["acceptance"]["nodes_primary_synced"]:
        errors.append(f"recovery: cluster did not return Primary/3/Synced: {recovered_states}")
    if not app_recovered:
        errors.append("recovery: application write did not resume")
    return record
```

- [ ] **Step 6: Replace `main()` so artifact creation follows credential verification**

```python
def main():
    guard_errors = validate_target(
        CLUSTER_NAME,
        CONFIG_PATH,
        INVENTORY,
        GALERA,
        PROXYSQL_HOSTS,
        [APP_HOST] if APP_HOST else [],
    )
    if not APP_PW:
        guard_errors.append("APP_DB_PASSWORD is missing")
    if guard_errors:
        for error in guard_errors:
            print(f"REFUSED: {error}")
        return 1

    record = new_record()
    record["acceptance"]["target_guard"] = True
    copy_attempted = False
    try:
        copy_attempted = True
        install_app_profile()
        record = run_measurement(record)
    except (EvidenceError, OSError, subprocess.TimeoutExpired) as exc:
        record["errors"].append(f"credential/setup: {exc}")
    except Exception as exc:  # noqa: BLE001 — zachowaj artifact po nieoczekiwanym bledzie.
        record["errors"].append(f"unexpected setup/measurement error {type(exc).__name__}: {exc}")
    except BaseException as exc:
        record["errors"].append(f"main interrupted by {type(exc).__name__}: {exc}")
    finally:
        credential_cleanup = remove_app_profile() if copy_attempted else {"absent": True, "history": []}
        record["cleanup"]["credential_profile_absent"] = credential_cleanup["absent"]
        record["cleanup"]["credential_history"] = credential_cleanup["history"]
        record["acceptance"]["credential_profile_absent"] = credential_cleanup["absent"]
        if not credential_cleanup["absent"]:
            record["errors"].append(f"credential profile remains or is unknown on {APP_HOST}")

    artifact = write_artifact(record)  # after credential absence verification
    local_failures = acceptance_failures(record, final=False)
    if local_failures:
        print(f"FAIL: {local_failures}")
        exit_code = 1
    elif not record["contract"]["match"]:
        print(f"CONTRACT_MISMATCH: expected {CONTRACT}, observed {record['outcome']}")
        exit_code = 3
    else:
        print(f"PASS: locally accepted {record['outcome']} measurement")
        exit_code = 0
    print(f"ARTEFAKT: {artifact}")  # final output line, exact current path
    return exit_code
```

The existing entrypoint remains `sys.exit(main())`. Remove every `render_record()` and `render_issue_draft()` call from the harness.

- [ ] **Step 7: Prove fault handling and full unit green**

Run:

```bash
python3 -m unittest tests.unit.test_quorum_degradation_harness -v
python3 -m unittest discover -s tests/unit -p 'test_*.py' -v
python3 -m pyflakes tests/lab/chaos-app-degradation.py tests/lab/_quorum_evidence.py tests/unit/test_quorum_degradation_harness.py
bash tests/validation/probe-no-secrets-leak.sh
```

Expected: all tests `ok`; pyflakes silent; secret guard PASS.

- [ ] **Step 8: Commit**

```bash
git add tests/lab/chaos-app-degradation.py tests/unit/test_quorum_degradation_harness.py
git commit -m "fix(lab): make quorum mutation cleanup and recovery unconditional"
```

---

### Task 4: Confirmed operator surface with exact run identity

**Files:**
- Modify: `Makefile` target `lab-app-degradation-test`
- Modify: `README.md`

**Interfaces:**
- Consumes: exported `APP_DB_PASSWORD`, `CLUSTER=newclaude16-r9`, `CONFIRM=yes`, and caller-generated `QUORUM_RUN_ID`.
- Produces: unchanged target name and exact artifact naming contract.

- [ ] **Step 1: Replace the Make target**

```makefile
lab-app-degradation-test:  ## P2 quorum loss (TYLKO newclaude16-r9, destrukcyjny; CONFIRM=yes)
	$(cluster_guard)
	@: "$${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
	@: "$${QUORUM_RUN_ID:?Ustaw unikalny QUORUM_RUN_ID (32 hex)}"
	@test "$(CLUSTER)" = "newclaude16-r9" || (echo "P2 jest przypiety do CLUSTER=newclaude16-r9"; exit 1)
	@test "$(CONFIRM)" = "yes" || (echo "Wymaga CONFIRM=yes (SIGKILL na n16g2/n16g3)"; exit 1)
	$(TARGET_ENV) APP_DB_PASSWORD="$${APP_DB_PASSWORD}" \
	  QUORUM_RUN_ID="$${QUORUM_RUN_ID}" \
	  APP_QUORUM_ERROR_CONTRACT="$${APP_QUORUM_ERROR_CONTRACT:-degraded}" \
	  tests/lab/chaos-app-degradation.py
```

- [ ] **Step 2: Add exact README invocation**

```markdown
# P2: jeden destrukcyjny pomiar utraty kworum na przypietym newclaude16-r9.
# APP_DB_PASSWORD musi byc wyeksportowane; run ID generuje operator:
run_id="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
QUORUM_RUN_ID="$run_id" make lab-app-degradation-test \
  CLUSTER=newclaude16-r9 CONFIRM=yes
# Artefakt: /var/tmp/quorum-evidence-newclaude16-r9-${run_id}.json
```

- [ ] **Step 3: Verify guards without remote mutation**

Run:

```bash
! make lab-app-degradation-test CLUSTER=newclaude16-r9 APP_DB_PASSWORD=x QUORUM_RUN_ID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
! make lab-app-degradation-test CLUSTER=finalclaude-r10 CONFIRM=yes APP_DB_PASSWORD=x QUORUM_RUN_ID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
make -n lab-app-degradation-test CLUSTER=newclaude16-r9 CONFIRM=yes APP_DB_PASSWORD=x QUORUM_RUN_ID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

Expected: first refusal requires `CONFIRM=yes`; second refusal pins n16; dry run shows exact environment and no password inside the Python/Ansible command argv.

- [ ] **Step 4: Commit**

```bash
git add Makefile README.md
git commit -m "chore(make): pin and identify the P2 quorum measurement"
```

---

### Task 5: Execute one run and append external gates to the exact artifact

**Files:** no tracked file changes.

**Preconditions:** operator exports real `APP_DB_PASSWORD` and `PMM_ADMIN_PASSWORD`. No password appears on the command line.

- [ ] **Step 1: Fail-closed preflight**

Run:

```bash
make lab-galera-verify CLUSTER=newclaude16-r9
make lab-proxysql-verify CLUSTER=newclaude16-r9
make lab-app-verify CLUSTER=newclaude16-r9
```

Expected: three PASS results. Stop before mutation on any failure.

- [ ] **Step 2: Generate one run ID, persist its exact path, and run once**

Run in one shell:

```bash
run_id="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
artifact="/var/tmp/quorum-evidence-newclaude16-r9-${run_id}.json"
printf '%s\n' "$artifact" > /var/tmp/p2-current-artifact-path
set +e
QUORUM_RUN_ID="$run_id" make lab-app-degradation-test CLUSTER=newclaude16-r9 CONFIRM=yes
measurement_rc=$?
set -e
printf 'measurement_rc=%s artifact=%s\n' "$measurement_rc" "$artifact"
test -f "$artifact"
```

Expected final harness line exactly equals the path stored in `/var/tmp/p2-current-artifact-path`. Exit `3` is allowed only for an otherwise accepted `clean` result under old `degraded`; exit `1` means acceptance failure and never permits cutover/issue filing.

- [ ] **Step 3: Run both recovery gates unconditionally and append their exact results atomically**

Run:

```bash
python3 - <<'PY'
import json
import os
import subprocess
from pathlib import Path

artifact = Path("/var/tmp/p2-current-artifact-path").read_text(encoding="utf-8").strip()
path = Path(artifact)
assert path.exists(), f"current artifact missing: {path}"
record = json.loads(path.read_text(encoding="utf-8"))
expected = f"/var/tmp/quorum-evidence-newclaude16-r9-{record['run_id']}.json"
assert str(path) == expected, (path, expected)

commands = {
    "platform_verify": ["make", "platform-verify"],
    "post_build_gate": ["make", "lab-post-build-gate", "CLUSTER=newclaude16-r9"],
}
for name, command in commands.items():
    result = subprocess.run(command, env=os.environ.copy())
    ok = result.returncode == 0
    record["external_gates"][name] = {"ok": ok, "command": " ".join(command), "rc": result.returncode}
    record["acceptance"][name] = ok
    if not ok:
        record["errors"].append(f"external gate {name} failed rc={result.returncode}")

temporary = path.with_suffix(".json.gates.tmp")
temporary.write_text(json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
print(path)
print("external_gate_failures:", [
    name for name, result in record["external_gates"].items() if result["ok"] is not True
])
PY
```

Expected: both Make targets PASS; the same artifact now has `platform_verify=true` and `post_build_gate=true`.

- [ ] **Step 4: Inspect the complete artifact without hiding an incomplete run**

```bash
python3 - <<'PY'
import json
import sys
from pathlib import Path

repo = Path.cwd()
sys.path.insert(0, str(repo / "tests" / "lab"))
import _quorum_evidence as qe  # noqa: E402

path = Path(Path("/var/tmp/p2-current-artifact-path").read_text(encoding="utf-8").strip())
record = json.loads(path.read_text(encoding="utf-8"))
assert str(path) == f"/var/tmp/quorum-evidence-newclaude16-r9-{record['run_id']}.json"
problems = qe.acceptance_failures(record, final=True)
print(path)
print("outcome:", record["outcome"])
print("contract:", record["contract"])
print("final_acceptance_problems:", problems)
PY
```

Expected for a complete measurement: empty `final_acceptance_problems` and accepted `degraded` or `clean`. A non-empty list is still preserved as the measured result and proceeds only to Task 6 record generation; it blocks issue generation and clean-contract cutover.

- [ ] **Step 5: No commit**

This task changes no tracked file.

---

### Task 6: Generate the record, close the contract, and prepare conditional public actions

**Files:**
- Create: `docs/records/2026-08-21-p2-quorum-degradation-measurement.md`
- Conditional `clean` modifications: `tests/lab/chaos-app-degradation.py`, `Makefile`, `tests/lab/probe-app-conformance.py`, `tests/lab/probe-proxysql.py`

- [ ] **Step 1: Generate the durable record only from the exact final artifact**

```bash
python3 - <<'PY'
import json
import subprocess
import sys
from pathlib import Path

repo = Path.cwd()
sys.path.insert(0, str(repo / "tests" / "lab"))
import _quorum_evidence as qe  # noqa: E402

artifact = Path(Path("/var/tmp/p2-current-artifact-path").read_text(encoding="utf-8").strip())
record = json.loads(artifact.read_text(encoding="utf-8"))
problems = qe.acceptance_failures(record, final=True)
commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
context = f"""
## Kontekst wykonania

- artifact: `{artifact}`
- run ID: `{record['run_id']}`
- commit harnessu: `{commit}`
- pomiar: `QUORUM_RUN_ID={record['run_id']} make lab-app-degradation-test CLUSTER=newclaude16-r9 CONFIRM=yes`
- platform recovery gate: `make platform-verify`
- tenant recovery gate: `make lab-post-build-gate CLUSTER=newclaude16-r9`
- final acceptance problems: `{problems}`
"""
destination = repo / "docs/records/2026-08-21-p2-quorum-degradation-measurement.md"
destination.write_text(qe.render_record(record) + context, encoding="utf-8")
print(destination)
PY
```

Expected: the record is generated for complete, unresolved, and failed runs. It contains every acceptance flag, all sanitized error reasons, versions, exact window, monitor timestamp when available, measured runtime tuple when available, byte-log evidence when available, credential cleanup, local recovery, and both external gates.

- [ ] **Step 2: For accepted `degraded`, generate a filing-ready draft — do not publish yet**

```bash
python3 - <<'PY'
import json
import sys
from pathlib import Path

repo = Path.cwd()
sys.path.insert(0, str(repo / "tests" / "lab"))
import _quorum_evidence as qe  # noqa: E402

artifact = Path(Path("/var/tmp/p2-current-artifact-path").read_text(encoding="utf-8").strip())
record = json.loads(artifact.read_text(encoding="utf-8"))
if record["outcome"] != "degraded":
    print("SKIP: outcome is not degraded")
    raise SystemExit(0)
draft = qe.render_issue_draft(record)
for leaked in ("192.168.1.", "n16g", "fcp", "app_user_n16", "newclaude16-r9"):
    assert leaked not in draft, leaked
path = Path("/var/tmp/proxysql-p2-quorum-degradation-issue.md")
path.write_text(draft, encoding="utf-8")
print(path)
PY
```

Expected for `degraded`: one sanitized draft after all acceptance checks. Present it to the operator. Run no issue-publication command without explicit instruction; if the operator authorizes and returns a URL, add the URL to the record before committing it.

- [ ] **Step 3: For accepted `clean`, prove the only mismatch is the old default, then cut over every caller**

First gate the cutover:

```bash
python3 - <<'PY'
import json
import sys
from pathlib import Path

repo = Path.cwd()
sys.path.insert(0, str(repo / "tests" / "lab"))
import _quorum_evidence as qe  # noqa: E402

artifact = Path(Path("/var/tmp/p2-current-artifact-path").read_text(encoding="utf-8").strip())
record = json.loads(artifact.read_text(encoding="utf-8"))
assert record["outcome"] == "clean"
assert record["contract"] == {"expected": "degraded", "observed": "clean", "match": False}
assert qe.acceptance_failures(record, final=True) == []
PY
```

Then change both effective defaults:

```python
# tests/lab/chaos-app-degradation.py
CONTRACT = os.environ.get("APP_QUORUM_ERROR_CONTRACT", "clean")
```

```makefile
# Makefile target lab-app-degradation-test
	  APP_QUORUM_ERROR_CONTRACT="$${APP_QUORUM_ERROR_CONTRACT:-clean}" \
```

Replace `tests/lab/probe-app-conformance.py:15-21` with:

```text
  * historycznie utrata kworum dawala przez VIP "ERROR 2027 malformed packet",
    gdy backend zwracal "ERROR 1047 (08S01)". Aktualny pomiar P2 nie odtworzyl
    bledu protokolu: ProxySQL zwrocil backendowy blad bazy albo czysty blad
    polaczenia. Dokladny wynik, wersje i bramki recovery sa w
    docs/records/2026-08-21-p2-quorum-degradation-measurement.md; destrukcyjna
    sonda chaos-app-degradation.py egzekwuje odtad kontrakt `clean`.
```

Replace the historical/current paragraph at `tests/lab/probe-proxysql.py:101-118` with:

```python
    # Historia: na newclaude8-r9 klient przez VIP dostawal ERROR 2027, gdy ten
    # sam backend zwracal ERROR 1047 (08S01). Aktualny pomiar P2 jest zapisany w
    # docs/records/2026-08-21-p2-quorum-degradation-measurement.md i nie
    # odtworzyl bledu protokolu. Niezalezny fakt routingowy pozostaje zmierzony:
    # podczas totalnej utraty kworum ProxySQL moze zostawic ostatni wezel ONLINE
    # w hostgrupie writera. Ponizszy steady-state guard nadal wymaga, by kazdy
    # routowany backend byl Primary + Synced.
```

- [ ] **Step 4: Verify conditional changes and commit locally**

Run for every outcome:

```bash
python3 -m unittest discover -s tests/unit -p 'test_*.py' -v
python3 -m pyflakes tests/lab/chaos-app-degradation.py tests/lab/_quorum_evidence.py tests/lab/probe-app-conformance.py tests/lab/probe-proxysql.py
bash tests/validation/probe-no-secrets-leak.sh
git diff --check
```

Expected: unit suite green, no pyflakes output, secret guard PASS, no whitespace errors.

Commit the record:

```bash
git add docs/records/2026-08-21-p2-quorum-degradation-measurement.md
git commit -m "docs(records): record application behavior during quorum loss"
```

Only for accepted `clean`:

```bash
git add Makefile tests/lab/chaos-app-degradation.py tests/lab/probe-app-conformance.py tests/lab/probe-proxysql.py
git commit -m "fix(lab): enforce clean quorum-loss contract after measurement"
```

- [ ] **Step 5: Stop before remote publication and ask the operator**

Present:
- branch name and local commit SHAs;
- final verification results;
- for `degraded`, path to `/var/tmp/proxysql-p2-quorum-degradation-issue.md`;
- two explicit choices: authorize branch push/PR, and separately authorize upstream issue publication.

Do not run `git push`, `gh pr create`, or `gh issue create` without the corresponding explicit instruction.

---

## Acceptance criteria mapped to tasks

| Spec criterion | Enforced by |
| --- | --- |
| canonical n16 lab target and explicit confirmation | Task 2 `validate_target`; Task 4 Make guards |
| complete version/state/writer baseline and successful app write | Task 2 collectors; Task 3 baseline block |
| two processes proven dead after effective `Restart=no` | Task 3 `arm_node` |
| survivor proven `non-Primary` | Task 3 bounded poll |
| both writes rejected; exact errors classified | Task 1 classifier; Task 3 failure window |
| monitor row belongs to current window | Task 2 `time_start_us`; Task 3 `>= window_start_us` |
| exact runtime placement measured | Task 2 parsed rows; Task 3 single survivor tuple |
| bounded relevant ProxySQL log delta | Task 2 inode/offset collector; Task 1 correlation predicate |
| per-host cleanup continues after failures | Task 3 `cleanup_nodes` fault tests |
| drop-ins absent and prior restart policy restored | Task 3 cleanup result + acceptance flags |
| all nodes Primary/Synced and app recovered | Task 3 fail-soft recovery poll |
| temporary credential absent before artifact | Task 2 removal retries; Task 3 `main` ordering |
| platform and full n16 gates pass | Task 5 exact-artifact augmentation |
| clean/degraded/unresolved without inference | Task 1 classifier and acceptance evaluator |
| unresolved record names missing criteria | Task 1 `render_record` acceptance/error sections |
| issue draft only after every criterion passes | Task 1 renderer guard; Task 5 gates; Task 6 generation |
| no unauthorized remote/public action | Task 6 explicit stop and operator gates |
