"""Render the canonical Grafana alert UID identities for probes and tests."""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from jinja2 import Environment

REPO_ROOT = Path(__file__).resolve().parents[2]
IDENTITY_PATH = REPO_ROOT / "playbooks" / "vars" / "alert_identity.yml"


def _hash(value: str, algorithm: str) -> str:
    return hashlib.new(algorithm, value.encode()).hexdigest()


def alert_uid_prefixes(cluster_label: str) -> tuple[str, str]:
    """Return current and legacy prefixes from the Ansible source of truth."""
    config = yaml.safe_load(IDENTITY_PATH.read_text(encoding="utf-8")) or {}
    try:
        current_template = config["f15_uid_prefix"]
        legacy_template = config["f15_uid_prefix_legacy"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid alert identity config: {IDENTITY_PATH}") from exc

    environment = Environment(autoescape=False)
    environment.filters["hash"] = _hash
    context = {"cluster_label": cluster_label}
    current = environment.from_string(current_template).render(**context).strip()
    legacy = environment.from_string(legacy_template).render(**context).strip()
    if not current or not legacy:
        raise ValueError(f"empty alert UID prefix for {cluster_label!r}")
    return current, legacy
