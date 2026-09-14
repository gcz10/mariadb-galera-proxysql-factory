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
# Wymaga w srodowisku: PROXMOX_VE_ENDPOINT oraz PROXMOX_VE_API_TOKEN ALBO pary
# PROXMOX_VE_USERNAME + PROXMOX_VE_PASSWORD (to samo, co provider terraform
# i bramka `pve_auth_guard` w Makefile).
set -euo pipefail

TF_DIR="${1:?Uzycie: pve-teardown.sh <katalog-terraform>}"
[ -d "$TF_DIR" ] || { echo "Brak katalogu: $TF_DIR" >&2; exit 1; }
: "${PROXMOX_VE_ENDPOINT:?Ustaw PROXMOX_VE_ENDPOINT}"
# Token ALBO uzytkownik+haslo — ten sam kontrakt co `pve_auth_guard`. Provider
# bpg/proxmox uwierzytelnia sie tokenem, wiec twardy wymog hasla czynil
# skodyfikowany teardown niewykonalnym dla operatora uzywajacego tokena, a bez
# sprzatania sierot ZFS kolejny apply na tych samych VMID pada.
if [ -z "${PROXMOX_VE_API_TOKEN:-}" ]; then
  : "${PROXMOX_VE_USERNAME:?Ustaw PROXMOX_VE_API_TOKEN albo PROXMOX_VE_USERNAME i PROXMOX_VE_PASSWORD}"
  : "${PROXMOX_VE_PASSWORD:?Ustaw PROXMOX_VE_API_TOKEN albo PROXMOX_VE_USERNAME i PROXMOX_VE_PASSWORD}"
fi

# Endpoint bywa podany z ukosnikiem na koncu (tak trzyma go .env i tak przyjmuje
# provider). Bez normalizacji sklejamy `//api2/json/...`, na co PVE odpowiada
# HTTP 500 "no such file". Provider tego nie zauwaza, ten skrypt cicho pomijal
# przez to CALE sprzatanie sierot.
PVE_API=$(printf '%s' "$PROXMOX_VE_ENDPOINT" | sed 's#/*$##')

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

# Opcjonalne argumenty po katalogu = nazwy wezlow do zniszczenia (np. grg1 grr1).
# Bez nich kasujemy CALY klaster.
shift || true
NODES=("$@")

# VMID-y bierzemy PRZED destroy — po nim stan terraform jest juz pusty.
# (while-read zamiast mapfile — macOS ma bash 3.2)
NODE_FILTER=""
if [ "${#NODES[@]}" -gt 0 ]; then
  NODE_FILTER=$(printf '%s,' "${NODES[@]}")
fi
VMID_CACHE="$TF_DIR/.teardown-vmids"
# Plik sprzed 2026-09-12: sama lista VMID bez nazw, trzymana obok. Nie jest juz
# zapisywany (ochrona dyskow jedzie w jednym wpisie), a zostawiony przez starsza
# wersje zostaje usuniety razem z zapisem nowego cache.
LEGACY_PROTECTED_CACHE="$TF_DIR/.teardown-protected-vmids"
VMIDS=()
PROTECTED_DISK_VMIDS=()
CACHE_ENTRIES=()
# Wpisy pliku wznowienia spoza zakresu BIEZACEGO wywolania (patrz nizej).
KEPT_ENTRIES=()
# Wpisy wznowienia PRZYJETE w tym wywolaniu — osobno od calej listy VMID, bo
# operator czyta ten komunikat, zeby wiedziec, skad przyszly cele (output vs plik).
RESTORED_ENTRIES=()
while IFS=':' read -r name vmid role del_disks; do
  [ -n "$vmid" ] || continue
  VMIDS+=("$vmid")
  protected=no
  if [ "$role" = "infra" ] || [ "$del_disks" = "false" ]; then
    PROTECTED_DISK_VMIDS+=("$vmid")
    protected=yes
  fi
  # Wpis zna NAZWE wezla, nie tylko VMID: wznowienie musi umiec odrzucic
  # maszyny, ktorych operator w danym wywolaniu nie wskazal.
  CACHE_ENTRIES+=("$name:$vmid:$protected")
done < <(
  cd "$TF_DIR" && terraform output -json vms 2>/dev/null |
    NODE_FILTER="$NODE_FILTER" python3 -c '
import sys, json, os
want = [n for n in os.environ.get("NODE_FILTER", "").split(",") if n]
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
for name, v in data.items():
    if not want or name in want:
        vmid = str(v.get("vmid", ""))
        role = str(v.get("role", ""))
        del_disks = str(v.get("delete_unreferenced_disks_on_destroy", "")).lower()
        print(name + ":" + vmid + ":" + role + ":" + del_disks)
' 2>/dev/null
)
# Plik wznowienia zna cele POTWIERDZONEGO przebiegu. Scala sie z biezaca lista
# przy KAZDYM wywolaniu — nie tylko wtedy, gdy `terraform output` jest pusty.
#
# `terraform destroy` kasuje maszyny PO KOLEI, wiec po przerwaniu w polowie
# output zwraca juz tylko OCALE WEZLY. Wersja, ktora adoptowala cache wylacznie
# przy pustym output, nadpisywala go wtedy ocalalymi i gubila sieroty ofiar
# wczesniejszego przebiegu (zmierzone 2026-09-14 na atrapach API: retry konczyl
# sie "usunietych sierot: 0" i kodem 0, a wolumen pierwszej ofiary zostawal —
# dokladnie ta sierota, ktora wywala nastepny `apply`).
#
# ZAKRES JEST WIAZACY. Wznowienie adoptowalo wczesniej CALA zapisana liste,
# takze przy wywolaniu per-node: teardown jednej maszyny wysylal wtedy DELETE
# na wolumeny sasiada, ktory nadal zyl i nadal byl w stanie terraform
# (zmierzone 2026-09-12 na atrapach API). Wpis spoza wskazanego zakresu albo
# w starym formacie (bez nazwy wezla) jest POMIJANY — sierota zostaje do
# recznego sprzatniecia, ale nic cudzego nie ginie.
if [ -f "$VMID_CACHE" ]; then
  restored=0
  skipped_foreign=0
  skipped_legacy=0
  # Wpisy spoza zakresu NIE moga zniknac. Plik wznowienia jest jedynym miejscem,
  # ktore pamieta ofiary przerwanego teardownu CALEGO roota — `terraform output`
  # ich juz nie zwroci. Gdyby przebieg per-node nadpisal plik wlasnym zakresem,
  # wolumeny pozostalych ofiar zostalyby bez ostrzezenia (dokladnie ta klasa
  # sieroty, ktora ta poprawka zamyka), a przy kasowaniu pliku po sukcesie —
  # zapomniane na stale. Dlatego wracaja do pliku i tylko CZEKAJA na przebieg,
  # ktory obejmie ich wezly.
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    # Stary format to sam VMID w linii — nie wiadomo, do ktorej maszyny
    # nalezy, wiec nie ma jak sprawdzic, czy miesci sie w zakresie.
    case "$entry" in
      *:*:*) ;;
      *) skipped_legacy=$((skipped_legacy + 1)); continue ;;
    esac
    name="${entry%%:*}"
    entry_rest="${entry#*:}"
    vmid="${entry_rest%%:*}"
    protected="${entry_rest#*:}"
    if [ -z "$name" ] || [ -z "$vmid" ]; then
      skipped_legacy=$((skipped_legacy + 1))
      continue
    fi
    if [ -n "$NODE_FILTER" ]; then
      case ",$NODE_FILTER" in
        *",$name,"*) ;;
        *)
          skipped_foreign=$((skipped_foreign + 1))
          KEPT_ENTRIES+=("$entry")
          continue
          ;;
      esac
    fi
    # Ten sam VMID moze stac i w output, i w cache — nie listujemy go dwa razy.
    # Petla jest pod bramka dlugosci, bo bash 3.2 (macOS) z `set -u` wywraca sie
    # na rozwinieciu PUSTEJ tablicy: `"${VMIDS[@]}"` to tam "unbound variable".
    if [ "${#VMIDS[@]}" -gt 0 ]; then
      already=no
      for seen in "${VMIDS[@]}"; do
        if [ "$seen" = "$vmid" ]; then
          already=yes
          break
        fi
      done
      [ "$already" = "yes" ] && continue
    fi
    VMIDS+=("$vmid")
    CACHE_ENTRIES+=("$name:$vmid:$protected")
    [ "$protected" = "yes" ] && PROTECTED_DISK_VMIDS+=("$vmid")
    RESTORED_ENTRIES+=("$name:$vmid:$protected")
    restored=$((restored + 1))
  done < "$VMID_CACHE"
  # Tylko faktycznie przyjete wpisy: VMIDS moze zawierac tez cele z output,
  # ktore z pliku wznowienia nie pochodza.
  [ "$restored" -gt 0 ] && echo "PRZYWRÓCONO niedokonczone cele poprzedniego przebiegu: ${RESTORED_ENTRIES[*]}" >&2
  [ "$skipped_foreign" -gt 0 ] && echo "POMINIETO $skipped_foreign wpisow spoza wskazanego zakresu (${NODES[*]}) — nie naleza do tego wywolania." >&2
  [ "$skipped_legacy" -gt 0 ] && echo "POMINIETO $skipped_legacy wpisow w starym formacie (bez nazwy wezla) — sprzataj te sieroty recznie." >&2
fi
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

# Plik wznowienia powstaje DOPIERO po potwierdzeniu celu. Zapis przed bramka
# zostawial swieza liste VMID po KAZDEJ odmowie (literowka w celu, przebieg na
# probe), a kolejne wywolanie adoptowalo ja jako "poprzedni przebieg".
# Plik jest PRZEPISYWANY od zera, gdy mamy cokolwiek do zapisania: dopisanie
# wpisow spoza zakresu (`>>`) do nieoskroconego pliku zdublowaloby je, bo sa w
# nim juz te same wpisy, ktore przed chwila czytalismy.
# Bramki dlugosci, bo bash 3.2 z `set -u` wywraca sie na pustej tablicy.
if [ "${#CACHE_ENTRIES[@]}" -gt 0 ] || [ "${#KEPT_ENTRIES[@]}" -gt 0 ]; then
  : > "$VMID_CACHE"
  if [ "${#CACHE_ENTRIES[@]}" -gt 0 ]; then
    printf '%s\n' "${CACHE_ENTRIES[@]}" >> "$VMID_CACHE"
  fi
  if [ "${#KEPT_ENTRIES[@]}" -gt 0 ]; then
    printf '%s\n' "${KEPT_ENTRIES[@]}" >> "$VMID_CACHE"
  fi
fi
rm -f "$LEGACY_PROTECTED_CACHE"
# Terraform odrzuca -target pojedynczej VM, gdy blok moved jest jeszcze tylko
# w konfiguracji, a state nadal ma adres rootowy. Nie wolno wtedy poszerzac
# targetu do calego zasobu (zniszczyloby to pozostale wezly); bramka ponizej
# konczy sie jawnie i wymaga najpierw zapisania migracji adresow.
if [ "${#NODES[@]}" -gt 0 ] && [ -f "$TF_DIR/terraform.tfstate" ]; then
  if python3 - "$TF_DIR/terraform.tfstate" "$TF_DIR" <<'PY'
import json
import re
import sys
from pathlib import Path

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        state = json.load(handle)
    root = Path(sys.argv[2])
    has_move = any(
        re.search(
            r"\bto\s*=\s*module\.vms\.proxmox_virtual_environment_vm\.node\b",
            path.read_text(encoding="utf-8"),
        )
        for path in root.glob("*.tf")
    )
    pending = any(
        item.get("type") == "proxmox_virtual_environment_vm"
        and not item.get("module")
        for item in state.get("resources", [])
    )
except (OSError, ValueError, TypeError) as exc:
    print(f"nie mozna odczytac stanu migracji: {exc}", file=sys.stderr)
    raise SystemExit(2)

raise SystemExit(0 if pending and has_move else 1)
PY
  then
    cat >&2 <<EOF
ODMOWA: state nie ma jeszcze zapisanych adresow module.vms (moved).
Najpierw wykonaj plan migracji i zastosuj go tylko, gdy pokazuje 0/0/0,
potem ponow teardown per-node.
EOF
    exit 1
  else
    CHECK_RC=$?
    if [ "$CHECK_RC" -ge 2 ]; then
      echo "ODMOWA: nie mozna bezpiecznie sprawdzic stanu migracji adresow." >&2
      exit 1
    fi
  fi
fi


if [ "${#NODES[@]}" -gt 0 ]; then
  echo "=== terraform destroy — TYLKO: ${NODES[*]} ==="
  TARGETS=()
  for n in "${NODES[@]}"; do
    TARGETS+=(-target="module.vms.proxmox_virtual_environment_vm.node[\"$n\"]")
  done
  ( cd "$TF_DIR" && terraform destroy -auto-approve "${TARGETS[@]}" )
else
  echo "=== terraform destroy — CALY klaster ($TF_DIR) ==="
  ( cd "$TF_DIR" && terraform destroy -auto-approve )
fi

[ "${#VMIDS[@]}" -eq 0 ] && exit 0

echo "=== sprzatanie sierot ZFS dla VMID: ${VMIDS[*]} ==="
# Naglowki uwierzytelniajace. Wedlug dokumentacji Proxmox VE API ("API Tokens")
# token nie wymaga CSRF przy DELETE, a naglowek nalezy podawac PLIKIEM: token
# jest dlugowieczny, a argv widzi kazdy uzytkownik przez `ps`.
# Identycznie bilet sesyjny i CSRF z galezi username+haslo przekazujemy przez
# plik naglowkow 0600 (-H @...), aby nie wyciekly do ps/argv (bilet zyje 2 h).
AUTH_ARGS=()
AUTH_HEADER_FILE=""
AUTH_PASS_FILE=""
CONTENT_FILE=""
trap 'rm -f ${CONTENT_FILE:+"$CONTENT_FILE"} ${AUTH_HEADER_FILE:+"$AUTH_HEADER_FILE"} ${AUTH_PASS_FILE:+"$AUTH_PASS_FILE"}' EXIT

if [ -n "${PROXMOX_VE_API_TOKEN:-}" ]; then
  AUTH_HEADER_FILE=$(mktemp)
  chmod 600 "$AUTH_HEADER_FILE"
  printf 'Authorization: PVEAPIToken=%s\n' "$PROXMOX_VE_API_TOKEN" > "$AUTH_HEADER_FILE"
  AUTH_ARGS=(-H @"$AUTH_HEADER_FILE")
else
  # Bezpieczne przekazanie hasla przez plik 0600 (zapobiega wyciekowi do argv / ps)
  AUTH_PASS_FILE=$(mktemp)
  chmod 600 "$AUTH_PASS_FILE"
  printf '%s' "$PROXMOX_VE_PASSWORD" > "$AUTH_PASS_FILE"
  AUTH=$(curl -sk --max-time 20 -X POST "${PVE_API}/api2/json/access/ticket" \
    --data-urlencode "username=${PROXMOX_VE_USERNAME}" \
    --data-urlencode "password@${AUTH_PASS_FILE}")
  rm -f "$AUTH_PASS_FILE"
  AUTH_PASS_FILE=""
  # Bez sprawdzenia bilet bywa pusty (zle haslo, API nieosiagalne), a skrypt
  # leci dalej z pustym cookie i sypie kaskada nieczytelnych 401.
  if ! TICKET=$(printf '%s' "$AUTH" | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["ticket"])' 2>/dev/null) \
     || [ -z "$TICKET" ]; then
    echo "BLAD: uwierzytelnianie w PVE API nie powiodlo sie (sprawdz PROXMOX_VE_*)." >&2
    echo "Maszyny zostaly usuniete, ale sieroty ZFS dla VMID ${VMIDS[*]} trzeba sprzatnac recznie." >&2
    exit 1
  fi
  CSRF=$(printf '%s' "$AUTH" | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["CSRFPreventionToken"])')
  # Bezpieczne przekazanie biletu i CSRF przez plik naglowkow 0600 bez wycieku do argv / ps.
  # Normalizujemy prefiks PVEAuthCookie= (API zwraca go z prefiksem lub bez).
  COOKIE_VAL="$TICKET"
  case "$COOKIE_VAL" in
    PVEAuthCookie=*) ;;
    *) COOKIE_VAL="PVEAuthCookie=$COOKIE_VAL" ;;
  esac
  AUTH_HEADER_FILE=$(mktemp)
  chmod 600 "$AUTH_HEADER_FILE"
  printf 'Cookie: %s\nCSRFPreventionToken: %s\n' "$COOKIE_VAL" "$CSRF" > "$AUTH_HEADER_FILE"
  AUTH_ARGS=(-H @"$AUTH_HEADER_FILE")
fi

CONTENT_FILE=$(mktemp)
# Lista wolumenow MUSI byc sprawdzona. Wczesniej kazdy VMID parsowal odpowiedz
# osobno z `2>/dev/null`, wiec HTML zamiast JSON-a konczyl sie komunikatem
# "usunietych sierot: 0" i kodem 0 — sprzatanie nie odbywalo sie wcale, a
# dowiadywalismy sie o tym dopiero przy nastepnym `apply` na tym samym VMID.
CONTENT_CODE=$(curl -sk --max-time 30 "${AUTH_ARGS[@]}" \
  -o "$CONTENT_FILE" -w '%{http_code}' \
  "${PVE_API}/api2/json/nodes/${PVE_NODE}/storage/${PVE_STORAGE}/content")
if [ "$CONTENT_CODE" != "200" ]; then
  echo "BLAD: PVE API zwrocilo HTTP $CONTENT_CODE przy liscie wolumenow." >&2
  echo "Maszyny zostaly usuniete, ale sieroty ZFS dla VMID ${VMIDS[*]} trzeba sprzatnac recznie." >&2
  exit 1
fi

# --- Sierota czy ZYWA maszyna? ---
# Sprzatanie kasuje WOLUMENY po numerze VMID, a VMID jest zasobem wielokrotnego
# uzytku: wolumen `vm-<vmid>-*` moze nalezec do maszyny utworzonej PO przerwanym
# przebiegu (reczny create, apply innego roota, odbudowa po awarii). Kasowanie
# dyskow zywej maszyny to utrata danych, ktorej nie cofa zaden kolejny `apply`.
# Dlatego przed DELETE pytamy API o stan maszyny. Sciezka to LISCIOWY
# `/qemu/<vmid>/status/current` (API-viewer PVE: `vm_status`, wymaga VM.Audit),
# a nie katalogowy `/qemu/<vmid>/status` — ten zwraca podkatalogi (start, stop,
# shutdown...), wiec 200 z niego nie mowiloby nic o istnieniu maszyny:
#   200          => maszyna istnieje (stan `running`/`stopped`), wolumeny NIE sa
#                   sierota — pomijamy i mowimy to glosno,
#   500/501/404  => PVE nie zna takiej maszyny (brak configu) => wolno sprzatac,
#   cokolwiek innego (takze 000 z timeoutu) => NIE WIEMY. Nierozstrzygniete
#   kasowanie jest nieodwracalne, wiec konczymy bledem zamiast zgadywac.
SWEEP_VMIDS=()
LIVE_VMIDS=()
UNKNOWN_VMIDS=()
for vmid in "${VMIDS[@]}"; do
  [ -n "$vmid" ] || continue
  code=$(curl -sk --max-time 30 -o /dev/null -w '%{http_code}' \
    "${AUTH_ARGS[@]}" \
    "${PVE_API}/api2/json/nodes/${PVE_NODE}/qemu/${vmid}/status/current" || echo 000)
  case "$code" in
    200) LIVE_VMIDS+=("$vmid") ;;
    500|501|404) SWEEP_VMIDS+=("$vmid") ;;
    *) UNKNOWN_VMIDS+=("$vmid") ;;
  esac
done
if [ "${#LIVE_VMIDS[@]}" -gt 0 ]; then
  echo "ZACHOWANO: VMID ${LIVE_VMIDS[*]} nadal ma maszyne w PVE — wolumeny nie sa sierotami." >&2
fi
if [ "${#UNKNOWN_VMIDS[@]}" -gt 0 ]; then
  echo "BLAD: nie udalo sie ustalic, czy VMID ${UNKNOWN_VMIDS[*]} jeszcze istnieje (odpowiedz inna niz 200/500/501/404)." >&2
  echo "Odmawiam kasowania wolumenow, ktore moga byc dyskami ZYWYCH maszyn. Powtorz, gdy PVE API odpowiada." >&2
  exit 1
fi
# Ta sama bramka dlugosci co wyzej: przypisanie z pustej tablicy wywraca bash 3.2.
if [ "${#SWEEP_VMIDS[@]}" -gt 0 ]; then
  VMIDS=("${SWEEP_VMIDS[@]}")
else
  VMIDS=()
fi

# Po filtrze zywotnosci lista moze byc PUSTA (wszystkie VMID maja jeszcze swoje
# maszyny) — `:-` chroni rozwiniecie tablicy w tym przypadku.
if ! VOLS_RAW=$(VMIDS_CSV="$(printf '%s,' "${VMIDS[@]:-}")" \
                PROTECTED_CSV="$(printf '%s,' "${PROTECTED_DISK_VMIDS[@]:-}")" \
                python3 -c '
import json, os, re, sys
want = [v for v in os.environ["VMIDS_CSV"].split(",") if v]
protected = [v for v in os.environ.get("PROTECTED_CSV", "").split(",") if v]
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle).get("data") or []
for item in data:
    volid = item.get("volid", "")
    disk = volid.split(":")[-1]
    match = re.match(r"vm-(\d+)-", disk)
    # Pelny segment zamiast substringa: "vm-992-" nie moze dopasowac
    # wolumenow sasiada "vm-9920-..." (3-cyfrowe VMID-y wroca).
    if match and match.group(1) in want:
        vmid = match.group(1)
        # Ochrona dyskow danych dla maszyn z rola infra lub delete_unreferenced_disks_on_destroy=false
        if vmid in protected and "-disk-" in disk:
            print(f"  zachowano chroniony dysk danych: {volid}", file=sys.stderr)
            continue
        print(volid)
' "$CONTENT_FILE"); then
  echo "BLAD: odpowiedz PVE API nie jest poprawnym JSON-em — nie wiadomo, czy zostaly sieroty." >&2
  echo "Maszyny zostaly usuniete, ale sieroty ZFS dla VMID ${VMIDS[*]} trzeba sprzatnac recznie." >&2
  exit 1
fi

VOLS=()
while IFS= read -r line; do
  [ -n "$line" ] && VOLS+=("$line")
done <<< "$VOLS_RAW"

removed=0
failed=0
# "${VOLS[@]}" z pusta tablica + set -u wywala sie na bash 3.2 (macOS)
if [ "${#VOLS[@]}" -gt 0 ]; then
  for vol in "${VOLS[@]}"; do
    # `|| echo 000`: timeout curla (rc 28) pod set -e ubijalby skrypt w polowie
    # petli — 000 wpada do licznika failed i raport zostaje kompletny.
    code=$(curl -sk --max-time 60 -o /dev/null -w '%{http_code}' -X DELETE \
      "${AUTH_ARGS[@]}" \
      "${PVE_API}/api2/json/nodes/${PVE_NODE}/storage/${PVE_STORAGE}/content/${vol}" || echo 000)
    if [ "$code" = "200" ]; then
      echo "  usunieto sierote: $vol"
      removed=$((removed + 1))
    else
      echo "  BLAD: nie udalo sie usunac $vol (HTTP $code)" >&2
      failed=$((failed + 1))
    fi
  done
fi

# Pozostawiona sierota wywali nastepny `terraform apply` na tym VMID, wiec
# konczymy bledem zamiast raportowac sukces czesciowy.
if [ "$failed" -gt 0 ]; then
  echo "BLAD: nie usunieto $failed wolumenow — sprzataj recznie przed kolejnym apply." >&2
  exit 1
fi
# Plik wznowienia kasujemy tylko wtedy, gdy przebieg obejmowal CALY root.
# Wywolanie per-node nie jest wlascicielem calej listy: wpisy innych wezlow
# (KEPT_ENTRIES) zostaja, zeby nie zniknely ofiary przerwanego teardownu roota.
# Gdy takich wpisow nie ma, plik nie ma czego wiecej niesc.
if [ -z "$NODE_FILTER" ] || [ "${#KEPT_ENTRIES[@]}" -eq 0 ]; then
  rm -f "$VMID_CACHE"
else
  printf '%s\n' "${KEPT_ENTRIES[@]}" > "$VMID_CACHE"
  echo "UWAGA: w $VMID_CACHE zostaly niedokonczone cele innych wezlow: ${KEPT_ENTRIES[*]}" >&2
fi
echo "=== teardown zakonczony (usunietych sierot: $removed) ==="
