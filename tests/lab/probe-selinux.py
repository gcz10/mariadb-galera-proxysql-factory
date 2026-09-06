#!/usr/bin/env python3
"""ISC-4: SELinux pozostaje Enforcing po pelnym deploy.

Poprzednia sonda (`tests/validation/probe-selinux.sh`) zostala usunieta razem
z martwymi skryptami, a ISC-4 zostal ze statusem PASS opartym o pomiar z
2026-08-14. Historyczny wynik nie jest pomiarem biezacej floty, wiec sonda
wraca — tym razem na wspolnym protokole fail-closed: host, ktory nie
odpowiedzial, jest NIEROZSTRZYGNIETY (exit 2), nigdy zielony.

Mierzone jest DWOJE, bo `getenforce` mowi tylko o stanie do najblizszego
restartu:
  * tryb biezacy (`getenforce`) musi byc `Enforcing`,
  * tryb trwaly (`/etc/selinux/config`, `SELINUX=`) musi byc `enforcing`,
    inaczej host wraca po restarcie w `permissive`/`disabled` i nikt tego nie
    zauwazy.

Uruchomienie: CLUSTER=<nazwa> python3 tests/lab/probe-selinux.py
"""

from __future__ import annotations

import sys

from _probe_common import ProbeContext, check, finish, require_hosts, run_ansible

# Kazda grupa inwentarza jest w zakresie: hardening dotyczy calego klastra,
# nie tylko wezlow bazodanowych.
GROUPS = ("galera", "proxysql", "infra", "app", "restore")

# Jeden strumien na host: tryb biezacy i trwaly rozdzielone znacznikami, zeby
# brak ktoregokolwiek pomiaru byl widoczny zamiast domyslac sie pustego stringa.
PROBE = (
    "set -o pipefail; "
    "printf 'current=%s\\n' \"$(getenforce 2>/dev/null || echo UNKNOWN)\"; "
    "printf 'persistent=%s\\n' "
    "\"$(sed -n 's/^[[:space:]]*SELINUX=\\([a-z]*\\).*/\\1/p' /etc/selinux/config 2>/dev/null "
    "| tail -n1 || true)\""
)


def parse(body: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in body.splitlines():
        key, _, value = line.strip().partition("=")
        if key in ("current", "persistent"):
            values[key] = value.strip()
    return values


def main() -> int:
    failures: list[str] = []
    undetermined: list[str] = []
    ctx = ProbeContext()

    hosts: list[str] = []
    for group in GROUPS:
        for host in ctx.group_hosts(group):
            if host not in hosts:
                hosts.append(host)

    if not hosts:
        undetermined.append("ISC-4: inwentarz nie definiuje zadnego hosta")
        return finish(failures, undetermined, "")

    result = run_ansible(ctx, ":".join(GROUPS), PROBE)
    require_hosts(result, hosts, "ISC-4 SELinux", failures, undetermined)

    measured = 0
    for host in hosts:
        if host in result.errors:
            continue
        values = parse(result.body(host))
        current = values.get("current", "")
        persistent = values.get("persistent", "")
        if not current or not persistent:
            undetermined.append(
                f"ISC-4: {host} nie zwrocil kompletnego pomiaru "
                f"(getenforce={current or 'brak'}, /etc/selinux/config={persistent or 'brak'})"
            )
            continue
        measured += 1
        check(
            current == "Enforcing",
            f"ISC-4: {host} ma getenforce={current}, oczekiwane Enforcing",
            failures,
        )
        check(
            persistent == "enforcing",
            f"ISC-4: {host} ma SELINUX={persistent} w /etc/selinux/config — "
            "po restarcie SELinux nie wroci do Enforcing",
            failures,
        )

    return finish(
        failures,
        undetermined,
        f"ISC-4 — SELinux Enforcing teraz i po restarcie na {measured}/{len(hosts)} hostach",
    )


if __name__ == "__main__":
    sys.exit(main())
