#!/usr/bin/env bash
# Kasowanie maszyn puli PVE, ktorych NIE ZNA zaden root terraform w tym repo.
#
# DLACZEGO to istnieje: `terraform/pve-teardown.sh` sprzata WYLACZNIE to, co widzi
# `terraform output vms` danego roota. Pula potrafi zawierac maszyny spoza stanu:
# klaster postawiony kiedys recznie, resztki po generacji bez roota, host bez
# definicji w repo. Odbudowa od zera zostawiala je wtedy w puli, a kolejny
# `terraform apply` wchodzil na zajete VMID/IP. Zmierzone 2026-09-06: 43 maszyny
# w stanie terraform i 25 poza nim, w tym dzialajacy host bez zadnej definicji.
#
# Uzycie:
#   tools/pve-pool-teardown.sh              # RAPORT (nic nie kasuje)
#   CONFIRM=yes tools/pve-pool-teardown.sh  # kasuje sieroty puli
#
# Wymaga PROXMOX_VE_ENDPOINT i PROXMOX_VE_API_TOKEN. Pula: FLEET_POOL
# (domyslnie `claude-isa`) — ta sama zmienna, co w tests/lab/fleet-state.py.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
POOL="${FLEET_POOL:-claude-isa}"
: "${PROXMOX_VE_ENDPOINT:?Ustaw PROXMOX_VE_ENDPOINT}"
: "${PROXMOX_VE_API_TOKEN:?Ustaw PROXMOX_VE_API_TOKEN}"

# Stan terraform czytamy PRZED jakakolwiek mutacja: maszyna widziana przez
# jakikolwiek root NIE jest sierota puli i nalezy do `infra-teardown`.
managed=""
for root in "$REPO_ROOT"/terraform/*/; do
  [ -f "$root/main.tf" ] || continue
  ids=$(terraform -chdir="$root" output -json vms 2>/dev/null |
    python3 -c 'import json,sys
try:
    data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    raise SystemExit(0)
print(" ".join(str(vm["vmid"]) for vm in data.values() if isinstance(vm, dict) and "vmid" in vm))' 2>/dev/null || true)
  managed="$managed $ids"
done

REPORT=$(POOL="$POOL" MANAGED="$managed" python3 - "$PROXMOX_VE_ENDPOINT" "$PROXMOX_VE_API_TOKEN" <<'PY'
import json, os, ssl, sys, urllib.request

endpoint, token = sys.argv[1].rstrip("/"), sys.argv[2]
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
request = urllib.request.Request(
    f"{endpoint}/api2/json/cluster/resources?type=vm",
    headers={"Authorization": f"PVEAPIToken={token}"},
)
with urllib.request.urlopen(request, context=ctx, timeout=30) as response:
    resources = json.load(response)["data"]

managed = {int(v) for v in os.environ["MANAGED"].split()}
for vm in sorted(resources, key=lambda v: v.get("vmid", 0)):
    if vm.get("pool") != os.environ["POOL"] or vm.get("vmid") in managed:
        continue
    print(f"{vm['vmid']} {vm.get('node', '')} {vm.get('name', '')} {vm.get('status', '')}")
PY
)

if [ -z "$REPORT" ]; then
  echo "OK: pula '$POOL' nie ma maszyn spoza stanu terraform"
  exit 0
fi

echo "Maszyny puli '$POOL' POZA stanem terraform:"
echo "$REPORT" | sed 's/^/  /'

if [ "${CONFIRM:-}" != "yes" ]; then
  echo
  echo "RAPORT — nic nie skasowano. Powtorz z CONFIRM=yes, zeby je usunac." >&2
  exit 1
fi

# `--fail` we WSZYSTKICH wywolaniach: bez niego curl konczy sie zerem takze przy
# HTTP 500/403, wiec skrypt drukowalby "skasowana" dla maszyny, ktora zyje.
api() {
  curl -sS --fail -k -H "Authorization: PVEAPIToken=${PROXMOX_VE_API_TOKEN}" "$@"
}

echo "$REPORT" | while read -r vmid node name status; do
  echo "== $vmid ($name) na $node: $status"
  if [ "$status" = "running" ]; then
    api -X POST "${PROXMOX_VE_ENDPOINT%/}/api2/json/nodes/$node/qemu/$vmid/status/stop" >/dev/null
    state="$status"
    for _ in $(seq 1 30); do
      sleep 5
      state=$(api "${PROXMOX_VE_ENDPOINT%/}/api2/json/nodes/$node/qemu/$vmid/status/current" |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["status"])')
      [ "$state" = "running" ] || break
    done
    # DELETE na dzialajacej maszynie PVE odrzuca; bez tej bramki petla po prostu
    # szlaby dalej i konczyla sie zielono, zostawiajac sierote w puli.
    if [ "$state" = "running" ]; then
      echo "FAIL: $vmid ($name) nadal dziala po 150 s — nie kasuje" >&2
      exit 1
    fi
  fi
  # purge=1 zdejmuje wpisy z zadan zapasowych i replikacji, drugi parametr
  # kasuje wolumeny nieprzypisane do konfiguracji — bez tego zostaja sieroty ZFS
  # i kolejny apply na tym samym VMID pada na "dataset already exists".
  api -X DELETE "${PROXMOX_VE_ENDPOINT%/}/api2/json/nodes/$node/qemu/$vmid?purge=1&destroy-unreferenced-disks=1" >/dev/null
  echo "   skasowana"
done

echo "OK: sieroty puli '$POOL' usuniete"
