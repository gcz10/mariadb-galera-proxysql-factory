#!/usr/bin/env python3
"""Verify the restore drill state on the clean restore host.

Checks: ISC-36 (last restore drill restored to the isolated host and passed the
integrity check), ISC-37 (the last successful drill is within the configured
restore_test_schedule window).

Reads /opt/galera-backup/clusters/<cluster>/state.json from the restore host.
"""

import json
import sys
from datetime import datetime, timezone

from _probe_common import ProbeContext, finish, require_hosts, run_ansible

CTX = ProbeContext()
CLUSTER = CTX.config
CLUSTER_NAME = CLUSTER["cluster"]["name"]
RESTORE_HOSTS = CTX.group_hosts("restore")
RESTORE_HOST = RESTORE_HOSTS[0] if RESTORE_HOSTS else ""
STATE_FILE = f"/opt/galera-backup/clusters/{CLUSTER_NAME}/state.json"
RESTORE_SCHEDULE = str(
    CLUSTER["backup"].get("restore_test_schedule", "0 4 * * 0")
)


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


def main():
    failures = []
    undetermined = []
    result = run_ansible(
        CTX,
        "restore",
        f"cat {STATE_FILE} 2>&1 || echo PROBE_CAT_FAILED",
        timeout=30,
    )
    require_hosts(
        result,
        RESTORE_HOSTS,
        "ISC-36/37 restore state",
        failures,
        undetermined,
    )
    if not RESTORE_HOST or RESTORE_HOST not in result.bodies:
        return finish(failures, undetermined, "")

    raw = result.body(RESTORE_HOST)
    if "PROBE_CAT_FAILED" in raw:
        failures.append(
            "ISC-36/37 — no restore drill state found "
            f"({STATE_FILE} missing; run the restore drill first)"
        )
        return finish(failures, undetermined, "")

    try:
        state = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        failures.append(f"cannot parse restore state: {exc}")
        return finish(failures, undetermined, "")
    if not isinstance(state, dict):
        failures.append("cannot parse restore state: top-level JSON is not an object")
        return finish(failures, undetermined, "")

    if state.get("cluster") != CLUSTER_NAME:
        failures.append(
            f"ISC-36 — state cluster {state.get('cluster')!r} != {CLUSTER_NAME!r}"
        )

    last_run = state.get("last_run", {})
    if last_run.get("command") != "restore":
        failures.append(
            f"ISC-36 — last run command={last_run.get('command')!r} "
            "(expected restore)"
        )
    if last_run.get("status") != "success":
        failures.append(
            f"ISC-36 — last restore drill status={last_run.get('status')!r} "
            "(expected success)"
        )

    last_success = state.get("last_success", {})
    if last_success.get("command") != "restore":
        failures.append(
            f"ISC-36 — last success command={last_success.get('command')!r} "
            "(expected restore)"
        )

    artifact = last_success.get("artifact", {})
    if isinstance(artifact, dict):
        rows_verified = artifact.get("rows_verified", 0)
        backup_name = artifact.get("backup_name", "unknown")
    else:
        rows_verified = 0
        backup_name = str(artifact)

    try:
        rows_count = int(rows_verified)
    except (TypeError, ValueError):
        rows_count = 0
    if rows_count <= 0:
        failures.append(
            f"ISC-36 — restore verified {rows_verified} rows (expected > 0)"
        )

    unixtime = last_success.get("unixtime", 0)
    if unixtime <= 0:
        failures.append("ISC-37 — missing or invalid last_success unixtime")
        age_days = None
    else:
        age_days = (datetime.now(timezone.utc).timestamp() - unixtime) / 86400
        max_age = schedule_max_age_days(RESTORE_SCHEDULE)
        if age_days > max_age:
            failures.append(
                f"ISC-37 — last restore drill {age_days:.1f}d ago exceeds "
                f"schedule window {max_age}d ('{RESTORE_SCHEDULE}')"
            )

    return finish(
        failures,
        undetermined,
        f"restore drill OK — {backup_name} restored to isolated host, "
        f"{rows_verified} rows verified, drill {age_days:.2f}d ago "
        f"(within '{RESTORE_SCHEDULE}' window)",
    )


if __name__ == "__main__":
    sys.exit(main())
