#!/usr/bin/env python3
"""Verify the f13_drift.yml static contract (ISC-21).

ISC-21: drift of significant configurations is detected. Per MASTER_PROMPT §18,
drift detection is READ-ONLY (reports, never auto-fixes in production).

This probe is STATIC: it parses playbooks/f13_drift.yml and checks the
playbook's contract without running Ansible and without contacting any host:
  1. ProxySQL MAIN vs DISK comparison is present for mysql_servers,
     mysql_galera_hostgroups, mysql_users — and runtime_* tables are NOT used
     (they carry dynamic status / galera-derived rows);
  2. a Galera cluster_state_uuid consistency check is present;
  3. no state-changing SQL is executed anywhere in the playbook: ProxySQL
     SAVE/LOAD admin-plane forms (including the multiword MYSQL SERVERS/USERS/
     VARIABLES and ADMIN VARIABLES variants) and DML (INSERT INTO / DELETE
     FROM / UPDATE ... SET).

The SQL scan walks the parsed YAML scalars verbatim — multiline
command/argv/stdin payloads included — instead of a YAML re-dump whose line
folding can split a command across lines. Before matching, line comments
(`#`, `-- ` — the same syntax in shell and SQL) and single-quoted string
literals inside read-only SELECT fragments are stripped, so a SELECT that
merely mentions a mutating verb as data, or a comment that mentions one, is
not flagged as an executed mutation.

CLUSTER_DRIFT_PLAYBOOK overrides the playbook path (test hook, same family as
CLUSTER_CONFIG / CLUSTER_INVENTORY).

Whether the lab actually PASSES f13_drift.yml at runtime, and whether an
injected unsaved change is detected there, is live evidence gathered
separately (ISA) — this probe cannot falsify that.
"""

import os
import re
import sys

import yaml

PLAYBOOK = os.environ.get("CLUSTER_DRIFT_PLAYBOOK", "playbooks/f13_drift.yml")

# must compare MAIN (memory) vs DISK — runtime_* carries dynamic data
EXPECTED_TABLE_PAIRS = [
    ("mysql_servers", "disk.mysql_servers"),
    ("mysql_galera_hostgroups", "disk.mysql_galera_hostgroups"),
    ("mysql_users", "disk.mysql_users"),
]

# ProxySQL admin-plane state changes plus plain DML. The multiword forms
# (SAVE/LOAD MYSQL SERVERS|USERS|VARIABLES, SAVE/LOAD ADMIN VARIABLES) need
# their destination/source word, which is what the one-word-only pattern this
# replaces silently missed.
MUTATING_SQL_RE = re.compile(
    r"""\b(?:
        (?:SAVE|LOAD)\s+
        (?:MYSQL\s+)?(?:SERVERS|USERS|VARIABLES|ADMIN\s+VARIABLES)\s+(?:TO|FROM)\s+\w+
        |INSERT\s+INTO\b
        |DELETE\s+FROM\b
        |UPDATE\s+\w+\s+SET\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# MySQL requires whitespace after `--` for a comment, so option-style
# arguments (mariadb --socket=...) survive; `#` comments need a word boundary
# in both shell and SQL.
LINE_COMMENT_RE = re.compile(r"(?:^|\s)(?:--\s|#)")
# '' inside a literal is an escaped quote.
SQL_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
# A read-only SELECT clause ends at the statement separator or the line;
# anything quoted inside it is data, not executed SQL.
SELECT_FRAGMENT_RE = re.compile(r"\bSELECT\b[^;\n]*", re.IGNORECASE)


def _iter_scalars(node):
    """Yield every string scalar of parsed YAML (mapping keys included)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_scalars(key)
            yield from _iter_scalars(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _iter_scalars(item)


def _scrub(scalar):
    """Drop comment noise and SELECT-fragment literals from one scalar."""
    lines = []
    for line in scalar.splitlines():
        match = LINE_COMMENT_RE.search(line)
        if match:
            line = line[: match.start()]
        lines.append(line)
    text = "\n".join(lines)
    return SELECT_FRAGMENT_RE.sub(
        lambda m: SQL_LITERAL_RE.sub(" ", m.group(0)), text
    )


def main():
    failures = []

    with open(PLAYBOOK, encoding="utf-8") as fh:
        plays = yaml.safe_load(fh)
    if not isinstance(plays, list):
        failures.append(f"{PLAYBOOK}: not a list of plays")
        plays = []

    scanned = "\n".join(_iter_scalars(plays))
    # must NOT use runtime_mysql_servers (dynamic) — must use main mysql_servers
    if re.search(r"runtime_mysql_servers\b", scanned):
        failures.append(
            "ISC-21 — drift check uses runtime_mysql_servers (dynamic status/derived "
            "rows); must compare mysql_servers (main) vs disk.mysql_servers"
        )
    for main_tbl, disk_tbl in EXPECTED_TABLE_PAIRS:
        if main_tbl not in scanned or disk_tbl not in scanned:
            failures.append(f"ISC-21 — drift check missing table pair {main_tbl} vs {disk_tbl}")
    if "wsrep_cluster_state_uuid" not in scanned:
        failures.append("ISC-21 — drift check missing Galera cluster_state_uuid consistency")
    # must be read-only: no SAVE/LOAD/DML that changes state
    for scalar in _iter_scalars(plays):
        match = MUTATING_SQL_RE.search(_scrub(scalar))
        if match:
            context = " ".join(scalar.split())[:80]
            failures.append(
                "ISC-21 — drift playbook performs a state-changing action (not read-only): "
                f"{match.group(0)!r} in {context!r}"
            )

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1

    print(
        "PASS: ISC-21 — f13_drift.yml static contract holds: ProxySQL main-vs-disk "
        "table pairs + Galera cluster_state_uuid check present; no state-changing SQL "
        "(SAVE/LOAD admin-plane incl. multiword forms, DML)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
