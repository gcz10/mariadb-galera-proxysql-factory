#!/usr/bin/env python3
"""ISC-65 (Anti): no two nodes are ever bootstrapped as independent Primary Components.

Static guard: any play that performs a Galera bootstrap (`mariadbd ... --wsrep-new-cluster`
in a shell/command task) MUST be single-host safe and reject a second Primary:
  - the play uses `serial: 1` (or targets a single explicit host),
  - the play requires an explicit `confirm` guard,
  - pre-tasks query `wsrep_cluster_status` on the Galera nodes and assert that no
    existing node reports `Primary`.

Bootstrapping two nodes simultaneously or bootstrapping while another Primary exists
would create independent Primary Components and split-brain.

Only shell/command task *args* are inspected — a task NAME mentioning "--wsrep-new-cluster"
(e.g. "join, bez --wsrep-new-cluster" = "without") is documentation, not a bootstrap.
"""

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]

# Dwa sposoby na wystawienie nowego Primary Component: surowa flaga mariadbd
# i wrapper systemd. Produkcyjna sciezka w bootstrap.yml uzywa wrappera, wiec
# sonda znajaca tylko flage przepuscilaby nowy playbook bootstrapujacy bez
# guardow — czyli dokladnie to, przed czym ISC-65 ma chronic.
BOOTSTRAP = re.compile(r"--wsrep-new-cluster|\bgalera_new_cluster\b")
COMMAND_MODULES = {
    "ansible.builtin.shell", "ansible.builtin.command", "shell", "command",
    "ansible.builtin.script", "script",
}
TASK_ATTRS = {
    "name", "when", "loop", "register", "changed_when", "failed_when", "no_log",
    "vars", "become", "delegate_to", "tags", "ignore_errors", "retries", "delay",
    "until", "with_items", "with_loop", "args", "environment", "run_once",
}


TASK_SECTIONS = ("tasks", "pre_tasks", "post_tasks", "handlers")


def _walk_tasks(items):
    """Rekursywne przejscie listy zadan, wchodzac w block/rescue/always.

    Bez tego bootstrap zagniezdzony w `block:` bylby niewidoczny dla sondy
    (wzorzec _walk_blocks z probe-no-conditional-env.py).
    """
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        if "block" in item:
            for sub_key in ("block", "rescue", "always"):
                if sub_key in item:
                    yield from _walk_tasks(item[sub_key])
            continue
        yield item


def has_bootstrap_action(play):
    """True if a shell/command task actually invokes --wsrep-new-cluster."""
    for section in TASK_SECTIONS:
        for task in _walk_tasks(play.get(section) or []):
            action = next((k for k in task if k not in TASK_ATTRS), None)
            if action in COMMAND_MODULES:
                raw = task[action]
                cmd = " ".join(raw) if isinstance(raw, list) else str(raw)
                if BOOTSTRAP.search(cmd):
                    return True
    return False


def has_existing_primary_guard(play):
    """True when pre-tasks probe wsrep state and reject an existing Primary."""
    has_probe = False
    has_anchored_classification = False
    has_assert = False

    for task in play.get("pre_tasks", []) or []:
        if not isinstance(task, dict):
            continue
        cmd = task.get("ansible.builtin.command") or task.get("command") or {}
        if "wsrep_cluster_status" in str(cmd):
            has_probe = True

        set_fact = task.get("ansible.builtin.set_fact") or task.get("set_fact") or {}
        live_primary = set_fact.get("bootstrap_live_primary", "")
        if "(?m)^wsrep_cluster_status" in live_primary and "Primary$" in live_primary:
            has_anchored_classification = True

        assert_task = task.get("ansible.builtin.assert") or task.get("assert") or {}
        that_list = str(assert_task.get("that", []))
        if "bootstrap_live_primary" in that_list and "== 0" in that_list:
            has_assert = True

    return has_probe and has_anchored_classification and has_assert
def is_single_host(hosts):
    s = str(hosts).strip()
    return bool(re.match(r"^(galera\[\d+\]|localhost|[a-z]+node\d+)$", s))


def main():
    violations = []
    bootstrap_plays = []
    playbooks = sorted((REPO / "playbooks").glob("*.yml"))
    if not playbooks:
        # Fail-closed: zero playbookow znaczy zly anchor, nie czysty repo.
        print(f"FAIL: ISC-65 — brak playbookow w {REPO / 'playbooks'} — "
              f"sonda nie miala czego sprawdzac")
        return 1

    for pb in playbooks:
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

            if not has_existing_primary_guard(play):
                violations.append(
                    f"{pb}: play '{play.get('name', '?')}' has no existing-Primary "
                    f"runtime guard (ISC-65)"
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

    if not bootstrap_plays:
        # Fail-closed: zero wykrytych bootstrap-play to slepa sonda albo brak
        # istniejacego bootstrapu — zadne z tego nie jest "bezpieczny repo".
        print("FAIL: ISC-65 — nie wykryto zadnej play bootstrapujacej — "
              "sonda nie widzi istniejacego bootstrapu (fail-closed)")
        return 1

    loc = ", ".join(f"{pb}::{p.get('name', '?')}" for pb, p in bootstrap_plays)
    print(f"PASS: ISC-65 — every bootstrap play is single-host-safe + confirm-gated "
          f"(bootstrap plays: {loc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
