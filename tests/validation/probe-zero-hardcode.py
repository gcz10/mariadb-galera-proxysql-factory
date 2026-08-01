#!/usr/bin/env python3
"""Verify cluster portability — no hardcoded cluster data (ISC-59), and a new
cluster requires only a new clusters/<name>/ directory (ISC-58).

ISC-59: roles, playbooks and templates MUST NOT contain hardcoded cluster-specific
        data. Node names as a `hosts:`/`delegate_to:` target, and lab IPs / cluster
        names in any code line, break a second cluster.
ISC-58: a new cluster requires ONLY clusters/<name>/. The example-cluster template
        must exist; roles/playbooks must not reference a specific cluster directory.

Comments (#) and task `name:` fields are excluded for node-name checks (they're
documentation); lab IPs and cluster names are flagged in any non-comment line.
"""

import os
import re
import sys

SCAN_DIRS = ["playbooks", "roles"]
SKIP_DIRS = {"clusters", "tests", "docs", "versions", ".git", "node_modules"}

HARDCODE_PATTERNS = [
    (re.compile(r"172\.(28|29)\.0\.\d+"), "lab IP"),
    (re.compile(
        r"\b(gnode\d+|g9t?node\d+|pnode\d+|rnode\d+|r9t?node\d+"
        r"|galera\d+|infranode)\b"
    ), "lab node name"),
    (re.compile(r"\b(lab_galera|lab-galera|lab-cluster)\b"), "lab cluster name"),
]


def scan_file(path):
    findings = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                stripped = line.lstrip()
                is_comment = stripped.startswith("#")
                is_name = re.match(r'name:\s*["\']', stripped) is not None
                is_connection = bool(re.search(r'\b(hosts|delegate_to)\s*:', line))
                for pat, label in HARDCODE_PATTERNS:
                    for m in pat.finditer(line):
                        if label == "lab node name":
                            # node name only matters as a connection target
                            if is_comment or is_name or not is_connection:
                                continue
                        elif is_comment:
                            continue
                        findings.append((lineno, label, m.group(0), stripped[:80]))
    except OSError:
        pass
    return findings


def iter_files(extensions):
    for top in SCAN_DIRS:
        if not os.path.isdir(top):
            continue
        for root, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                if fname.endswith(extensions):
                    yield os.path.join(root, fname)


def main():
    failures = []

    # ISC-59: no hardcoded cluster data in roles/playbooks/templates
    hits = []
    for path in iter_files((".yml", ".yaml", ".j2", ".py", ".sh", ".cnf")):
        for lineno, label, match, snippet in scan_file(path):
            hits.append(f"{path}:{lineno} {label} {match!r}: {snippet}")
    if hits:
        failures.append("ISC-59 — hardcoded cluster data found in roles/playbooks:")
        failures.extend(f"  - {h}" for h in hits[:20])

    # ISC-58: example-cluster template exists
    for required in ("clusters/example-cluster/cluster.yml",
                     "clusters/example-cluster/inventory.yml"):
        if not os.path.exists(required):
            failures.append(f"ISC-58 — portable template missing: {required}")

    # ISC-58: roles/playbooks must not reference a specific cluster directory
    for path in iter_files((".yml", ".yaml", ".j2")):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "clusters/lab-cluster" in text:
            failures.append(
                f"ISC-58 — {path} references clusters/lab-cluster "
                f"(must use clusters/<name>/ via the CLUSTER var)"
            )

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1

    print("PASS: ISC-58/59 — factory portable: 0 hardcoded cluster data in "
          "roles/playbooks; example-cluster template present; new cluster = "
          "only clusters/<name>/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
