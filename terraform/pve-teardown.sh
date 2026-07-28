#!/usr/bin/env bash
# Teardown infrastruktury PVE: terraform destroy + posprzatanie sierot ZFS.
#
# DLACZEGO to istnieje: `terraform destroy` (bpg/proxmox) usuwa VM, ale zostawia
# wolumeny `vm-<vmid>-cloudinit` (czasem tez `-disk-N`). Kolejny `terraform apply`
# pada wtedy na wiekszosci VM z:
#   unable to create VM <vmid> - zfs error: cannot create '...-cloudinit': dataset already exists
# Bez tego kroku KAZDE odtworzenie infrastruktury wymaga recznego `zfs destroy` na PVE.
#
# Uzycie: terraform/pve-teardown.sh <katalog-terraform>
#   np.   terraform/pve-teardown.sh terraform/claude-r10
#
# Wymaga w srodowisku: PROXMOX_VE_ENDPOINT, PROXMOX_VE_USERNAME, PROXMOX_VE_PASSWORD
# (te same, ktorych uzywa provider terraform).
set -euo pipefail

TF_DIR="${1:?Uzycie: pve-teardown.sh <katalog-terraform>}"
[ -d "$TF_DIR" ] || { echo "Brak katalogu: $TF_DIR" >&2; exit 1; }
: "${PROXMOX_VE_ENDPOINT:?Ustaw PROXMOX_VE_ENDPOINT}"
: "${PROXMOX_VE_USERNAME:?Ustaw PROXMOX_VE_USERNAME}"
: "${PROXMOX_VE_PASSWORD:?Ustaw PROXMOX_VE_PASSWORD}"

PVE_NODE="${PVE_NODE:-pve}"
PVE_STORAGE="${PVE_STORAGE:-local-zfs}"

# VMID-y bierzemy PRZED destroy — po nim stan terraform jest juz pusty.
# (while-read zamiast mapfile — macOS ma bash 3.2)
VMIDS=()
while IFS= read -r line; do
  [ -n "$line" ] && VMIDS+=("$line")
done < <(
  cd "$TF_DIR" && terraform output -json vms 2>/dev/null |
    python3 -c 'import sys,json; d=json.load(sys.stdin); print("\n".join(str(v["vmid"]) for v in d.values()))' 2>/dev/null
)
if [ "${#VMIDS[@]}" -eq 0 ]; then
  echo "UWAGA: nie odczytano VMID z terraform output — sprzatanie sierot pominiete." >&2
fi

echo "=== terraform destroy ($TF_DIR) ==="
( cd "$TF_DIR" && terraform destroy -auto-approve )

[ "${#VMIDS[@]}" -eq 0 ] && exit 0

echo "=== sprzatanie sierot ZFS dla VMID: ${VMIDS[*]} ==="
AUTH=$(curl -sk --max-time 20 -X POST "${PROXMOX_VE_ENDPOINT}/api2/json/access/ticket" \
  --data-urlencode "username=${PROXMOX_VE_USERNAME}" \
  --data-urlencode "password=${PROXMOX_VE_PASSWORD}")
TICKET=$(printf '%s' "$AUTH" | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["ticket"])')
CSRF=$(printf '%s' "$AUTH" | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["CSRFPreventionToken"])')

CONTENT=$(curl -sk --max-time 30 -b "PVEAuthCookie=${TICKET}" \
  "${PROXMOX_VE_ENDPOINT}/api2/json/nodes/${PVE_NODE}/storage/${PVE_STORAGE}/content")

removed=0
for vmid in "${VMIDS[@]}"; do
  # Wolumeny osierocone po tym konkretnym VMID (cloudinit, disk-N, ...).
  VOLS=()
  while IFS= read -r line; do
    [ -n "$line" ] && VOLS+=("$line")
  done < <(
    printf '%s' "$CONTENT" | python3 -c "
import sys, json
data = json.load(sys.stdin).get('data', []) or []
for item in data:
    volid = item.get('volid', '')
    if 'vm-${vmid}-' in volid:
        print(volid)
"
  )
  # "${VOLS[@]}" z pusta tablica + set -u wywala sie na bash 3.2 (macOS)
  [ "${#VOLS[@]}" -gt 0 ] || continue
  for vol in "${VOLS[@]}"; do
    [ -n "$vol" ] || continue
    code=$(curl -sk --max-time 60 -o /dev/null -w '%{http_code}' -X DELETE \
      -b "PVEAuthCookie=${TICKET}" -H "CSRFPreventionToken: ${CSRF}" \
      "${PROXMOX_VE_ENDPOINT}/api2/json/nodes/${PVE_NODE}/storage/${PVE_STORAGE}/content/${vol}")
    if [ "$code" = "200" ]; then
      echo "  usunieto sierote: $vol"
      removed=$((removed + 1))
    else
      echo "  UWAGA: nie udalo sie usunac $vol (HTTP $code)" >&2
    fi
  done
done
echo "=== teardown zakonczony (usunietych sierot: $removed) ==="
