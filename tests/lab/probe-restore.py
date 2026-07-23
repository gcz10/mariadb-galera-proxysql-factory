#!/usr/bin/env python3
"""Verify the restore drill state on the clean restore host.

Checks: ISC-36 (last restore drill restored to the isolated host and passed the
integrity check), ISC-37 (the last successful drill is within the configured
restore_test_schedule window).

Reads /var/lib/mariadb-backup-state/last_restore.json from the restore host.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import yaml

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")
STATE_FILE = "/var/lib/mariadb-backup-state/last_restore.json"

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER = yaml.safe_load(fh)

RESTORE_SCHEDULE = str(CLUSTER["backup"].get("restore_test_schedule", "0 4 * * 0"))


def schedule_max_age_days(cron):
    """Derive an allowed staleness window from a cron expression."""
    fields = cron.split()
    if len(fields) != 5:
        return 8
    _, _, dom, _, dow = fields
    if dow != "*":
        return 8      # weekly
    if dom != "*":
        return 32     # monthly
    return 2          # daily


def body(node, out):
    m = re.search(rf'^{re.escape(node)}\s*\|\s*\w+\s*\|\s*rc=\d+\s*>>?\s*$', out, re.M)
    return out[m.end():].strip() if m else out.strip()


def main():
    failures = []

    r = subprocess.run(
        [ANSIBLE, "restore", "-i", INVENTORY, "-m", "ansible.builtin.shell",
         "-a", f"cat {STATE_FILE}"],
        capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print("FAIL: ISC-36/37 — no restore drill state found "
              f"({STATE_FILE} missing; run the restore drill first)")
        return 1

    try:
        state = json.loads(body("rnode1", r.stdout))
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL: cannot parse restore state: {exc}")
        return 1

    # ISC-36: the drill restored and passed integrity.
    if state.get("status") != "success":
        failures.append(f"ISC-36 — last restore drill status={state.get('status')!r} (expected success)")
    if int(state.get("rows_verified", 0)) <= 0:
        failures.append(f"ISC-36 — restore verified {state.get('rows_verified')} rows (expected > 0)")

    # ISC-37: the drill is within the scheduled window.
    last = state.get("last_restore", "")
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400
        max_age = schedule_max_age_days(RESTORE_SCHEDULE)
        if age_days > max_age:
            failures.append(
                f"ISC-37 — last restore drill {age_days:.1f}d ago exceeds "
                f"schedule window {max_age}d ('{RESTORE_SCHEDULE}')")
    except (ValueError, AttributeError):
        failures.append(f"ISC-37 — invalid last_restore timestamp: {last!r}")
        age_days = None

    if failures:
        print("FAIL: restore drill verification failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"PASS: restore drill OK — {state['backup_name']} restored to isolated host, "
        f"{state['rows_verified']} rows verified, drill {age_days:.2f}d ago "
        f"(within '{RESTORE_SCHEDULE}' window)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
