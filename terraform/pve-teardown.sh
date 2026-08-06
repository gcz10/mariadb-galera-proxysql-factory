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

# --- Zabezpieczenie 1: katalog musi byc konfiguracja terraform Z TEGO repo ---
# Skrypt kasuje maszyny bezpowrotnie. Bez tej walidacji `pve-teardown.sh /`
# albo literowka w nazwie konczy sie `terraform destroy` w przypadkowym miejscu.
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
TF_ABS=$(cd "$TF_DIR" && pwd)
case "$TF_ABS" in
  "$REPO_ROOT"/terraform/*) ;;
  *) echo "ODMOWA: $TF_DIR jest poza $REPO_ROOT/terraform/" >&2; exit 1 ;;
esac
if ! ls "$TF_ABS"/*.tf >/dev/null 2>&1; then
  echo "ODMOWA: $TF_DIR nie zawiera zadnego pliku .tf — to nie jest konfiguracja terraform" >&2
  exit 1
fi

PVE_NODE="${PVE_NODE:-pve}"
PVE_STORAGE="${PVE_STORAGE:-local-zfs}"

# Opcjonalne argumenty po katalogu = nazwy wezlow do zniszczenia (np. gnode1 rnode1).
# Bez nich kasujemy CALY klaster.
shift || true
NODES=("$@")

# VMID-y bierzemy PRZED destroy — po nim stan terraform jest juz pusty.
# (while-read zamiast mapfile — macOS ma bash 3.2)
NODE_FILTER=""
if [ "${#NODES[@]}" -gt 0 ]; then
  NODE_FILTER=$(printf '%s,' "${NODES[@]}")
fi
VMIDS=()
while IFS= read -r line; do
  [ -n "$line" ] && VMIDS+=("$line")
done < <(
  cd "$TF_DIR" && terraform output -json vms 2>/dev/null |
    NODE_FILTER="$NODE_FILTER" python3 -c '
import sys, json, os
want = [n for n in os.environ.get("NODE_FILTER", "").split(",") if n]
data = json.load(sys.stdin)
for name, v in data.items():
    if not want or name in want:
        print(v["vmid"])
' 2>/dev/null
)
if [ "${#VMIDS[@]}" -eq 0 ]; then
  echo "UWAGA: nie odczytano VMID z terraform output — sprzatanie sierot pominiete." >&2
fi

# --- Zabezpieczenie 2: potwierdzenie musi POWTORZYC cel ---
# `CONFIRM_DESTROY=1` nie chroni przed niczym: wpisuje sie odruchowo i przenosi
# miedzy poleceniami. Zadamy dokladnej nazwy katalogu, wiec ani literowka w $1,
# ani skopiowana komenda dla innego klastra nie przejdzie.
DESTROY_WHAT=$( [ "${#NODES[@]}" -gt 0 ] && echo "wezly: ${NODES[*]}" || echo "CALY katalog" )

# Maszyny wspoldzielone: nazwa wystepujaca w inwentarzu wiecej niz jednego
# klastra oznacza, ze destroy odetnie tez te pozostale (ProxySQL, VIP, PMM).
# Wyliczane z repo, nie z listy nazw wpisanej na sztywno.
if [ -d "$REPO_ROOT/clusters" ]; then
  SHARED_WARN=$(
    TF_ABS="$TF_ABS" REPO_ROOT="$REPO_ROOT" NODE_FILTER="$NODE_FILTER" python3 - <<'PY' 2>/dev/null || true
import json, os, pathlib, subprocess, collections
tf, root = os.environ["TF_ABS"], pathlib.Path(os.environ["REPO_ROOT"])
want = [n for n in os.environ.get("NODE_FILTER", "").split(",") if n]
try:
    vms = json.loads(subprocess.run(["terraform", "output", "-json", "vms"],
                                    cwd=tf, capture_output=True, text=True).stdout)
except Exception:
    raise SystemExit
doomed = {n for n in vms if not want or n in want}
users = collections.defaultdict(set)
for inv in root.glob("clusters/*/inventory.yml"):
    text = inv.read_text()
    for host in doomed:
        if f"{host}:" in text:
            users[host].add(inv.parent.name)
shared = {h: c for h, c in users.items() if len(c) > 1}
if shared:
    for host, clusters in sorted(shared.items()):
        print(f"  {host} -> uzywany przez: {', '.join(sorted(clusters))}")
PY
  )
  if [ -n "$SHARED_WARN" ]; then
    echo "!!! WARSTWA WSPOLDZIELONA — destroy odetnie WIECEJ NIZ JEDEN klaster:" >&2
    echo "$SHARED_WARN" >&2
  fi
fi

if [ "${CONFIRM_DESTROY:-}" != "$TF_DIR" ]; then
  cat >&2 <<EOF
ODMOWA: brak potwierdzenia.
  cel:   $TF_DIR ($DESTROY_WHAT)
  VMID:  ${VMIDS[*]:-brak odczytu}
Aby wykonac, powtorz cel w zmiennej:
  CONFIRM_DESTROY=$TF_DIR $0 $TF_DIR${NODES[*]:+ ${NODES[*]}}
EOF
  exit 1
fi

if [ "${#NODES[@]}" -gt 0 ]; then
  echo "=== terraform destroy — TYLKO: ${NODES[*]} ==="
  TARGETS=()
  for n in "${NODES[@]}"; do
    TARGETS+=(-target="proxmox_virtual_environment_vm.node[\"$n\"]")
  done
  ( cd "$TF_DIR" && terraform destroy -auto-approve "${TARGETS[@]}" )
else
  echo "=== terraform destroy — CALY klaster ($TF_DIR) ==="
  ( cd "$TF_DIR" && terraform destroy -auto-approve )
fi

[ "${#VMIDS[@]}" -eq 0 ] && exit 0

echo "=== sprzatanie sierot ZFS dla VMID: ${VMIDS[*]} ==="
AUTH=$(curl -sk --max-time 20 -X POST "${PROXMOX_VE_ENDPOINT}/api2/json/access/ticket" \
  --data-urlencode "username=${PROXMOX_VE_USERNAME}" \
  --data-urlencode "password=${PROXMOX_VE_PASSWORD}")
# Bez sprawdzenia bilet bywa pusty (zle haslo, API nieosiagalne), a skrypt
# leci dalej z pustym cookie i sypie kaskada nieczytelnych 401.
if ! TICKET=$(printf '%s' "$AUTH" | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["ticket"])' 2>/dev/null) \
   || [ -z "$TICKET" ]; then
  echo "BLAD: uwierzytelnianie w PVE API nie powiodlo sie (sprawdz PROXMOX_VE_*)." >&2
  echo "Maszyny zostaly usuniete, ale sieroty ZFS dla VMID ${VMIDS[*]} trzeba sprzatnac recznie." >&2
  exit 1
fi
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
