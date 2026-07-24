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
import re
import subprocess

paths = subprocess.check_output(
    ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
).decode().split("\0")
assignment = re.compile(
    r"\b(?:password|passwd|secret|token|api_key)\s*[:=]\s*(?:([\"'])(.*?)\1|([^\s#,\]}]+))",
    re.IGNORECASE,
)
placeholder = re.compile(
    r"(?:.*(?:replace[-_]?me|change[-_]?me).*|example|placeholder|vault:.*|<[^>]+>|null|none|true|false|disabled|unknown)",
    re.IGNORECASE,
)
jinja_expression = re.compile(r"\{\{.*\}\}", re.DOTALL)
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
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in assignment.finditer(line):
            if not match.group(1) and path.suffix in unquoted_code_suffixes:
                continue
            value = (match.group(2) if match.group(1) else match.group(3)).strip()
            if (
                jinja_expression.fullmatch(value)
                or environment_reference.fullmatch(value)
                or placeholder.fullmatch(value)
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

if [ "$FAIL" -eq 0 ]; then
  echo "PASS: ISC-43 — no secrets detected in repo files or process argv"
  exit 0
else
  exit 1
fi
