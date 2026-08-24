#!/usr/bin/env python3
"""Verify the Galera rolling-restart capability (ISC-50, ISC-51).

ISC-50: the rolling-restart playbook cycles mariadbd with `serial: 1`, so nodes
        are restarted one at a time (never a simultaneous mass restart).
ISC-51: a health gate (wsrep_local_state=4 Synced + Primary + full cluster size)
        blocks the next node until the previous one has fully rejoined.

This probe is two-layered:
  1. STATIC  — parse f12_rolling_restart.yml: every play that targets multiple
               Galera hosts and cycles mariadbd — through systemd or through a
               raw shell command — MUST set serial:1, and MUST poll
               wsrep_local_state/cluster_status/cluster_size as a health gate.
  2. RUNTIME — every Galera node reports Synced (state 4), Primary, wsrep_ready=ON
               and the expected cluster size (no node stranded after a restart).

Requires APP/PROXYSQL secrets only if you also run the playbook; the probe itself
is read-only (queries wsrep status via the local MariaDB socket).
"""

import re
import sys

import yaml

from _probe_common import ProbeContext, REPO_ROOT, finish, require_hosts, run_ansible

CTX = ProbeContext()
EXPECTED_SIZE = str(CTX.config["galera"]["nodes_expected"])
GALERA = CTX.group_hosts("galera")
PLAYBOOK = REPO_ROOT / "playbooks/f12_rolling_restart.yml"

# Wykrywanie "ten play rusza mariadbd" ma DWIE warstwy, bo restart da sie napisac
# na dwa sposoby, a bramka serial:1 musi obowiazywac oba.
#
# Warstwa 1 (regex): surowe komendy. Zadna z nich nie wystepuje dzis w repo —
# to siatka na przyszla regresje, gdyby ktos wrocil do restartu przez shell.
LIFECYCLE = re.compile(
    r"--wsrep-new-cluster"
    r"|mariadbd\s+--user"
    r"|pkill[^\n]*\bmariadbd\b"
    r"|kill\s+-\d+[^\n]*\bmariadbd\b"
    r"|mariadb-admin\s+shutdown",
    re.IGNORECASE,
)

# Warstwa 2 (strukturalna): produkcyjna sciezka, czyli systemd. Szukamy po drzewie
# zadan, a nie regexem po zrzucie YAML, bo klucze modulu rozdziela dowolny inny
# klucz (`no_block`, `enabled`, `scope`) i sasiedztwo `name`/`state` nie jest pewne.
SERVICE_MODULES = (
    "ansible.builtin.systemd_service",
    "ansible.builtin.systemd",
    "ansible.builtin.service",
)
CYCLE_STATES = frozenset({"restarted", "stopped"})


def cycles_mariadb_service(node) -> bool:
    """Czy w tym poddrzewie jest zadanie zmieniajace stan uslugi MariaDB."""
    if isinstance(node, list):
        return any(cycles_mariadb_service(item) for item in node)
    if isinstance(node, dict):
        for key, value in node.items():
            if key in SERVICE_MODULES and isinstance(value, dict):
                if "mariadb" in str(value.get("name", "")) and str(value.get("state", "")) in CYCLE_STATES:
                    return True
            if cycles_mariadb_service(value):
                return True
    return False


def targets_multiple_galera(hosts):
    s = str(hosts)
    if "galera" not in s and s.strip() != "all":
        return False
    if re.search(r"galera\[\d+\]\s*$", s):
        return False
    return True


def wsrep_status(node, failures, undetermined):
    q = (
        "SHOW STATUS WHERE Variable_name IN "
        "('wsrep_local_state','wsrep_cluster_status','wsrep_ready','wsrep_cluster_size')"
    )
    result = run_ansible(
        CTX,
        node,
        f'mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "{q}" 2>&1 || true',
    )
    require_hosts(result, [node], f"wsrep-status {node}", failures, undetermined)
    if node not in result.bodies:
        return None

    status = {}
    for line in result.body(node).splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            status[parts[0]] = parts[1]
    return status


def main():
    failures = []
    undetermined = []

    # === STATIC: serial:1 + health gate in the rolling-restart playbook ===
    plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    if not isinstance(plays, list):
        failures.append(f"{PLAYBOOK}: not a list of plays")
        plays = []

    serial_checked = False
    for play in plays:
        if not isinstance(play, dict) or "hosts" not in play:
            continue
        text = yaml.safe_dump(play)
        if targets_multiple_galera(play["hosts"]) and (
            LIFECYCLE.search(text) or cycles_mariadb_service(play)
        ):
            serial = play.get("serial")
            if str(serial) != "1":
                failures.append(
                    f"ISC-50 — play '{play.get('name', '?')}' cycles mariadbd "
                    f"but serial={serial!r} (must be 1)"
                )
            # health gate must poll the three wsrep markers OR include the canonical helper
            gate_ok = (
                "assert_galera_healthy" in text
                or (
                    "wsrep_local_state" in text
                    and "wsrep_cluster_status" in text
                    and "wsrep_cluster_size" in text
                    and "until" in text
                )
            )
            if not gate_ok:
                failures.append(
                    f"ISC-51 — play '{play.get('name', '?')}' lacks a health gate "
                    f"(until + wsrep_local_state/cluster_status/cluster_size or tasks/assert_galera_healthy.yml)"
                )
            serial_checked = True

    if not serial_checked:
        failures.append(
            "ISC-50 — no serial:1 Galera lifecycle play found in "
            f"{PLAYBOOK}"
        )

    # === RUNTIME: every node Synced + Primary + full size + ready ===
    if not GALERA:
        undetermined.append("wsrep-status: inwentarz nie definiuje hostow galera")
    for node in GALERA:
        status = wsrep_status(node, failures, undetermined)
        if status is None:
            continue
        if not status:
            failures.append(f"ISC-51 — {node}: no wsrep status (MariaDB down?)")
            continue
        checks = {
            "wsrep_local_state": ("4", "Synced"),
            "wsrep_cluster_status": ("Primary", "Primary"),
            "wsrep_cluster_size": (EXPECTED_SIZE, "full size"),
            "wsrep_ready": ("ON", "ready"),
        }
        for var, (expected, label) in checks.items():
            actual = status.get(var)
            if actual != expected:
                failures.append(
                    f"ISC-51 — {node}.{var}={actual!r} (expected {expected!r} {label})"
                )

    return finish(
        failures,
        undetermined,
        f"ISC-50/51 — rolling restart serial:1 + health gate verified; "
        f"{len(GALERA)}/{len(GALERA)} Galera nodes Synced/Primary/{EXPECTED_SIZE}/ready",
    )


if __name__ == "__main__":
    sys.exit(main())
