"""Klasyfikacja sekretow: co blokuje argv, a co tylko podlega redakcji.

Rozroznienie jest istotne dla bezpieczenstwa: identyfikatory NIE moga trafic do
`SENSITIVE_SECRET_KEYS`, bo straznik argv odrzucilby wtedy wlasne `-u <user>`
writer-guarda. Historia tego bledu jest w komentarzu przy stalej.
"""

from __future__ import annotations

# Values that gate argv. Identifiers MUST NOT be here: enrolling the ProxySQL
# admin username made the guard reject the writer guard's own `-u <user>`.
SENSITIVE_SECRET_KEYS = frozenset({
    "GALERA_BACKUP_PROXYSQL_STATS_PASSWORD",
    "GALERA_BACKUP_ENCRYPTION_KEY",
    "GALERA_BACKUP_S3_SECRET_KEY",
    "GALERA_BACKUP_SMB_PASSWORD",
})

# Additionally masked in output. Credential halves worth hiding from logs, but
# never allowed to gate argv. GALERA_BACKUP_PROXYSQL_STATS_USER is deliberately
# absent from BOTH sets: it is an identifier, and short identity strings are
# substrings of real argv/output tokens (`admin` occurs in `mariadb-admin`,
# `admin_host` and `/etc/proxysql/admin-check.cnf`), so guarding or redacting
# on it breaks legitimate commands and mangles diagnostics.
REDACT_ONLY_SECRET_KEYS = frozenset({
    "GALERA_BACKUP_S3_ACCESS_KEY",
    "GALERA_BACKUP_SMB_USERNAME",
})


def sensitive_secret_values(secrets: dict[str, str]) -> set[str]:
    """Credential values that gate argv — identifiers must never appear here."""
    return {v for k, v in secrets.items() if k in SENSITIVE_SECRET_KEYS and v}


def redactable_secret_values(secrets: dict[str, str]) -> set[str]:
    """Values masked in captured output: credentials plus credential halves."""
    return {
        v for k, v in secrets.items()
        if v and (k in SENSITIVE_SECRET_KEYS or k in REDACT_ONLY_SECRET_KEYS)
    }
