#!/usr/bin/env python3
"""Play-level `environment:` nie może odwoływać się do warunkowej konfiguracji backupu.

clusters/schema/cluster.schema.json wymaga bloku `backup.s3` WYLACZNIE gdy
`backup.destination == 's3'`, i dopuszcza `destination: smb` bez bloku `s3`.
Ansible templatuje play-level `environment:` podczas post-validate PIERWSZEGO
zadania play'a — czyli PRZED `pre_tasks`. Assert `backup.destination == 's3'`
w pre_tasks NIE moze tego ochronic: klaster zgodny ze schema (destination: smb)
umiera z "object of type 'dict' has no attribute 's3'" zamiast z zaprojektowanym
fail_msg. `--syntax-check` tego NIE lapie (zwraca rc=0).

Poprawny wzorzec: `environment:` na poziomie POJEDYNCZYCH zadan, ktore go
potrzebuja — patrz playbooks/f10_restore.yml.

PASS: zaden play-level environment nie odwoluje sie do backup.s3/smb/filesystem.
FAIL: odwoluje sie.
"""

import glob
import re
import sys

import yaml

CONDITIONAL = re.compile(
    r"backup\s*\.\s*(s3|smb|filesystem)\b"
    r"|backup\s*\[\s*['\"](s3|smb|filesystem)['\"]\s*\]"
)


def main():
    violations = []
    for pb in sorted(glob.glob("playbooks/*.yml")):
        try:
            with open(pb, encoding="utf-8") as fh:
                plays = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            violations.append(f"{pb}: YAML parse error: {exc}")
            continue
        if not isinstance(plays, list):
            continue
        for play in plays:
            if not isinstance(play, dict) or "hosts" not in play:
                continue
            env = play.get("environment")
            if not isinstance(env, dict):
                continue
            for key, value in env.items():
                if CONDITIONAL.search(str(value)):
                    violations.append(
                        f"{pb}: play '{play.get('name', '?')}' — "
                        f"play-level environment['{key}'] odwoluje sie do "
                        "warunkowej konfiguracji backupu; przenies environment "
                        "na poziom zadan (wzorzec: playbooks/f10_restore.yml)"
                    )

    if violations:
        print(
            "FAIL: play-level environment odwoluje sie do warunkowej "
            "konfiguracji backupu:"
        )
        for violation in violations:
            print(f"  - {violation}")
        return 1

    print(
        "PASS: zaden play-level environment nie zalezy od "
        "backup.s3/smb/filesystem"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
