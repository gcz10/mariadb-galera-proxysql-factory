#!/usr/bin/env python3
"""ISC-30/64: split-brain / network-partition test (lab-only, destructive).

Partitions one Galera node from the other two with iptables, then proves:
  - the majority partition (2/3) stays a single Primary Component and accepts writes,
  - the minority partition (1/3) goes non-Primary and REFUSES writes,
  - therefore there are never two independent writable Primaries (ISC-30).
  - ISC-64: refuses to run on the production profile.

The partition is always healed (try/finally) and the node rejoins the cluster.
"""

import os
import re
import subprocess
import sys
import time
import yaml

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER = yaml.safe_load(fh)
with open(INVENTORY, encoding="utf-8") as fh:
    INV = yaml.safe_load(fh)

ENVIRONMENT = CLUSTER["cluster"]["environment"]
GALERA = INV["all"]["children"]["galera"]["hosts"]
MINORITY = "gnode3"                       # node to isolate (1/3)
MAJORITY = [h for h in GALERA if h != MINORITY]   # 2/3 stays Primary
PARTITION_WAIT = 25                       # Galera evs.inactive_timeout margin


def sh(node, script, timeout=60, check=False):
    cmd = [ANSIBLE, node, "-i", INVENTORY, "-m", "ansible.builtin.shell", "-a", script]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"ansible {node} failed: {r.stdout}\n{r.stderr}")
    return r


def body(node, result):
    out = result.stdout
    m = re.search(rf'^{re.escape(node)}\s*\|\s*\w+\s*\|\s*rc=\d+\s*>>?\s*$', out, re.M)
    return out[m.end():].strip() if m else out.strip()


def wsrep(node, var):
    q = f"SHOW STATUS LIKE '{var}'"
    r = sh(node, f'mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "{q}"')
    parts = body(node, r).split("\t")
    return parts[1] if len(parts) == 2 else ""


def can_write(node, token):
    """Attempt a write on node; return True if it committed."""
    q = (f"CREATE TABLE IF NOT EXISTS isa_test.split_brain (id BIGINT PRIMARY KEY); "
         f"INSERT INTO isa_test.split_brain (id) VALUES ({token})")
    r = sh(node, f'timeout 8 mariadb --socket=/var/lib/mysql/mysql.sock -e "{q}"')
    return r.returncode == 0


def partition_rules(action):
    """action: 'I' to insert DROP rules, 'D' to delete them (heal)."""
    for peer in MAJORITY:
        ip = GALERA[peer]["galera_node_address"]
        sh(MINORITY, f"iptables -{action} INPUT -s {ip} -j DROP", check=False)
        sh(MINORITY, f"iptables -{action} OUTPUT -d {ip} -j DROP", check=False)


def main():
    failures = []

    if ENVIRONMENT == "production":
        print("REFUSED: chaos-split-brain is destructive and must not run on production (ISC-64)")
        return 1

    partitioned = False
    try:
        # Isolate the minority node from the majority.
        partition_rules("I")
        partitioned = True
        print(f"partitioned {MINORITY} from {MAJORITY}; waiting {PARTITION_WAIT}s for reconfig")
        time.sleep(PARTITION_WAIT)

        maj_status = wsrep(MAJORITY[0], "wsrep_cluster_status")
        maj_size = wsrep(MAJORITY[0], "wsrep_cluster_size")
        min_status = wsrep(MINORITY, "wsrep_cluster_status")
        min_size = wsrep(MINORITY, "wsrep_cluster_size")
        print(f"majority {MAJORITY[0]}: status={maj_status} size={maj_size}; "
              f"minority {MINORITY}: status={min_status} size={min_size}")

        token = int(time.time())
        maj_write = can_write(MAJORITY[0], token)
        min_write = can_write(MINORITY, token + 1)

        # Majority: single Primary of size 2, accepts writes.
        if maj_status != "Primary":
            failures.append(f"majority {MAJORITY[0]} status={maj_status} (expected Primary)")
        if maj_size != str(len(MAJORITY)):
            failures.append(f"majority size={maj_size} (expected {len(MAJORITY)})")
        if not maj_write:
            failures.append(f"majority {MAJORITY[0]} could NOT write while Primary")

        # Minority: non-Primary, refuses writes — no second writable Primary.
        if min_status == "Primary":
            failures.append(
                f"SPLIT-BRAIN: minority {MINORITY} is also Primary (two writable Primaries)")
        if min_write:
            failures.append(
                f"SPLIT-BRAIN: minority {MINORITY} accepted a write while partitioned (ISC-30)")

    finally:
        if partitioned:
            partition_rules("D")
            # Best-effort flush in case rule deletion missed anything.
            sh(MINORITY, "iptables -F", check=False)
            # Wait for the minority to rejoin the Primary Component.
            for _ in range(20):
                if wsrep(MINORITY, "wsrep_local_state_comment") == "Synced" and \
                   wsrep(MINORITY, "wsrep_cluster_size") == str(len(GALERA)):
                    break
                time.sleep(3)

    # Confirm the cluster healed back to full size, single Primary.
    healed_size = wsrep(MAJORITY[0], "wsrep_cluster_size")
    if healed_size != str(len(GALERA)):
        failures.append(f"cluster did not heal: size={healed_size} (expected {len(GALERA)})")

    if failures:
        print("FAIL: split-brain test failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"PASS: no split-brain — during partition only the majority ({MAJORITY[0]}, size 2) "
        f"was Primary and writable; minority ({MINORITY}) went non-Primary and refused writes; "
        f"cluster healed to size {healed_size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
