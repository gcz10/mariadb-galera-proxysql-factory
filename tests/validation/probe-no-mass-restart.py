#!/usr/bin/env python3
"""ISC-31 (Anti): no playbook restarts all Galera nodes simultaneously.

Static guard: any play that targets more than one Galera node AND performs a
mariadbd lifecycle action (start / stop / kill / restart / bootstrap) MUST set
`serial: 1`, so Galera nodes are only ever cycled one at a time. A mass restart
would drop the Primary Component and take the whole cluster down.

PASS: no offending play. FAIL: a multi-node Galera play cycles mariadbd without serial:1.
"""

import glob
import re
import sys

import yaml

# mariadbd lifecycle actions (start/stop/kill/bootstrap) — NOT read-only checks
# like `pgrep -x mariadbd`, which must not trip the guard.
LIFECYCLE = re.compile(
    r"--wsrep-new-cluster"
    r"|mariadbd\s+--user"
    r"|pkill[^\n]*\bmariadbd\b"
    r"|kill\s+-\d+[^\n]*\bmariadbd\b"
    r"|mariadb-admin\s+shutdown"
    r"|mysqladmin\s+shutdown",
    re.IGNORECASE,
)
# systemd/service restart or stop of a mariadb/galera unit
SERVICE_STATE = re.compile(r"state:\s*(restarted|stopped)", re.IGNORECASE)
MARIADB_UNIT = re.compile(r"name:\s*[\"']?maria", re.IGNORECASE)


def targets_multiple_galera(hosts):
    """True if the play can hit more than one Galera node."""
    s = str(hosts)
    if "galera" not in s and s.strip() != "all":
        return False
    # A single explicit index (galera[0], galera[2]) is exactly one node.
    if re.search(r"galera\[\d+\]\s*$", s):
        return False
    return True


def play_cycles_mariadbd(play):
    text = yaml.safe_dump(play)
    if LIFECYCLE.search(text):
        return True
    if SERVICE_STATE.search(text) and MARIADB_UNIT.search(text):
        return True
    return False


def main():
    violations = []
    for pb in sorted(glob.glob("playbooks/*.yml")):
        try:
            with open(pb, encoding="utf-8") as fh:
                plays = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            violations.append(f"{pb}: YAML parse error: {exc}")
            continue
        if not isinstance(plays, list):
            continue
        for play in plays:
            if not isinstance(play, dict) or "hosts" not in play:
                continue
            if targets_multiple_galera(play["hosts"]) and play_cycles_mariadbd(play):
                serial = play.get("serial")
                if str(serial) != "1":
                    violations.append(
                        f"{pb}: play '{play.get('name', '?')}' hosts={play['hosts']!r} "
                        f"cycles mariadbd but serial={serial!r} (must be 1)")

    if violations:
        print("FAIL: ISC-31 — playbook may restart multiple Galera nodes at once:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("PASS: ISC-31 — every multi-node Galera lifecycle play uses serial:1 "
          "(no simultaneous mass restart)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
