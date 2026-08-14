"""Canonical helper for tests/lab test scripts.

Consolidates:
- Ansible CLI multi-host and single-host execution and output parsing.
- Cluster configuration and inventory loading.
- Dynamic ProxySQL hostgroup resolution.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional

import yaml

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
DEFAULT_INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
ANSIBLE_BIN = os.environ.get("ANSIBLE", "ansible")


def load_config(path: Optional[str] = None) -> dict:
    config_path = path or DEFAULT_CONFIG
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_inventory(path: Optional[str] = None) -> dict:
    inv_path = path or DEFAULT_INVENTORY
    with open(inv_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_writer_hostgroup(config: Optional[dict] = None) -> int:
    cfg = config or load_config()
    base = int(cfg.get("proxysql", {}).get("hostgroup_base", 10))
    return base + 0


def run_ansible(
    pattern: str,
    module: str = "ansible.builtin.shell",
    args: str = "",
    inventory: Optional[str] = None,
    timeout: int = 60,
    check: bool = False,
) -> Dict[str, str]:
    """Execute Ansible module on a pattern and return {host: stdout_body}."""
    inv = inventory or DEFAULT_INVENTORY
    cmd = [ANSIBLE_BIN, pattern, "-i", inv, "-m", module, "-a", args, "--fork", "5"]
    result = subprocess.run(
        cmd,
        cwd=WORKSPACE_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Ansible execution failed (rc={result.returncode}):\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    data = {}
    current_host = None
    current_lines = []

    for line in result.stdout.splitlines():
        header = re.match(r"^(\S+)\s*\|\s*\w+\s*\|\s*rc=\d+\s*>>?\s*$", line)
        if header:
            if current_host is not None:
                data[current_host] = "\n".join(current_lines).strip()
            current_host = header.group(1)
            current_lines = []
        elif current_host is not None:
            current_lines.append(line)

    if current_host is not None:
        data[current_host] = "\n".join(current_lines).strip()

    return data


def run_single(
    node: str,
    script: str,
    inventory: Optional[str] = None,
    timeout: int = 60,
    check: bool = False,
) -> str:
    """Execute shell command on a single node and extract body output."""
    res = run_ansible(node, "ansible.builtin.shell", script, inventory, timeout, check)
    if node in res:
        return res[node]
    if len(res) == 1:
        return next(iter(res.values()))
    return ""
