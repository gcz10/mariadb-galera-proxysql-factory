#!/usr/bin/env python3
"""Verify the redundant ProxySQL endpoint (Keepalived VIP).

Checks: ISC-24 (VIP assigned to exactly one ProxySQL node when healthy),
ISC-26 (the VIP holder's ProxySQL is actually running — VIP never sits on an
instance whose ProxySQL is down). ISC-25 (failover < RTO) is exercised by the
live failover test, not a steady-state probe.

Reads the endpoint address from cluster.yml.
"""

import os
import re
import subprocess
import sys
import yaml

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")
IFACE = os.environ.get("PROXYSQL_ENDPOINT_INTERFACE", "eth0")

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER_CONFIG = yaml.safe_load(fh)

VIP = CLUSTER_CONFIG["proxysql"]["endpoint"]["address"]


def run_ansible_query(nodes, script):
    """Run a shell snippet on nodes via ansible, return {node: body}."""
    cmd = [
        ANSIBLE, nodes, "-i", INVENTORY, "-m", "ansible.builtin.shell",
        "-a", script, "--fork", "5",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    data = {}
    current_host = None
    current_body = []
    for line in result.stdout.splitlines():
        header = re.match(r'^(\S+)\s*\|\s*\w+\s*\|\s*rc=\d+\s*>>?\s*$', line)
        if header:
            if current_host:
                data[current_host] = "\n".join(current_body).strip()
            current_host = header.group(1)
            current_body = []
        elif current_host:
            current_body.append(line)
    if current_host:
        data[current_host] = "\n".join(current_body).strip()
    return data


def check(condition, message, failures):
    if not condition:
        failures.append(message)


def main():
    failures = []

    # Per-node: does it hold the VIP? is ProxySQL running?
    probe = (
        f"if ip -o -4 addr show dev {IFACE} | grep -q '{VIP}/'; then echo VIP=1; else echo VIP=0; fi; "
        f"if pgrep -x proxysql >/dev/null; then echo PROXYSQL=1; else echo PROXYSQL=0; fi"
    )
    raw = run_ansible_query("proxysql", probe)
    if not raw:
        print("FAIL: no ProxySQL nodes responded to endpoint probe")
        return 1

    state = {}
    for node, body in raw.items():
        vals = dict(
            kv.split("=", 1) for kv in body.split() if "=" in kv
        )
        state[node] = {"vip": vals.get("VIP") == "1", "proxysql": vals.get("PROXYSQL") == "1"}

    vip_holders = [n for n, s in state.items() if s["vip"]]

    # ISC-24: exactly one node holds the VIP
    check(
        len(vip_holders) == 1,
        f"VIP {VIP} held by {len(vip_holders)} nodes {vip_holders} (expected exactly 1)",
        failures,
    )

    # ISC-26: the VIP holder's ProxySQL must be running
    for holder in vip_holders:
        check(
            state[holder]["proxysql"],
            f"{holder} holds VIP {VIP} but its ProxySQL is DOWN (ISC-26 violation)",
            failures,
        )

    if failures:
        print("FAIL: ProxySQL endpoint checks failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    holder = vip_holders[0]
    print(
        f"PASS: ProxySQL endpoint healthy — VIP {VIP} on {holder} "
        f"(ProxySQL running), {len(state)} node(s) evaluated"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
