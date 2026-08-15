"""Czyste funkcje tekstowe i walidacja opcji — bez I/O i bez stanu.

Zadna z nich nie jest podmieniana przez `patch.object` w testach, dlatego
przeniesienie ich tutaj nie moze cicho uniewaznic zadnego mocka.
"""

import re

from .errors import BackupError


def sanitize_cluster_name(name: str) -> str:
    if not name or not re.match(r"^[A-Za-z0-9_-]+$", name):
        raise BackupError(
            "E_INVALID_CLUSTER",
            f"Cluster name '{name}' is invalid; must match ^[A-Za-z0-9_-]+$"
        )
    return name


def quote_sql_identifier(value: str) -> str:
    return f"`{value.replace('`', '``')}`"


def escape_metric_label(val: str) -> str:
    res = str(val)
    res = res.replace("\\", "\\\\")
    res = res.replace('"', '\\"')
    res = res.replace("\n", "\\n")
    return res


def normalize_smb_source(source: str) -> str:
    return str(source).replace("\\", "/").rstrip("/").casefold()


def validate_smb_options(options: list[str]) -> list[str]:
    errors = []
    opt_set = {str(opt) for opt in options}

    for opt in options:
        opt_lower = opt.lower()
        if opt != opt_lower:
            errors.append(f"SMB option must use canonical lowercase spelling: '{opt}'")
        if opt_lower.startswith("username=") or opt_lower == "username":
            errors.append("Unsafe SMB option 'username=' specified (credentials must be stored in root-only secrets)")
        if opt_lower.startswith("password=") or opt_lower == "password":
            errors.append("Unsafe SMB option 'password=' specified (credentials must be stored in root-only secrets)")
        if opt_lower.startswith("credentials=") or opt_lower == "credentials":
            errors.append("Unsafe SMB option 'credentials=' specified")
        if opt_lower == "noseal":
            errors.append("Unsafe SMB option 'noseal' specified")
        if opt_lower.startswith("vers=1") or opt_lower.startswith("vers=2"):
            errors.append(f"Unsafe SMB dialect specified: '{opt}' (SMB 3.x required)")

    required_opts = ["seal", "nosuid", "nodev", "noexec"]
    for req in required_opts:
        if req not in opt_set:
            errors.append(f"Missing required SMB option: '{req}'")

    if "vers=3.1.1" not in opt_set:
        errors.append("Missing required SMB option: 'vers=3.1.1'")

    return errors
