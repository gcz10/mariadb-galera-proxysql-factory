#!/usr/bin/env python3
"""Katalog w roles/ jest albo prawdziwa rola, albo magazynem assetow — nigdy
cicha atrapa.

DLACZEGO TO ISTNIEJE. Ansible traktuje katalog bez `tasks/main.yml` jako
POPRAWNA, pusta role. Zmierzone na tym repo:

    roles: mariadb_install   ->  rc=0, PLAY RECAP pusty, zero zadan

Czyli literowka w nazwie roli albo odwolanie do katalogu, ktory trzyma same
szablony, nie jest bledem — jest cisza. W operacji niszczacej cisza wyglada
identycznie jak sukces.

Wiekszosc katalogow w roles/ tej fabryki to swiadomie NIE role: playbooki
siegaja po ich `templates/`/`files/` sciezka (`../roles/<x>/templates/<y>.j2`),
bo repo celowo nie przepisuje playbookow na role. To jest w porzadku dopoki
nikt nie zaadresuje takiego katalogu NAZWA roli.

KONTRAKT
1. Katalog z `tasks/main.yml` to rola — moze byc wolana przez `roles:`,
   `include_role`, `import_role`.
2. Katalog bez `tasks/main.yml` NIE MOZE byc nigdzie wolany jako rola.
3. Katalog bez `tasks/main.yml` musi cokolwiek zawierac. Pusty szkielet
   (pozostalosc po `ansible-galaxy init`) maskuje blad „rola nie istnieje":
   z katalogiem `roles: preflight` konczy sie rc=0, bez katalogu rc=1.
"""

import os
import re
import sys

ROLES_DIR = "roles"
PLAYBOOK_DIRS = ["playbooks"]


def real_roles_and_assets():
    real, assets = set(), set()
    if not os.path.isdir(ROLES_DIR):
        return real, assets
    for name in sorted(os.listdir(ROLES_DIR)):
        path = os.path.join(ROLES_DIR, name)
        if not os.path.isdir(path):
            continue
        if os.path.isfile(os.path.join(path, "tasks", "main.yml")):
            real.add(name)
        else:
            assets.add(name)
    return real, assets


def has_any_content(name):
    for _, _, files in os.walk(os.path.join(ROLES_DIR, name)):
        if files:
            return True
    return False


def iter_playbooks():
    for top in PLAYBOOK_DIRS:
        for root, _, files in os.walk(top):
            for fname in files:
                if fname.endswith((".yml", ".yaml")):
                    yield os.path.join(root, fname)


# `roles:` przyjmuje liste nazw; `include_role`/`import_role` biora `name:`.
# Parsujemy tekstem, nie YAML-em: playbooki maja bloki `when:` z Jinja i
# wystarczy nam znalezc odwolanie, nie zrozumiec cala strukture.
ROLE_LIST_RE = re.compile(r"^(\s*)roles:\s*$")
LIST_ITEM_RE = re.compile(r"^\s*-\s*(?:role:\s*)?([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*$")
INCLUDE_RE = re.compile(r"(?:include_role|import_role):")
NAME_RE = re.compile(r"^\s*name:\s*[\"']?([A-Za-z0-9_][A-Za-z0-9_.-]*)[\"']?\s*$")


def role_references(path):
    """Zwraca [(linia, nazwa_roli)] dla kazdego odwolania nazwa roli."""
    refs = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    for idx, line in enumerate(lines):
        m = ROLE_LIST_RE.match(line)
        if m:
            indent = len(m.group(1))
            for j in range(idx + 1, len(lines)):
                nxt = lines[j]
                if not nxt.strip() or nxt.lstrip().startswith("#"):
                    continue
                if len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                item = LIST_ITEM_RE.match(nxt)
                if item:
                    refs.append((j + 1, item.group(1)))
            continue
        if INCLUDE_RE.search(line):
            for j in range(idx + 1, min(idx + 6, len(lines))):
                nm = NAME_RE.match(lines[j])
                if nm:
                    refs.append((j + 1, nm.group(1)))
                    break
    return refs


def main():
    failures = []
    real, assets = real_roles_and_assets()

    # 3. Pusty szkielet maskuje blad „rola nie istnieje".
    for name in sorted(assets):
        if not has_any_content(name):
            failures.append(
                f"{ROLES_DIR}/{name}/ jest pustym szkieletem — Ansible uzna go za "
                f"poprawna, pusta role i `roles: {name}` skonczy sie rc=0 zamiast bledu"
            )

    # 2. Katalog bez tasks/main.yml nie moze byc wolany jako rola.
    for path in iter_playbooks():
        for lineno, name in role_references(path):
            if name in assets:
                failures.append(
                    f"{path}:{lineno} wola `{name}` jako role, a "
                    f"{ROLES_DIR}/{name}/tasks/main.yml nie istnieje — to cichy no-op "
                    f"(rc=0, zero zadan), nie blad"
                )
            elif name not in real and os.path.isdir(ROLES_DIR):
                failures.append(
                    f"{path}:{lineno} wola role `{name}`, ktorej nie ma w {ROLES_DIR}/"
                )

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1

    print(
        f"PASS: kontrakt rol — {len(real)} rol z tasks/main.yml "
        f"({', '.join(sorted(real)) or '-'}); "
        f"{len(assets)} katalogow z samymi assetami "
        f"({', '.join(sorted(assets)) or '-'}) nie jest wolanych nazwa roli"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
