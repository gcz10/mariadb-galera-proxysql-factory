#!/usr/bin/env bash
# Sonda: Anti — brak mutowalnych zrodel wersji i wylaczonych kontroli integralnosci
# (ISC-63 + audyt P1-A 2026-09).
# Uruchomienie z repo root: ./tests/validation/probe-no-state-latest.sh
# PASS: brak "state: latest", brak disable_gpg_check, brak URL "-latest." w lockfile'ach
# FAIL: wykryto ktorykolwiek wzorzec
set -euo pipefail

FAIL=0

# Przeszukaj role i playbooki
for f in $(find roles playbooks -type f -name '*.yml' -o -name '*.yaml' 2>/dev/null); do
  if grep -qE '^\s*state:\s*latest' "$f" 2>/dev/null; then
    echo "FAIL: ISC-63 — 'state: latest' found in $f"
    grep -nE '^\s*state:\s*latest' "$f" | head -5
    FAIL=1
  fi
done

# Audyt P1-A: percona-release-latest.noarch.rpm przechodzil przez poprzednia
# wersje sondy, bo mutowalnosc siedziala w URL, nie w `state:`.
# Filtr `grep -vE ':[[:space:]]*#'` odrzuca linie komentarzy — np. historyczny
# komentarz w platform_install.yml opisuje STARE zachowanie tym samym slowem.
for f in roles playbooks; do
  hits=$(grep -rnE 'disable_gpg_check:[[:space:]]*true' "$f" 2>/dev/null | grep -vE ':[[:space:]]*#' || true)
  if [ -n "$hits" ]; then
    echo "FAIL: P1-A — 'disable_gpg_check: true' found in $f"
    printf '%s\n' "$hits" | head -5
    FAIL=1
  fi
done

hits=$(grep -rnE 'https?://[^[:space:]"'\'']*-latest\.(noarch|x86_64|src)\.rpm' versions/*.lock.yml 2>/dev/null | grep -vE ':[[:space:]]*#' || true)
if [ -n "$hits" ]; then
  echo "FAIL: P1-A — mutowalny URL '-latest' pakietu RPM w lockfile"
  printf '%s\n' "$hits" | head -5
  FAIL=1
fi

if [ "$FAIL" -eq 0 ]; then
  echo "PASS: ISC-63/P1-A — no mutable version sources or disabled integrity checks"
  exit 0
else
  exit 1
fi
