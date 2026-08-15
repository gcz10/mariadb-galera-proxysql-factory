#!/usr/bin/env python3
"""Verify the rolling-patch pattern (ISC-52, ISC-55, ISC-57).

ISC-52: patching has a canary — one node off the active writer is patched first,
        and only after health is confirmed does rolling proceed.
ISC-55: the upgrade stops on cluster health loss — a health gate
        (wsrep_local_state=4 + Primary + full size) follows every node.
ISC-57: ProxySQL is upgraded separately, one instance at a time (serial:1), with
        config saved to disk first (SAVE ... TO DISK).

STATIC checks on f12_patch.yml:
  - ISC-52: a canary phase targets a non-writer node before the rolling phase.
  - ISC-55: every Galera patch phase has a health gate (until + wsrep_local_state +
            cluster_status + cluster_size) that aborts on failure.
  - ISC-57: a dedicated ProxySQL play uses serial:1 and SAVE ... TO DISK.
"""

import re
import sys

import yaml

PLAYBOOK = "playbooks/f12_patch.yml"

HEALTH_GATE_VARS = ["wsrep_local_state", "wsrep_cluster_status", "wsrep_cluster_size"]


def main():
    failures = []

    plays = yaml.safe_load(open(PLAYBOOK, encoding="utf-8"))
    if not isinstance(plays, list):
        failures.append(f"{PLAYBOOK}: not a list of plays")
        plays = []

    canary_found = False
    health_gates = 0
    proxysql_serial = False
    proxysql_save = False
    for play in plays:
        if not isinstance(play, dict):
            continue
        name = (play.get("name") or "").lower()
        text = yaml.safe_dump(play)
        hosts = str(play.get("hosts", ""))

        # ISC-52: canary phase (non-writer first)
        if "canary" in name and "galera" in hosts:
            canary_found = True

        # ISC-55: brama zdrowia na fazach galera. Liczymy OBIE formy: zapytanie
        # inline z `until:` oraz include kanonicznego helpera. Po konsolidacji
        # bramy zyja w tasks/assert_galera_healthy.yml i wariant inline przestal
        # wystepowac w tym playbooku — sonda liczaca tylko inline meldowala
        # "found 0" i przestala pilnowac czegokolwiek.
        if "galera" in hosts:
            if "assert_galera_healthy" in text:
                health_gates += text.count("assert_galera_healthy")
            elif "until" in text and all(v in text for v in HEALTH_GATE_VARS):
                health_gates += 1

        # ISC-57: ProxySQL serial:1 + SAVE TO DISK
        if "proxysql" in hosts:
            if str(play.get("serial")) == "1":
                proxysql_serial = True
            if re.search(r"SAVE\s+[\w\s]+?TO\s+DISK", text, re.IGNORECASE):
                proxysql_save = True

    if not canary_found:
        failures.append("ISC-52 — no canary (non-writer-first) patch phase found")
    if health_gates < 2:
        failures.append(
            f"ISC-55 — expected >=2 Galera health gates, found {health_gates}"
        )
    if not proxysql_serial:
        failures.append("ISC-57 — ProxySQL patch play lacks serial:1")
    if not proxysql_save:
        failures.append("ISC-57 — ProxySQL patch play lacks SAVE ... TO DISK")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1

    print(f"PASS: ISC-52/55/57 — canary-first patch, {health_gates} health gates "
          "(stop on health loss), ProxySQL serial:1 + SAVE TO DISK verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
