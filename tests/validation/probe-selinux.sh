#!/usr/bin/env bash
# Sonda: sprawdza czy SELinux jest w trybie Enforcing (ISC-4).
# Uruchomienie na docelowym hoście: ./tests/validation/probe-selinux.sh
# PASS: getenforce = Enforcing
# FAIL: inaczej
set -euo pipefail

RESULT=$(getenforce 2>/dev/null || echo "UNKNOWN")

if [ "$RESULT" = "Enforcing" ]; then
  echo "PASS: ISC-4 — SELinux Enforcing"
  exit 0
else
  echo "FAIL: ISC-4 — SELinux is '$RESULT', expected Enforcing"
  exit 1
fi
