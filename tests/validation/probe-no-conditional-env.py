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

Reguly fail-closed:
- Play-level `environment:` jako string lub lista (nie dict) jest NIEANALIZOWALNY
  i traktowany jako NARUSZENIE — Ansible akceptuje obie formy, a sonda nie jest
  w stanie bezpiecznie potwierdzić braku odwołania do warunkowej konfiguracji.
- Jeden poziom pośrednictwa zmiennych jest rozwiązywany: jeśli wartość w
  environment to dokładnie `{{ name }}`, sonda szuka `name` w `vars:` play'a
  i dopasowuje regex do rozwiązanej wartości.
- Block-level `environment:` wewnątrz `pre_tasks` jest równie niebezpieczny co
  play-level (templatowany w tym samym momencie) i również jest naruszeniem.
- Task-level `environment:` (bezpośrednio na zadaniu) to POPRAWNY wzorzec.

PASS: zaden play-level/block-w-pre_tasks environment nie odwoluje sie do backup.s3/smb/filesystem.
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

# Matches exactly {{ name }} with optional whitespace
_SINGLE_VAR = re.compile(r"^\{\{\s*(\w+)\s*\}\}$")

TASK_SECTIONS = ("tasks", "post_tasks", "handlers")
PRE_TASK_SECTIONS = ("pre_tasks",)


def _resolve_indirection(value, play_vars):
    """Resolve one level of variable indirection.

    If value is exactly '{{ name }}', look up name in play_vars and return
    the resolved string. Otherwise return the original value as string.
    """
    s = str(value)
    m = _SINGLE_VAR.match(s)
    if m and play_vars:
        var_name = m.group(1)
        if var_name in play_vars:
            return str(play_vars[var_name])
    return s


def _check_env_dict(env, play_vars, pb, context_label, violations):
    """Check a dict-shaped environment for conditional backup references."""
    for key, value in env.items():
        resolved = _resolve_indirection(value, play_vars)
        if CONDITIONAL.search(str(value)) or CONDITIONAL.search(resolved):
            violations.append(
                f"{pb}: {context_label} — "
                f"environment['{key}'] odwoluje sie do "
                "warunkowej konfiguracji backupu; przenies environment "
                "na poziom zadan (wzorzec: playbooks/f10_restore.yml)"
            )


def _check_environment(env, play_vars, pb, context_label, violations):
    """Check an environment value of any shape."""
    if env is None:
        return
    if isinstance(env, dict):
        _check_env_dict(env, play_vars, pb, context_label, violations)
    else:
        # Non-dict environment (string or list) is unanalysable — fail closed.
        violations.append(
            f"{pb}: {context_label} — "
            f"environment ma typ {type(env).__name__} (nie dict); "
            "nie mozna potwierdzic braku odwolania do warunkowej konfiguracji "
            "backupu — fail closed"
        )


def _walk_blocks(items, play_vars, pb, section_name, in_pre_tasks, violations):
    """Recursively walk task lists, checking block-level environment in pre_tasks."""
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        # Block/rescue/always structures
        if "block" in item:
            if in_pre_tasks and "environment" in item:
                play_name = item.get("name", "block")
                label = f"block '{play_name}' w {section_name}"
                _check_environment(
                    item["environment"], play_vars, pb, label, violations
                )
            # Recurse into block/rescue/always
            for sub_key in ("block", "rescue", "always"):
                if sub_key in item:
                    _walk_blocks(
                        item[sub_key], play_vars, pb, section_name,
                        in_pre_tasks, violations
                    )
        # Task-level environment is the CORRECT pattern — never flag it.


def scan_plays(plays, pb):
    """Scan a list of plays for conditional environment violations."""
    violations = []
    if not isinstance(plays, list):
        return violations
    for play in plays:
        if not isinstance(play, dict) or "hosts" not in play:
            continue
        play_name = play.get("name", "?")
        play_vars = play.get("vars")
        if not isinstance(play_vars, dict):
            play_vars = {}

        # Play-level environment — the fatal case
        if "environment" in play:
            label = f"play '{play_name}' — play-level"
            _check_environment(
                play["environment"], play_vars, pb, label, violations
            )

        # Block-level environment inside pre_tasks — equally dangerous
        for section in PRE_TASK_SECTIONS:
            if section in play:
                _walk_blocks(
                    play[section], play_vars, pb, section,
                    in_pre_tasks=True, violations=violations
                )

        # tasks/post_tasks/handlers — task-level is correct, but blocks
        # with environment are still checked for completeness (they are
        # templated at task execution time, so only block-in-pre_tasks is
        # truly dangerous; we do NOT flag these).

    return violations


def scan_file(pb):
    """Load and scan a single playbook file."""
    try:
        with open(pb, encoding="utf-8") as fh:
            plays = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        return [f"{pb}: YAML parse error: {exc}"]
    return scan_plays(plays, pb)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

_SELFTEST_CASES = [
    # (description, yaml_string, expect_violation)
    (
        "evasion (a): play-level environment as string",
        """\
- hosts: backup_source
  environment: "{{ backup_env }}"
  tasks:
    - name: dummy
      debug: msg=hi
""",
        True,
    ),
    (
        "evasion (a): play-level environment as list",
        """\
- hosts: backup_source
  environment:
    - S3_ENDPOINT: "{{ backup.s3.endpoint }}"
  tasks:
    - name: dummy
      debug: msg=hi
""",
        True,
    ),
    (
        "evasion (b): one level of variable indirection",
        """\
- hosts: backup_source
  vars:
    ep: "{{ backup.s3.endpoint }}"
  environment:
    S3_ENDPOINT: "{{ ep }}"
  tasks:
    - name: dummy
      debug: msg=hi
""",
        True,
    ),
    (
        "evasion (c): block-level environment inside pre_tasks",
        """\
- hosts: backup_source
  pre_tasks:
    - name: setup block
      environment:
        S3_ENDPOINT: "{{ backup.s3.endpoint }}"
      block:
        - name: inner task
          debug: msg=hi
  tasks:
    - name: dummy
      debug: msg=hi
""",
        True,
    ),
    (
        "legitimate task-level environment referencing backup.s3",
        """\
- hosts: backup_source
  tasks:
    - name: upload to s3
      command: s3-upload-helper put
      environment:
        S3_ENDPOINT: "{{ backup.s3.endpoint }}"
        S3_SECURE: "{{ backup.s3.secure | default(false) | string | lower }}"
""",
        False,
    ),
    (
        "clean play with no conditional references",
        """\
- hosts: proxysql
  environment:
    MYSQL_PWD: "{{ proxysql_admin_password }}"
  tasks:
    - name: check proxysql
      command: mysql -e "SELECT 1"
""",
        False,
    ),
]


def selftest():
    """Run detector against in-memory snippets."""
    failures = []
    for desc, yaml_str, expect_violation in _SELFTEST_CASES:
        plays = yaml.safe_load(yaml_str)
        violations = scan_plays(plays, "<selftest>")
        got_violation = len(violations) > 0
        if got_violation != expect_violation:
            status = "DETECTED" if got_violation else "MISSED"
            expected = "DETECTED" if expect_violation else "NOT flagged"
            failures.append(
                f"  - {desc}: got {status}, expected {expected}"
            )
            if violations:
                for v in violations:
                    failures.append(f"    violation: {v}")

    if failures:
        print("SELFTEST FAIL")
        for f in failures:
            print(f)
        return 1

    print("SELFTEST PASS")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if "--selftest" in sys.argv:
        return selftest()

    violations = []
    for pb in sorted(glob.glob("playbooks/*.yml")):
        violations.extend(scan_file(pb))

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
