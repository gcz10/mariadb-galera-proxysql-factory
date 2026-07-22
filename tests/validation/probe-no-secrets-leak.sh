#!/usr/bin/env bash
# Sonda: sekrety nie występują w repo ani logach (ISC-43, Anti).
# Uruchomienie z repo root: ./tests/validation/probe-no-secrets-leak.sh
# PASS: brak sekretów w plikach repo
# FAIL: wykryto potencjalny sekret
set -euo pipefail

FAIL=0

# 1. Sprawdź pliki repo (pomijając .git, secrets.yml, .vault)
echo "--- Checking repo files for secrets ---"
while IFS= read -r f; do
  # Pomiń pliki vault/secrets (to są szyfrowane pliki Ansible Vault — nie plaintext)
  case "$f" in
    *.vault|*secrets.yml|*.git/*|*.gitignore) continue ;;
  esac
  # Szukaj wzorców haseł / kluczy prywatnych
  if grep -nE '(password|passwd|secret|token|api_key)\s*[:=]\s*["\x27][^"VAULT\x27]' "$f" 2>/dev/null | grep -v 'VAULT:' | grep -v 'replace-me' | grep -v 'example' | grep -v 'PLACEHOLDER' | grep -q .; then
    echo "FAIL: ISC-43 — potential secret in $f"
    grep -nE '(password|passwd|secret|token|api_key)\s*[:=]\s*["\x27][^"VAULT\x27]' "$f" 2>/dev/null | grep -v 'VAULT:' | grep -v 'replace-me' | grep -v 'example' | grep -v 'PLACEHOLDER' | head -5
    FAIL=1
  fi
  if grep -qE 'BEGIN (RSA |EC )?PRIVATE KEY' "$f" 2>/dev/null; then
    echo "FAIL: ISC-43 — private key found in $f"
    FAIL=1
  fi
done < <(find . -type f -not -path './.git/*' -not -path './.gitignore')

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
