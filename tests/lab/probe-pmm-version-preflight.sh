#!/usr/bin/env bash
set -euo pipefail

: "${PMM_ADMIN_PASSWORD:?PMM_ADMIN_PASSWORD is required}"

set +e
output="$(
  PMM_ADMIN_PASSWORD="${PMM_ADMIN_PASSWORD}" \
    ansible-playbook playbooks/f11_pmm_client.yml \
      -i clusters/lab-cluster/inventory.yml \
      -i localhost, \
      -e @clusters/lab-cluster/cluster.yml \
      -e '{"pmm":{"version":"0.0.0"}}' \
      --limit localhost 2>&1
)"
status=$?
set -e

if [[ ${status} -eq 0 ]]; then
  printf '%s\n' "FAIL: PMM preflight accepted a runtime outside versions.lock" >&2
  exit 1
fi

if [[ "${output}" != *"PMM runtime version"*"0.0.0"* ]]; then
  printf '%s\n' "FAIL: playbook did not reject the mismatched PMM version in preflight" >&2
  exit 1
fi

if [[ "${output}" == *"Wymagaj danych dostępowych poza repozytorium"* ]]; then
  printf '%s\n' "FAIL: playbook continued beyond PMM version preflight" >&2
  exit 1
fi

printf '%s\n' "PASS: mismatched PMM runtime is rejected before host-side work"
