#!/usr/bin/env python3
"""Drugi przebieg converge musi dac `changed=0` na kazdym hoscie.

DLACZEGO TO POWSTALO: Red Hat CoP (automation-good-practices, sekcja Testing)
stawia to jako wymog bezwarunkowy - "Every role and playbook MUST be tested for
idempotency: running it twice in a row should produce changed=0 on the second
run". To repozytorium mialo 501 testow jednostkowych i ANI JEDNEJ bramki na to.
Makefile deklarowal "idempotentny converge" w komentarzu celu, a linia 448
przyznawala wprost: "wyjdzie to jako changed=0, ale bramki nie bylo zadnej".

CO TO LAPIE, CZEGO NIE WIDZI RESZTA BRAMKI: zadanie, ktore melduje zmiane przy
kazdym uruchomieniu - `command` bez `creates`/`changed_when`, szablon z data
w srodku, plik z prawami ustawianymi w kolko. Sondy stanu ustalonego widza
zdrowy klaster i nie maja jak tego zobaczyc, a koszt jest realny: handler od
takiego zadania restartuje MariaDB przy KAZDYM converge, czyli rutynowa zmiana
konfiguracji staje sie rolling restartem produkcji.

DLACZEGO PRZEBIEG NA ZYWO, A NIE MOLECULE: kontener nie odwzoruje kworum, SST
ani VIP-a, a to na nich stoi wartosc tego produktu. Sonda uruchamia dokladnie
te playbooki, ktore uruchamia `cluster-deploy`, na tych samych maszynach.

Falsyfikowalna: gdyby ktos dopisal zadanie bez `changed_when`, ta sonda spadnie
na czerwono przy nastepnym przebiegu bramki.
"""

import os
import re
import subprocess
import sys

from _probe_common import ProbeContext, finish

CTX = ProbeContext()
CLUSTER = CTX.config["cluster"]["name"]
INVENTORY = os.environ.get("CLUSTER_INVENTORY", f"clusters/{CLUSTER}/inventory.yml")
CONFIG = os.environ.get("CLUSTER_CONFIG", f"clusters/{CLUSTER}/cluster.yml")

# Dokladnie to, co robi `cluster-deploy`. Firewall pomijamy swiadomie: ma wlasny
# parametr `firewall_target_hosts` i jest czescia innego celu.
PLAYBOOKS = ["playbooks/f2_install.yml", "playbooks/site.yml"]

RECAP_LINE = re.compile(
    r"^(?P<host>\S+)\s*:\s*ok=\d+\s+changed=(?P<changed>\d+)\s+unreachable=(?P<unreachable>\d+)"
    r"\s+failed=(?P<failed>\d+)"
)


def run(playbook, failures, undetermined):
    proc = subprocess.run(
        ["ansible-playbook", playbook, "-i", INVENTORY, "-e", f"@{CONFIG}"],
        capture_output=True, text=True,
    )
    recap = {}
    for line in proc.stdout.splitlines():
        match = RECAP_LINE.match(line.strip())
        if match:
            recap[match.group("host")] = {
                "changed": int(match.group("changed")),
                "unreachable": int(match.group("unreachable")),
                "failed": int(match.group("failed")),
            }
    if not recap:
        undetermined.append(
            f"{playbook}: brak PLAY RECAP w wyjsciu (rc={proc.returncode}) - nie zmierzono"
        )
        return
    for host, counts in sorted(recap.items()):
        if counts["failed"] or counts["unreachable"]:
            undetermined.append(
                f"{playbook}: {host} failed={counts['failed']} "
                f"unreachable={counts['unreachable']} - idempotencji nie zmierzono"
            )
        elif counts["changed"]:
            failures.append(
                f"{playbook}: {host} zglosil changed={counts['changed']} przy DRUGIM "
                f"przebiegu - zadanie melduje zmiane w kolko (brak `changed_when`/`creates`), "
                f"wiec kazdy converge dotyka uslugi"
            )
    print(f"  {playbook}: " + ", ".join(f"{h}=changed:{c['changed']}" for h, c in sorted(recap.items())))


def main():
    failures, undetermined = [], []
    print(f"# drugi przebieg converge dla {CLUSTER} (CoP: changed=0 wymagane)")
    for playbook in PLAYBOOKS:
        run(playbook, failures, undetermined)
    return finish(
        failures,
        undetermined,
        f"idempotencja converge potwierdzona - changed=0 na wszystkich hostach "
        f"({', '.join(PLAYBOOKS)})",
    )


if __name__ == "__main__":
    sys.exit(main())
