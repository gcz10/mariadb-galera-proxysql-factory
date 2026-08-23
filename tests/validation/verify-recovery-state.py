#!/usr/bin/env python3
"""Zweryfikuj, ze wybor wezla recovery pochodzi z BIEZACEGO przebiegu.

Wyjscie stdout jest kontraktem dla Makefile: przy sukcesie dokladnie jedna nazwa
wezla i newline. Kazda niejednoznacznosc idzie na stderr i konczy sie rc=1.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml


def refuse(message: str) -> int:
    print(f"REFUSED: {message}", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return refuse(
            "usage: verify-recovery-state.py <state.json> <run_id> <inventory.yml>"
        )

    state_path = Path(argv[1])
    expected_run_id = argv[2]
    inventory_path = Path(argv[3])
    if not expected_run_id:
        return refuse("expected run_id is empty")

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return refuse(f"state is not readable JSON: {exc}")
    if not isinstance(payload, dict):
        return refuse("state JSON must be an object")

    run_id = payload.get("run_id")
    generated_at = payload.get("generated_at")
    node = payload.get("node")
    if run_id != expected_run_id:
        return refuse("state run_id does not match the current recovery run_id")
    if not isinstance(generated_at, str) or not generated_at:
        return refuse("generated_at is missing or empty")
    try:
        timestamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return refuse("generated_at is not an ISO-8601 timestamp")
    if timestamp.tzinfo is None:
        return refuse("generated_at must include a timezone")
    if not isinstance(node, str) or not node:
        return refuse("node is missing or empty")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", node) is None:
        return refuse("node is not a safe inventory identifier")

    try:
        inventory = yaml.safe_load(inventory_path.read_text(encoding="utf-8")) or {}
        galera = inventory["all"]["children"]["galera"]["hosts"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        return refuse(f"inventory galera group is unreadable: {exc}")
    if not isinstance(galera, dict) or not galera:
        return refuse("inventory galera group is empty")
    if node not in galera:
        return refuse(f"node {node!r} is not in inventory galera group")

    print(node)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
