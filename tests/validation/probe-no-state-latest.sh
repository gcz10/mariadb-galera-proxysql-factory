#!/usr/bin/env bash
# Sonda: Anti — żaden task produkcyjny nie używa state: latest (ISC-63).
# Uruchomienie z repo root: ./tests/validation/probe-no-state-latest.sh
# PASS: brak "state: latest" w rolach i playbookach produkcyjnych
# FAIL: wykryto "state: latest"
set -euo pipefail

FAIL=0

# Przeszukaj role i playbooki
for f in $(find roles playbooks -type f -name '*.yml' -o -name '*.yaml' 2>/dev/null); do
  # Pomiń profile laboratory (tam candidate.lock.yml dozwolone, ale state: latest nadal nie)
  if grep -qE '^\s*state:\s*latest' "$f" 2>/dev/null; then
    echo "FAIL: ISC-63 — 'state: latest' found in $f"
    grep -nE '^\s*state:\s*latest' "$f" | head -5
    FAIL=1
  fi
done

if [ "$FAIL" -eq 0 ]; then
  echo "PASS: ISC-63 — no 'state: latest' in roles or playbooks"
  exit 0
else
  exit 1
fi
