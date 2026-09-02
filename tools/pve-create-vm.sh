#!/usr/bin/env bash
# pve-create-vm.sh — Utwórz maszynę wirtualną na Proxmox VE przez REST API bez Terraforma.
# Enkapsulacja procedury z docs/runbooks/machines-from-elsewhere.md.
#
# Wymaga zmiennych środowiskowych:
#   PROXMOX_VE_ENDPOINT  — np. https://192.168.1.181:8006
#   PROXMOX_VE_API_TOKEN — np. root@pam!isa-tf=...
#   PROXMOX_VE_NODE      — opcjonalnie, domyślnie "pve"
#
# Przykłady:
#   ./tools/pve-create-vm.sh --vmid 10020 --name c12db1 --ip 40 --cluster cassiopeiav12-r9
#   ./tools/pve-create-vm.sh --vmid 10023 --name c12r1 --ip 192.168.1.43 --cluster cassiopeiav12-r9 --role restore --ram 2560

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  cat <<EOF
Użycie: $0 [OPCJE]

Wymagane parametry:
  --vmid <ID>          Identyfikator VMID w Proxmox (np. 10020)
  --name <NAZWA>       Nazwa maszyny (np. c12db1)
  --ip <IP|OKTET>      Adres IP (np. 192.168.1.40 lub ostatni oktet: 40)
  --cluster <KLASTER>  Nazwa klastra dla tagów (np. cassiopeiav12-r9)

Opcje:
  --role <ROLA>        Rola węzła: galera, restore, proxysql, app (domyślnie: galera)
  --image <STORAGE:IMG> Obraz chmurowy (domyślnie: local:import/Rocky-9.8-GenericCloud.qcow2)
  --cores <N>          Liczba rdzeni CPU (domyślnie: 2)
  --ram <MB>           Pamięć RAM w MB (domyślnie: 3072)
  --disk <GB>          Rozmiar dysku w GB po rozszerzeniu (domyślnie: 40)
  --pool <POOL>        Pula Proxmox (domyślnie: claude-isa)
  --node <NODE>        Węzeł Proxmox (domyślnie: z PROXMOX_VE_NODE lub 'pve')
  --gateway <IP>       Brama domyślna (domyślnie: 192.168.1.1)
  --key-file <SCIEZKA> Ścieżka do klucza publicznego (domyślnie: secrets/ssh_key.pub)
  --no-wait-ssh        Pomiń oczekiwanie na podniesienie portu SSH (port 22)
  -h, --help           Pokaż tę pomoc
EOF
  exit "${1:-0}"
}

VMID=""
NAME=""
IP_INPUT=""
CLUSTER=""
ROLE="galera"
IMAGE="local:import/Rocky-9.8-GenericCloud.qcow2"
CORES=2
RAM=3072
DISK=40
POOL="claude-isa"
NODE="${PROXMOX_VE_NODE:-pve}"
GATEWAY="192.168.1.1"
KEY_FILE="$REPO_ROOT/secrets/ssh_key.pub"
WAIT_SSH=true

while [ $# -gt 0 ]; do
  case "$1" in
    --vmid)       VMID="$2"; shift 2 ;;
    --name)       NAME="$2"; shift 2 ;;
    --ip)         IP_INPUT="$2"; shift 2 ;;
    --cluster)    CLUSTER="$2"; shift 2 ;;
    --role)       ROLE="$2"; shift 2 ;;
    --galera)     ROLE="galera"; shift ;;
    --restore)    ROLE="restore"; shift ;;
    --image)      IMAGE="$2"; shift 2 ;;
    --cores)      CORES="$2"; shift 2 ;;
    --ram)        RAM="$2"; shift 2 ;;
    --disk)       DISK="$2"; shift 2 ;;
    --pool)       POOL="$2"; shift 2 ;;
    --node)       NODE="$2"; shift 2 ;;
    --gateway)    GATEWAY="$2"; shift 2 ;;
    --key-file)   KEY_FILE="$2"; shift 2 ;;
    --no-wait-ssh) WAIT_SSH=false; shift ;;
    -h|--help)    usage 0 ;;
    *) echo "BŁĄD: Nieznany parametr: $1" >&2; usage 2 ;;
  esac
done

# Walidacja wymaganych parametrów
[ -n "$VMID" ] || { echo "BŁĄD: --vmid jest wymagany" >&2; exit 2; }
[ -n "$NAME" ] || { echo "BŁĄD: --name jest wymagany" >&2; exit 2; }
[ -n "$IP_INPUT" ] || { echo "BŁĄD: --ip jest wymagany" >&2; exit 2; }
[ -n "$CLUSTER" ] || { echo "BŁĄD: --cluster jest wymagany" >&2; exit 2; }

# Normalizacja adresu IP
if [[ "$IP_INPUT" =~ ^[0-9]+$ ]]; then
  IP_FULL="192.168.1.$IP_INPUT"
elif [[ "$IP_INPUT" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  IP_FULL="$IP_INPUT"
else
  echo "BŁĄD: Niepoprawny format IP: '$IP_INPUT'" >&2
  exit 2
fi

# Walidacja poświadczeń PVE
: "${PROXMOX_VE_ENDPOINT:?BŁĄD: Ustaw zmienną środowiskową PROXMOX_VE_ENDPOINT}"
: "${PROXMOX_VE_API_TOKEN:?BŁĄD: Ustaw zmienną środowiskową PROXMOX_VE_API_TOKEN}"

[ -f "$KEY_FILE" ] || { echo "BŁĄD: Brak pliku klucza SSH: $KEY_FILE" >&2; exit 2; }

EP="${PROXMOX_VE_ENDPOINT%/}"
H="Authorization: PVEAPIToken=$PROXMOX_VE_API_TOKEN"

echo "=== [1/5] Pre-flight check: unikalność VMID $VMID na węźle $NODE ==="
vm_exists=$(curl -sk -H "$H" "$EP/api2/json/nodes/$NODE/qemu" \
  | python3 -c "import json,sys; print(any(int(v.get('vmid',0))==$VMID for v in (json.load(sys.stdin).get('data') or [])))")

if [ "$vm_exists" = "True" ]; then
  echo "BŁĄD: Maszyna o VMID $VMID już istnieje na węźle $NODE!" >&2
  exit 1
fi

vol_exists=$(curl -sk -H "$H" "$EP/api2/json/nodes/$NODE/storage/local-zfs/content" \
  | python3 -c "import json,sys; print([c.get('volid') for c in (json.load(sys.stdin).get('data') or []) if str(c.get('vmid'))=='$VMID'])")

if [ "$vol_exists" != "[]" ]; then
  echo "BŁĄD: Znaleziono osierocone wolumeny dla VMID $VMID: $vol_exists" >&2
  echo "      Usuń je przed utworzeniem maszyny, aby uniknąć kolizji ZFS." >&2
  exit 1
fi
echo "VMID $VMID jest wolny w QEMU i storage/local-zfs."

# Pomocnik oczekiwania na asynchroniczny task w Proxmoxie
wait_task() {
  local upid="$1"
  local step_name="${2:-Zadanie}"
  local enc
  enc=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$upid")

  for i in $(seq 1 120); do
    local res
    res=$(curl -sk -H "$H" "$EP/api2/json/nodes/$NODE/tasks/$enc/status" \
      | python3 -c "import json,sys
d=json.load(sys.stdin).get('data') or {}
print(d.get('status',''), d.get('exitstatus',''))")

    case "$res" in
      "stopped OK")
        echo "  -> $step_name: OK"
        return 0
        ;;
      "stopped "*)
        local exit_detail="${res#stopped }"
        echo "BŁĄD: $step_name zakończył się błędem: $exit_detail" >&2
        return 1
        ;;
    esac
    sleep 2
  done

  echo "BŁĄD: Timeout oczekiwania na $step_name ($upid)" >&2
  return 1
}

# Zakoduj klucz publiczny SSH
KEY_ENC=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(open(sys.argv[1]).read(), safe=""))' "$KEY_FILE")

echo "=== [2/5] Tworzenie maszyny VM $NAME (VMID $VMID, IP: $IP_FULL) ==="
TAGS="rocky,$ROLE,$CLUSTER"
CREATE_RESP=$(curl -sk -H "$H" -X POST "$EP/api2/json/nodes/$NODE/qemu" \
  --data-urlencode "vmid=$VMID" \
  --data-urlencode "name=$NAME" \
  --data-urlencode "pool=$POOL" \
  --data-urlencode "cores=$CORES" \
  --data-urlencode "cpu=host" \
  --data-urlencode "memory=$RAM" \
  --data-urlencode "balloon=0" \
  --data-urlencode "agent=enabled=1,type=virtio" \
  --data-urlencode "virtio0=local-zfs:0,import-from=$IMAGE,aio=io_uring,discard=on" \
  --data-urlencode "ide2=local-zfs:cloudinit" \
  --data-urlencode "net0=virtio,bridge=vmbr0,firewall=0" \
  --data-urlencode "boot=order=virtio0;net0" \
  --data-urlencode "ciuser=root" \
  --data-urlencode "sshkeys=$KEY_ENC" \
  --data-urlencode "ipconfig0=gw=$GATEWAY,ip=$IP_FULL/24" \
  --data-urlencode "nameserver=1.1.1.1 8.8.8.8" \
  --data-urlencode "tags=$TAGS" \
  --data-urlencode "onboot=1")

CREATE_UPID=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('data') or '')" "$CREATE_RESP")
if [ -z "$CREATE_UPID" ]; then
  echo "BŁĄD: Nie udało się zlecić utworzenia VM: $CREATE_RESP" >&2
  exit 1
fi
wait_task "$CREATE_UPID" "Tworzenie i import obrazu ($IMAGE)"

echo "=== [3/5] Rozszerzenie dysku virtio0 do ${DISK}G ==="
RESIZE_RESP=$(curl -sk -H "$H" -X PUT "$EP/api2/json/nodes/$NODE/qemu/$VMID/resize" \
  --data-urlencode "disk=virtio0" \
  --data-urlencode "size=${DISK}G")

RESIZE_UPID=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('data') or '')" "$RESIZE_RESP")
if [ -n "$RESIZE_UPID" ]; then
  wait_task "$RESIZE_UPID" "Resize dysku virtio0 do ${DISK}G"
else
  # Czasami resize jest synchroniczny lub zwraca puste data przy natychmiastowym sukcesie
  echo "  -> Resize zlecony."
fi

echo "=== [4/5] Uruchomienie VM $NAME ==="
START_RESP=$(curl -sk -H "$H" -X POST "$EP/api2/json/nodes/$NODE/qemu/$VMID/status/start")
START_UPID=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('data') or '')" "$START_RESP")
if [ -n "$START_UPID" ]; then
  wait_task "$START_UPID" "Start maszyny $NAME"
fi

echo "Maszyna $NAME (VMID: $VMID) uruchomiona."

if [ "$WAIT_SSH" = true ]; then
  echo "=== [5/5] Oczekiwanie na podniesienie SSH ($IP_FULL:22) ==="
  for i in $(seq 1 60); do
    if timeout 2 bash -c "echo > /dev/tcp/$IP_FULL/22" 2>/dev/null; then
      echo "Sukces: $NAME ($VMID, $IP_FULL) odpowiada na porcie SSH 22 (po $((i * 3))s)."
      exit 0
    fi
    sleep 3
  done
  echo "OSTRZEŻENIE: Maszyna wystartowała, ale port 22 na $IP_FULL nie odpowiedział w ciągu 180s." >&2
  echo "             Sprawdź konsolę PVE lub cloud-init w razie problemów." >&2
  exit 0
else
  echo "Pominięto oczekiwanie na SSH (--no-wait-ssh)."
  exit 0
fi
