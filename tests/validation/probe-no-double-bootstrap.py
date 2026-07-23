#!/usr/bin/env python3
"""ISC-65 (Anti): no two nodes are ever bootstrapped as independent Primary Components.

Static guard: any play that performs a Galera bootstrap (`mariadbd ... --wsrep-new-cluster`
in a shell/command task) MUST be single-host safe, so it can never form two independent
Primary Components:
  - the play uses `serial: 1` (or targets a single explicit host like `galera[0]`/`gnode1`),
  - AND the play requires an explicit `confirm` guard (so bootstrap never runs silently).

A bootstrap that hits two+ nodes simultaneously (or two concurrent bootstrap plays)
would create two independent Primary Components → split-brain at cluster formation.

Only shell/command task *args* are inspected — a task NAME mentioning "--wsrep-new-cluster"
(e.g. "join, bez --wsrep-new-cluster" = "without") is documentation, not a bootstrap.
"""

import glob
import re
import sys

import yaml

BOOTSTRAP = re.compile(r"--wsrep-new-cluster")
COMMAND_MODULES = {
    "ansible.builtin.shell", "ansible.builtin.command", "shell", "command",
    "ansible.builtin.script", "script",
}
TASK_ATTRS = {
    "name", "when", "loop", "register", "changed_when", "failed_when", "no_log",
    "vars", "become", "delegate_to", "tags", "ignore_errors", "retries", "delay",
    "until", "with_items", "with_loop", "args", "environment", "run_once",
}


def has_bootstrap_action(play):
    """True if a shell/command task actually invokes --wsrep-new-cluster."""
    for task in play.get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        action = next((k for k in task if k not in TASK_ATTRS), None)
        if action in COMMAND_MODULES:
            raw = task[action]
            cmd = " ".join(raw) if isinstance(raw, list) else str(raw)
            if BOOTSTRAP.search(cmd):
                return True
    return False


def is_single_host(hosts):
    s = str(hosts).strip()
    return bool(re.match(r"^(galera\[\d+\]|localhost|[a-z]+node\d+)$", s))


def main():
    violations = []
    bootstrap_plays = []

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
            if not has_bootstrap_action(play):
                continue
            bootstrap_plays.append((pb, play))
            text = yaml.safe_dump(play)
            hosts = str(play.get("hosts", ""))
            serial = str(play.get("serial"))
            single_safe = serial == "1" or is_single_host(hosts)
            if not single_safe:
                violations.append(
                    f"{pb}: play '{play.get('name', '?')}' bootstraps (--wsrep-new-cluster) "
                    f"but hosts={hosts!r} serial={serial!r} — must be single-host-safe (ISC-65)"
                )
            if "confirm" not in text:
                violations.append(
                    f"{pb}: play '{play.get('name', '?')}' bootstraps without a "
                    f"confirm guard (ISC-65) — bootstrap must require -e confirm=yes"
                )

    bootstrap_files = {pb for pb, _ in bootstrap_plays}
    if len(bootstrap_files) > 1:
        violations.append(
            f"ISC-65 — multiple bootstrap playbooks found: {sorted(bootstrap_files)} "
            f"— consolidate to one to avoid two independent Primary Components"
        )

    if violations:
        print("FAIL: ISC-65 — bootstrap may create two independent Primary Components:")
        for v in violations:
            print(f"  - {v}")
        return 1

    loc = ", ".join(f"{pb}::{p.get('name', '?')}" for pb, p in bootstrap_plays) or "none"
    print(f"PASS: ISC-65 — every bootstrap play is single-host-safe + confirm-gated "
          f"(bootstrap plays: {loc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
