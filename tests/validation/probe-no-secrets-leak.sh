#!/usr/bin/env bash
# Sonda: sekrety nie występują w repo ani logach (ISC-43, Anti).
# Uruchomienie z repo root: ./tests/validation/probe-no-secrets-leak.sh
# PASS: brak sekretów w plikach repo
# FAIL: wykryto potencjalny sekret
set -euo pipefail

FAIL=0

# 1. Sprawdź śledzone i nieignorowane pliki repo.
echo "--- Checking repo files for secrets ---"
if ! python3 - <<'PY'
from pathlib import Path
import os
import re
import subprocess

paths = subprocess.check_output(
    ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
).decode().split("\0")
# Tests inject an ignored temporary fixture through this additive path.
paths.extend(filter(None, os.environ.get("SECRET_PROBE_EXTRA_PATHS", "").split(os.pathsep)))
assignment = re.compile(
    r"(?:[\"'](?P<quoted_key>password|passwd|secret|token|api_key)[\"']|"
    r"(?P<plain_key>\b(?:password|passwd|secret|token|api_key)))\s*[:=]\s*"
    r"(?:(?P<value_quote>[\"'])(?P<quoted_value>.*?)(?P=value_quote)|"
    r"(?P<unquoted_value>\{\{.*?\}\}|[^\s#,\]}()\"']+))",
    re.IGNORECASE,
)
placeholder = re.compile(
    r"(?:.*(?:replace[-_]?me|change[-_]?me).*|example|placeholder|vault:.*|<[^>]+>|null|none|true|false|disabled|unknown|yes|no|on|off|\.\.\.|password|passwd|secret|credentials|smbpassword|s3cr3t|DBPASS)",
    re.IGNORECASE,
)
jinja_expression = re.compile(r"\{\{.*\}\}", re.DOTALL)
# Goly identyfikator bez cudzyslowow to referencja do zmiennej, nie literal.
# Literalny sekret w YAML jest cytowany (tak wygladaja fixture'y w tescie);
# odwolanie do zmiennej wewnatrz wielolinijkowego wyrazenia Jinja nie jest,
# bo cudzyslowy obejmuja tam caly slownik, nie pojedyncza wartosc.
# Zwalniamy wylacznie nazwy ZDEFINIOWANE w tym samym pliku — inaczej goly
# literal bez cudzyslowow przeszedlby niezauwazony.
bare_identifier = re.compile(r"[a-z_][a-z0-9_]*", re.IGNORECASE)
variable_definition = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*:", re.IGNORECASE | re.MULTILINE)
environment_reference = re.compile(
    r"(?:\$\{[A-Z_][A-Z0-9_]*(?::\?[^}]*)?\}|\$[A-Z_][A-Z0-9_]*)"
)
private_key = re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY")
password_in_argv = re.compile(r"-[pW]\s*\{\{[^}]*(?:password|passwd|secret|auth_pass)", re.IGNORECASE)
unquoted_code_suffixes = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java"}
findings = []

for name in filter(None, paths):
    path = Path(name)
    if path.suffix == ".vault" or path.name.endswith("secrets.yml"):
        continue
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        continue
    defined_variables = set(variable_definition.findall(text))
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in assignment.finditer(line):
            if (
                match.group("plain_key")
                and match.start() > 0
                and line[match.start() - 1] in "\"'"
            ):
                continue
            if not match.group("value_quote") and (
                path.suffix in unquoted_code_suffixes
                or path.name == "galera-backup"
                or path.parts[:1] == ("docs",)
            ):
                continue
            value = (
                match.group("quoted_value")
                if match.group("value_quote")
                else match.group("unquoted_value")
            ).strip()
            # Pusta wartosc nie niesie sekretu. Bez tego zarchiwizowane stany
            # Terraform ("password": "") wywalaja bramke na falszywym alarmie.
            if not value:
                continue
            if (
                jinja_expression.fullmatch(value)
                or environment_reference.fullmatch(value)
                or placeholder.fullmatch(value)
                or (
                    not match.group("value_quote")
                    and bare_identifier.fullmatch(value)
                    and value in defined_variables
                )
            ):
                continue
            findings.append(f"{path}:{line_number}:{line.strip()}")
        if private_key.search(line):
            findings.append(f"{path}:{line_number}:private key marker")
        for m in password_in_argv.finditer(line):
            findings.append(f"{path}:{line_number}:password passed to shell -p/-W via Jinja (argv leak; use MYSQL_PWD env): {line.strip()}")

for finding in findings:
    print(f"FAIL: ISC-43 — potential secret in {finding}")
raise SystemExit(1 if findings else 0)
PY
then
  FAIL=1
fi

# 2. Sprawdź argv procesów Ansible (jeśli działa)
echo "--- Checking running process argv ---"
if ps -eo args 2>/dev/null | grep -iE 'ansible.*-e.*password|ansible.*--extra-vars.*pass' | grep -v grep | grep -q .; then
  echo "FAIL: ISC-43 — password detected in running Ansible process argv"
  ps -eo args | grep -iE 'ansible.*-e.*password|ansible.*--extra-vars.*pass' | grep -v grep | head -5
  FAIL=1
fi

# 3. Uruchom testy jednostkowe bezpieczeństwa sekretów w runnerze
echo "--- Running behavioral secret safety unit tests ---"
if ! python3 -m unittest tests.unit.test_galera_backup_core.GaleraBackupCoreTests.test_secret_cannot_enter_subprocess_argv tests.unit.test_galera_backup_core.GaleraBackupCoreTests.test_secret_redaction >/dev/null 2>&1; then
  echo "FAIL: ISC-43 — secret redaction/argv unit tests failed"
  FAIL=1
fi
if [ "$FAIL" -eq 0 ]; then
  echo "PASS: ISC-43 — no secrets detected in repo files or process argv"
  exit 0
else
  exit 1
fi
