#!/usr/bin/env bash
# pve-create-vm.sh — Utwórz maszynę wirtualną na Proxmox VE przez REST API bez Terraforma.
#
# Narzędzie jest GENERYCZNE: nic, co opisuje konkretną instalację PVE (adresacja,
# brama, pula, węzeł, storage, obraz, most sieciowy, DNS), nie ma tu wartości
# domyślnej. Wywołujący podaje pełny adres IPv4 z prefiksem (albo osobny
# --prefix) i resztę topologii jawnie. Domyślne zostają wyłącznie parametry
# samej maszyny (rdzenie/RAM/dysk).
#
# Wymaga zmiennych środowiskowych:
#   PROXMOX_VE_ENDPOINT  — np. https://pve.example:8006
#   PROXMOX_VE_API_TOKEN — np. root@pam!isa-tf=...
#   PROXMOX_VE_NODE      — węzeł PVE, gdy brak --node
#   FLEET_POOL           — pula PVE tej infrastruktury, gdy brak --pool
#
# Przykłady (adresy RFC 5737 — dokumentacyjne, nie są adresacją żadnego labu):
#   ./tools/pve-create-vm.sh --vmid 10020 --name node1 --ip 192.0.2.10/24 \
#     --gateway 192.0.2.1 --cluster example-c1 --pool example-pool --node pve1 \
#     --storage local-zfs --image local:import/Rocky-9.8-GenericCloud.qcow2 \
#     --nameserver "1.1.1.1 8.8.8.8" --bridge vmbr0
#   ./tools/pve-create-vm.sh --vmid 10023 --name node2 --ip 192.0.2.11 --prefix 24 \
#     --gateway 192.0.2.1 --cluster example-c1 --pool example-pool --node pve1 \
#     --storage local-zfs --image local:import/Rocky-9.8-GenericCloud.qcow2 \
#     --nameserver "1.1.1.1 8.8.8.8" --bridge vmbr0 --role restore

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  cat <<EOF
Użycie: $0 [OPCJE]

Wymagane parametry:
  --vmid <ID>            Identyfikator VMID w Proxmox (np. 10020)
  --name <NAZWA>         Nazwa maszyny (np. node1)
  --ip <IP|IP/PREFIKS>   Pełny adres IPv4; prefiks sieci w CIDR lub przez --prefix
                         (np. 192.0.2.10/24 albo --ip 192.0.2.10 --prefix 24)
  --gateway <IP>         Brama domyślna TEJ sieci (np. 192.0.2.1)
  --cluster <KLASTER>    Nazwa klastra dla tagów (np. example-c1)
  --pool <POOL>          Pula Proxmox tej infrastruktury (albo zmienna FLEET_POOL)
  --node <NODE>          Węzeł Proxmox tej infrastruktury (albo PROXMOX_VE_NODE)
  --bridge <MOST>        Most sieciowy VM na tym węźle (np. vmbr0)
  --storage <STORAGE>    Storage PVE na dysk VM i cloud-init (np. local-zfs)
  --image <STORAGE:IMG>  Obraz chmurowy do importu (np. local:import/Rocky-9.8-GenericCloud.qcow2)
  --nameserver <LISTA>   Serwery DNS dla VM, rozdzielone spacją (np. "1.1.1.1 8.8.8.8")

Opcje:
  --prefix <BITS>        Prefiks sieci (1-32), gdy --ip podano bez "/PREFIKS"
  --role <ROLA>          Rola węzła: galera, restore, proxysql, app (domyślnie: galera)
  --cores <N>            Liczba rdzeni CPU (domyślnie: 2)
  --ram <MB>             Pamięć RAM w MB (domyślnie: 3072)
  --disk <GB>            Rozmiar dysku w GB po rozszerzeniu (domyślnie: 40)
  --key-file <SCIEZKA>   Ścieżka do klucza publicznego (domyślnie: secrets/ssh_key.pub)
  --no-wait-ssh          Pomiń oczekiwanie na podniesienie portu SSH (port 22)
  -h, --help             Pokaż tę pomoc
EOF
  exit "${1:-0}"
}

VMID=""
NAME=""
IP_INPUT=""
IP_PREFIX=""
CLUSTER=""
ROLE="galera"
IMAGE=""
CORES=2
RAM=3072
DISK=40
STORAGE=""
POOL="${FLEET_POOL:-}"
NODE="${PROXMOX_VE_NODE:-}"
BRIDGE=""
GATEWAY=""
NAMESERVER=""
KEY_FILE="$REPO_ROOT/secrets/ssh_key.pub"
WAIT_SSH=true

while [ $# -gt 0 ]; do
  case "$1" in
    --vmid)       VMID="$2"; shift 2 ;;
    --name)       NAME="$2"; shift 2 ;;
    --ip)         IP_INPUT="$2"; shift 2 ;;
    --prefix)     IP_PREFIX="$2"; shift 2 ;;
    --cluster)    CLUSTER="$2"; shift 2 ;;
    --role)       ROLE="$2"; shift 2 ;;
    --galera)     ROLE="galera"; shift ;;
    --restore)    ROLE="restore"; shift ;;
    --image)      IMAGE="$2"; shift 2 ;;
    --storage)    STORAGE="$2"; shift 2 ;;
    --cores)      CORES="$2"; shift 2 ;;
    --ram)        RAM="$2"; shift 2 ;;
    --disk)       DISK="$2"; shift 2 ;;
    --pool)       POOL="$2"; shift 2 ;;
    --node)       NODE="$2"; shift 2 ;;
    --bridge)     BRIDGE="$2"; shift 2 ;;
    --gateway)    GATEWAY="$2"; shift 2 ;;
    --nameserver) NAMESERVER="$2"; shift 2 ;;
    --key-file)   KEY_FILE="$2"; shift 2 ;;
    --no-wait-ssh) WAIT_SSH=false; shift ;;
    -h|--help)    usage 0 ;;
    *) echo "BŁĄD: Nieznany parametr: $1" >&2; usage 2 ;;
  esac
done

# Walidacja wymaganych parametrów. Wszystko, co zależy od konkretnej
# infrastruktury (adresacja, brama, pula, węzeł, storage, obraz, most, DNS),
# MUSI być jawne — brak wartości przerywa pracę zanim padnie jakiekolwiek
# zapytanie do PVE.
[ -n "$VMID" ] || { echo "BŁĄD: --vmid jest wymagany" >&2; exit 2; }
[ -n "$NAME" ] || { echo "BŁĄD: --name jest wymagany" >&2; exit 2; }
[ -n "$IP_INPUT" ] || { echo "BŁĄD: --ip jest wymagany" >&2; exit 2; }
[ -n "$CLUSTER" ] || { echo "BŁĄD: --cluster jest wymagany" >&2; exit 2; }
[ -n "$POOL" ] || { echo "BŁĄD: --pool jest wymagany (albo ustaw FLEET_POOL na pulę tej infrastruktury)" >&2; exit 2; }
[ -n "$GATEWAY" ] || { echo "BŁĄD: --gateway jest wymagany" >&2; exit 2; }
[ -n "$BRIDGE" ] || { echo "BŁĄD: --bridge jest wymagany" >&2; exit 2; }
[ -n "$NODE" ] || { echo "BŁĄD: --node jest wymagany (albo ustaw PROXMOX_VE_NODE)" >&2; exit 2; }
[ -n "$STORAGE" ] || { echo "BŁĄD: --storage jest wymagany" >&2; exit 2; }
[ -n "$IMAGE" ] || { echo "BŁĄD: --image jest wymagany" >&2; exit 2; }
[ -n "$NAMESERVER" ] || { echo "BŁĄD: --nameserver jest wymagany" >&2; exit 2; }

# AUDIT 2026-09-07: VMID leci do zapytań PVE i do pythona — musi być liczbą,
# zanim cokolwiek go zinterpoluje.
[[ "$VMID" =~ ^[0-9]+$ ]] || { echo "BŁĄD: --vmid musi być liczbą (podano: '$VMID')" >&2; exit 2; }
[ "$VMID" -ge 100 ] && [ "$VMID" -le 999999999 ] || { echo "BŁĄD: --vmid poza zakresem PVE (100-999999999)" >&2; exit 2; }

octet_ok() { local o="$1"; [[ "$o" =~ ^[0-9]+$ ]] && [ "$o" -ge 0 ] && [ "$o" -le 255 ]; }
prefix_ok() { local p="$1"; [[ "$p" =~ ^[0-9]+$ ]] && [ "$p" -ge 1 ] && [ "$p" -le 32 ]; }

# Normalizacja adresu IP: przyjmujemy WYŁĄCZNIE pełny IPv4, opcjonalnie z /prefiksem.
# Skrót "ostatni oktet" był zgadywaniem sieci labu — nie istnieje w tym narzędziu.
CIDR_PREFIX=""
if [[ "$IP_INPUT" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)/([0-9]+)$ ]]; then
  IP_OCTETS=("${BASH_REMATCH[@]:1:4}")
  CIDR_PREFIX="${BASH_REMATCH[5]}"
  IP_ADDR="${IP_INPUT%%/*}"
elif [[ "$IP_INPUT" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  IP_OCTETS=("${BASH_REMATCH[@]:1}")
  IP_ADDR="$IP_INPUT"
else
  echo "BŁĄD: Niepoprawny format IP: '$IP_INPUT' (wymagane IPv4, opcjonalnie z /PREFIKS)" >&2
  exit 2
fi

for o in "${IP_OCTETS[@]}"; do
  octet_ok "$o" || { echo "BŁĄD: oktet poza zakresem 0-255 w '$IP_INPUT'" >&2; exit 2; }
done

if [ -n "$CIDR_PREFIX" ]; then
  prefix_ok "$CIDR_PREFIX" || { echo "BŁĄD: prefiks poza zakresem 1-32 w '$IP_INPUT'" >&2; exit 2; }
  if [ -n "$IP_PREFIX" ] && [ "$IP_PREFIX" != "$CIDR_PREFIX" ]; then
    echo "BŁĄD: prefiks podany dwa razy i sprzecznie ('$IP_INPUT' vs --prefix '$IP_PREFIX')" >&2
    exit 2
  fi
  IP_PREFIX="$CIDR_PREFIX"
fi

[ -n "$IP_PREFIX" ] || { echo "BŁĄD: brak prefiksu sieci: podaj --ip <IP>/<PREFIKS> albo --prefix <BITS>" >&2; exit 2; }
prefix_ok "$IP_PREFIX" || { echo "BŁĄD: --prefix musi być liczbą 1-32 (podano: '$IP_PREFIX')" >&2; exit 2; }
IP_CIDR="$IP_ADDR/$IP_PREFIX"

case "$ROLE" in
  galera|restore|proxysql|app) ;;
  *) echo "BŁĄD: --role musi być jednym z: galera, restore, proxysql, app (podano: '$ROLE')" >&2; exit 2 ;;
esac

for num_var in CORES RAM DISK; do
  eval "val=\${$num_var}"
  [[ "$val" =~ ^[0-9]+$ ]] && [ "$val" -gt 0 ] || { echo "BŁĄD: --$(echo "$num_var" | tr 'A-Z' 'a-z') musi być dodatnią liczbą (podano: '$val')" >&2; exit 2; }
done
[[ "$GATEWAY" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "BŁĄD: --gateway musi być adresem IPv4 (podano: '$GATEWAY')" >&2; exit 2; }

# AUDIT 2026-09-21: nazwa mostu i storage trafiają bezpośrednio do wartości
# parametrów PVE (przecinki rozdzielają pola), a CLUSTER do listy tagów — znaki
# strukturalne muszą być odrzucone, zanim cokolwiek pójdzie w request.
PVE_NAME_RE='^[A-Za-z0-9][A-Za-z0-9._-]*$'
[[ "$STORAGE" =~ $PVE_NAME_RE ]] || { echo "BŁĄD: --storage musi być nazwą storage PVE (podano: '$STORAGE')" >&2; exit 2; }
[[ "$BRIDGE" =~ $PVE_NAME_RE ]] || { echo "BŁĄD: --bridge musi być nazwą mostu PVE (podano: '$BRIDGE')" >&2; exit 2; }
[[ "$CLUSTER" =~ $PVE_NAME_RE ]] || { echo "BŁĄD: --cluster musi być nazwą użyteczną jako tag PVE (podano: '$CLUSTER')" >&2; exit 2; }
NS_RE='^[-0-9A-Za-z._:]+( [-0-9A-Za-z._:]+)*$'
[[ "$NAMESERVER" =~ $NS_RE ]] || { echo "BŁĄD: --nameserver musi być listą serwerów DNS rozdzieloną spacjami (podano: '$NAMESERVER')" >&2; exit 2; }

# Walidacja poświadczeń PVE. Sprawdzamy obecność zmiennych PRZED pierwszym curl-em,
# żeby brak konfiguracji nie kończył się żądaniem do nieustawionego endpointu.
if [ -z "${PROXMOX_VE_ENDPOINT:-}" ]; then
  echo "BŁĄD: Ustaw zmienną środowiskową PROXMOX_VE_ENDPOINT" >&2
  exit 2
fi
if [ -z "${PROXMOX_VE_API_TOKEN:-}" ]; then
  echo "BŁĄD: Ustaw zmienną środowiskową PROXMOX_VE_API_TOKEN" >&2
  exit 2
fi
EP="${PROXMOX_VE_ENDPOINT%/}"
WAIT_RETRIES="${PVE_SSH_WAIT_RETRIES:-90}"
WAIT_SLEEP="${PVE_SSH_WAIT_SLEEP:-3}"

if ! [[ "$WAIT_RETRIES" =~ ^[0-9]+$ ]] || [ "$WAIT_RETRIES" -le 0 ]; then
  echo "BŁĄD: PVE_SSH_WAIT_RETRIES musi być dodatnią liczbą całkowitą (podano: '$PVE_SSH_WAIT_RETRIES')" >&2
  exit 2
fi

if ! [[ "$WAIT_SLEEP" =~ ^[0-9]+$ ]]; then
  echo "BŁĄD: PVE_SSH_WAIT_SLEEP musi być nieujemną liczbą całkowitą (podano: '$PVE_SSH_WAIT_SLEEP')" >&2
  exit 2
fi

[ -f "$KEY_FILE" ] || { echo "BŁĄD: Brak pliku klucza SSH: $KEY_FILE" >&2; exit 2; }

# Bezpieczne przekazanie tokenu API do curl przez plik tymczasowy 0600 (zapobiega wyciekowi do argv / ps)
AUTH_HEADER_FILE=$(mktemp "${TMPDIR:-/tmp}/pve-auth.XXXXXX")
chmod 0600 "$AUTH_HEADER_FILE"
trap 'rm -f "$AUTH_HEADER_FILE"' EXIT
printf 'Authorization: PVEAPIToken=%s\n' "$PROXMOX_VE_API_TOKEN" > "$AUTH_HEADER_FILE"

# AUDIT 2026-09-07: `-sk` bez `--fail` zwraca rc=0 przy HTTP 500/403 (HTML idzie
# do parsera i wywala go tracebackiem), a bez `--max-time` czarny endpoint
# wiesza skrypt. Wspolne flagi dla KAZDEGO wywolania.
api() {
  curl -sS --fail -k --max-time 30 -H @"$AUTH_HEADER_FILE" "$@"
}

echo "=== [1/5] Pre-flight check: unikalność VMID $VMID na węźle $NODE ==="
vm_exists=$(api "$EP/api2/json/nodes/$NODE/qemu" \
  | python3 -c "import json,sys; print(any(int(v.get('vmid',0))==$VMID for v in (json.load(sys.stdin).get('data') or [])))")

if [ "$vm_exists" = "True" ]; then
  echo "BŁĄD: Maszyna o VMID $VMID już istnieje na węźle $NODE!" >&2
  exit 1
fi

vol_exists=$(api "$EP/api2/json/nodes/$NODE/storage/$STORAGE/content" \
  | python3 -c "import json,sys; print([c.get('volid') for c in (json.load(sys.stdin).get('data') or []) if str(c.get('vmid'))=='$VMID'])")
if [ "$vol_exists" != "[]" ]; then
  echo "BŁĄD: Znaleziono osierocone wolumeny dla VMID $VMID: $vol_exists" >&2
  echo "      Usuń je przed utworzeniem maszyny, aby uniknąć kolizji ZFS." >&2
  exit 1
fi
echo "VMID $VMID jest wolny w QEMU i storage/$STORAGE."

# Pomocnik oczekiwania na asynchroniczny task w Proxmoxie
wait_task() {
  local upid="$1"
  local step_name="${2:-Zadanie}"
  local enc
  enc=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$upid")

  for i in $(seq 1 120); do
    local res
    res=$(api "$EP/api2/json/nodes/$NODE/tasks/$enc/status" \
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

echo "=== [2/5] Tworzenie maszyny VM $NAME (VMID $VMID, IP: $IP_CIDR) ==="
TAGS="rocky;$ROLE;$CLUSTER"
CREATE_RESP=$(api -X POST "$EP/api2/json/nodes/$NODE/qemu" \
  --data-urlencode "vmid=$VMID" \
  --data-urlencode "name=$NAME" \
  --data-urlencode "pool=$POOL" \
  --data-urlencode "cores=$CORES" \
  --data-urlencode "cpu=host" \
  --data-urlencode "memory=$RAM" \
  --data-urlencode "balloon=0" \
  --data-urlencode "agent=enabled=1,type=virtio" \
  --data-urlencode "virtio0=$STORAGE:0,import-from=$IMAGE,aio=io_uring,discard=on" \
  --data-urlencode "ide2=$STORAGE:cloudinit" \
  --data-urlencode "net0=virtio,bridge=$BRIDGE,firewall=0" \
  --data-urlencode "boot=order=virtio0;net0" \
  --data-urlencode "ciuser=root" \
  --data-urlencode "sshkeys=$KEY_ENC" \
  --data-urlencode "ipconfig0=gw=$GATEWAY,ip=$IP_CIDR" \
  --data-urlencode "nameserver=$NAMESERVER" \
  --data-urlencode "tags=$TAGS" \
  --data-urlencode "onboot=1")

CREATE_UPID=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('data') or '')" "$CREATE_RESP")
if [ -z "$CREATE_UPID" ]; then
  echo "BŁĄD: Nie udało się zlecić utworzenia VM: $CREATE_RESP" >&2
  exit 1
fi
wait_task "$CREATE_UPID" "Tworzenie i import obrazu ($IMAGE)"

echo "=== [3/5] Rozszerzenie dysku virtio0 do ${DISK}G ==="
RESIZE_RESP=$(api -X PUT "$EP/api2/json/nodes/$NODE/qemu/$VMID/resize" \
  --data-urlencode "size=${DISK}G")

RESIZE_UPID=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('data') or '')" "$RESIZE_RESP")
if [ -n "$RESIZE_UPID" ]; then
  wait_task "$RESIZE_UPID" "Resize dysku virtio0 do ${DISK}G"
elif [ "$RESIZE_RESP" = "" ] || echo "$RESIZE_RESP" | grep -q '"errors"'; then
  echo "BŁĄD: Nie udało się zlecić resize: $RESIZE_RESP" >&2
  exit 1
else
  # Puste data przy natychmiastowym sukcesie resize (synchroniczna ścieżka PVE)
  echo "  -> Resize zlecony (synchronicznie)."
fi

echo "=== [4/5] Uruchomienie VM $NAME ==="
START_RESP=$(api -X POST "$EP/api2/json/nodes/$NODE/qemu/$VMID/status/start")
START_UPID=$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('data') or '')" "$START_RESP")
if [ -z "$START_UPID" ]; then
  echo "BŁĄD: Nie udało się uruchomić VM: $START_RESP" >&2
  exit 1
fi
wait_task "$START_UPID" "Start maszyny $NAME"

echo "Maszyna $NAME (VMID: $VMID) uruchomiona."

if [ "$WAIT_SSH" = true ]; then
  echo "=== [5/5] Oczekiwanie na podniesienie SSH ($IP_ADDR:22) ==="
  for i in $(seq 1 "$WAIT_RETRIES"); do
    if timeout 2 bash -c "echo > /dev/tcp/$IP_ADDR/22" 2>/dev/null; then
      echo "Sukces: $NAME ($VMID, $IP_ADDR) odpowiada na porcie SSH 22 (próba $i/$WAIT_RETRIES)."
      exit 0
    fi
    [ "$i" -lt "$WAIT_RETRIES" ] && [ "$WAIT_SLEEP" -gt 0 ] && sleep "$WAIT_SLEEP"
  done
  echo "BŁĄD: Maszyna wystartowała, ale port 22 na $IP_ADDR nie odpowiedział po $WAIT_RETRIES próbach." >&2
  echo "      Sprawdź konsolę PVE lub cloud-init w razie problemów (lub użyj --no-wait-ssh)." >&2
  exit 1
else
  echo "Pominięto oczekiwanie na SSH (--no-wait-ssh)."
  exit 0
fi
