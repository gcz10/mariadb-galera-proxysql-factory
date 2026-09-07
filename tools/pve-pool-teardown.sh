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
#   tools/pve-pool-teardown.sh                     # RAPORT (nic nie kasuje)
#   CONFIRM_POOL=<pula> tools/pve-pool-teardown.sh # kasuje sieroty puli
#
# Wymaga PROXMOX_VE_ENDPOINT i PROXMOX_VE_API_TOKEN. Pula: FLEET_POOL
# (domyslnie `claude-isa`) — ta sama zmienna, co w tests/lab/fleet-state.py.
#
# NAPRAWY PO AUDYCIE 2026-09-07:
#   1. FAIL-LOUD przy nieczytelnym `terraform output` — wczesniej
#      `2>/dev/null || true` zostawialo `managed` pusty, wiec CALA zywa flota
#      kwalifikowala sie jako "sieroty" i CONFIRM kasowal ja.
#   2. Polling zadania DELETE (UPID) — wczesniej odpowiedz byla odrzucana
#      i "skasowana" drukowano dla zadania, ktore moglo wlasnie padnac.
#   3. Bramka potwierdzenia powtorzeniem NAZWY PULY (CONFIRM_POOL), nie
#      odruchowym CONFIRM=yes, ktory przeciekal przez export miedzy celami.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
POOL="${FLEET_POOL:-claude-isa}"
: "${PROXMOX_VE_ENDPOINT:?Ustaw PROXMOX_VE_ENDPOINT}"
: "${PROXMOX_VE_API_TOKEN:?Ustaw PROXMOX_VE_API_TOKEN}"

# Poswiadczenia NIGDY nie ida w argv (byly widoczne w `ps` kazdemu na hoscie):
# repo ma na to wzorzec z pve-teardown.sh i pve-create-vm.sh — plik naglowkow
# 0600 czytany przez `curl -H @plik`. Python nie dostaje ich wcale: pobiera
# gotowy JSON ze standardowego wejscia.
AUTH_HEADER_FILE=$(mktemp)
chmod 600 "$AUTH_HEADER_FILE"
trap 'rm -f "$AUTH_HEADER_FILE"' EXIT
printf 'Authorization: PVEAPIToken=%s\n' "$PROXMOX_VE_API_TOKEN" > "$AUTH_HEADER_FILE"

# `--fail` we WSZYSTKICH wywolaniach: bez niego curl konczy sie zerem takze przy
# HTTP 500/403, wiec skrypt raportowalby pusta pule albo "skasowana" dla maszyny,
# ktora zyje.
api() {
  curl -sS --fail -k -H @"$AUTH_HEADER_FILE" "$@"
}

# Stan terraform czytamy PRZED jakakolwiek mutacja: maszyna widziana przez
# jakikolwiek root NIE jest sierota puli i nalezy do `infra-teardown`.
#
# Root bez czytelnego outputu ZATRZYMUJE skrypt przed raportem i przed
# jakimkolwiek kasowaniem — pusty `managed` oznaczalby, ze zywe maszyny
# wygladaja jak sieroty.
managed=""
for root in "$REPO_ROOT"/terraform/*/; do
  [ -f "$root/main.tf" ] || continue
  ids=$(terraform -chdir="$root" output -json vms 2>/dev/null |
    python3 -c 'import json,sys
try:
    data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    raise SystemExit(1)
if not isinstance(data, dict):
    raise SystemExit(1)
print(" ".join(str(vm["vmid"]) for vm in data.values() if isinstance(vm, dict) and "vmid" in vm))') || {
    echo "FAIL: terraform output dla $root nieczytelny (backend/lock/state) — odmawiam wyznaczania sierot" >&2
    exit 1
  }
  managed="$managed $ids"
done

REPORT=$(api "${PROXMOX_VE_ENDPOINT%/}/api2/json/cluster/resources?type=vm" |
  POOL="$POOL" MANAGED="$managed" python3 -c '
import json, os, sys

resources = json.load(sys.stdin)["data"]
managed = {int(v) for v in os.environ["MANAGED"].split()}
for vm in sorted(resources, key=lambda v: v.get("vmid", 0)):
    if vm.get("pool") != os.environ["POOL"] or vm.get("vmid") in managed:
        continue
    print(vm["vmid"], vm.get("node", ""), vm.get("name", ""), vm.get("status", ""))
'
)

if [ -z "$REPORT" ]; then
  echo "OK: pula '$POOL' nie ma maszyn spoza stanu terraform"
  exit 0
fi

echo "Maszyny puli '$POOL' POZA stanem terraform:"
echo "$REPORT" | sed 's/^/  /'

# Bramka: potwierdzenie powtarza DOKLADNIE nazwe puli. `CONFIRM=yes` mial
# tendencje do przezywania w srodowisku (export) i przenosil sie miedzy celami.
if [ "${CONFIRM_POOL:-}" != "$POOL" ]; then
  count=$(echo "$REPORT" | wc -l | tr -d ' ')
  echo
  echo "Cel: pula '$POOL' na $PROXMOX_VE_ENDPOINT — $count maszyn do KASOWANIA." >&2
  echo "RAPORT — nic nie skasowano. Powtorz z CONFIRM_POOL='$POOL', zeby je usunac." >&2
  exit 1
fi

# Oczekiwanie na zakonczenie zadania asynchronicznego PVE (UPID). Bez tego
# "skasowana" oznaczalo wylacznie "zlecilam zadanie" — porazka taska wychodzila
# dopiero przy kolejnym apply na tym VMID.
wait_task() {
  local upid="$1" node="$2" what="$3" enc res i
  enc=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$upid")
  for i in $(seq 1 120); do
    res=$(api "${PROXMOX_VE_ENDPOINT%/}/api2/json/nodes/$node/tasks/$enc/status" |
      python3 -c 'import json,sys
d=json.load(sys.stdin).get("data") or {}
print(d.get("status",""), d.get("exitstatus",""))') || return 1
    case "$res" in
      "stopped OK")
        echo "   $what: OK"
        return 0
        ;;
      "stopped "*)
        echo "FAIL: $what zakonczone bledem: ${res#stopped }" >&2
        return 1
        ;;
    esac
    sleep 2
  done
  echo "FAIL: timeout oczekiwania na $what ($upid)" >&2
  return 1
}

# Pętla biegnie w subshellu potoku (`while read`), wiec `exit 1` wewnatrz
# ustawia tylko kod potoku — bez `pipefail`-swiadomego przekazania rc do
# glownej powloki skrypt konczylby sukces mimo porazki taska (audit 2026-09-07).
rc_delete=0
while read -r vmid node name status; do
  echo "== $vmid ($name) na $node: $status"
  if [ "$status" = "running" ]; then
    stop_upid=$(api -X POST "${PROXMOX_VE_ENDPOINT%/}/api2/json/nodes/$node/qemu/$vmid/status/stop" |
      python3 -c 'import json,sys; print(json.load(sys.stdin).get("data") or "")') || true
    if [ -n "$stop_upid" ]; then
      wait_task "$stop_upid" "$node" "stop $vmid" || { echo "FAIL: $vmid — stop nieudany" >&2; rc_delete=1; break; }
    fi
  fi
  # purge=1 zdejmuje wpisy z zadan zapasowych i replikacji, drugi parametr
  # kasuje wolumeny nieprzypisane do konfiguracji — bez tego zostaja sieroty ZFS
  # i kolejny apply na tym samym VMID pada na "dataset already exists".
  del_upid=$(api -X DELETE "${PROXMOX_VE_ENDPOINT%/}/api2/json/nodes/$node/qemu/$vmid?purge=1&destroy-unreferenced-disks=1" |
    python3 -c 'import json,sys; print(json.load(sys.stdin).get("data") or "")') || true
  if [ -z "$del_upid" ]; then
    echo "FAIL: $vmid — PVE nie zwrocilo UPID zadania DELETE" >&2
    rc_delete=1
    break
  fi
  if ! wait_task "$del_upid" "$node" "delete $vmid"; then
    rc_delete=1
    break
  fi
  echo "   skasowana"
done <<EOF2
$REPORT
EOF2
if [ "$rc_delete" -ne 0 ]; then
  echo "FAIL: przynajmniej jedna maszyna NIE zostala skasowana — nie wolno raportowac sukcesu" >&2
  exit 1
fi

echo "OK: sieroty puli '$POOL' usuniete"
