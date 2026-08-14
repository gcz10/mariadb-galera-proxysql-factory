#!/usr/bin/env bash
# Sonda: firewalld działa i dopuszcza wyłącznie zadeklarowany ruch (ISC-5).
# Uruchomienie na docelowym hoście: ./tests/validation/probe-firewalld.sh "22/tcp 3306/tcp ..."
# Argumenty: $1 = lista dozwolonych portów (OBOWIĄZKOWA)
#
# Poprzednia wersja czytała wyłącznie linie `ports:` z `--list-all-zones`.
# Polityka tego projektu (roles/firewall/templates/public.xml.j2) wyraża CAŁĄ
# allowlistę jako rich-rule powiązane ze źródłowym CIDR, więc `ports:` są puste,
# zbiór otwartych portów wychodził pusty i sonda przechodziła PRÓŻNO — meldowała
# PASS nie zmierzywszy niczego. Ta wersja czyta rich-rule, porty proste i usługi,
# oraz odrzuca strefy z przypisaniem `sources:`, bo mają pierwszeństwo przed
# public i mogą ominąć allowlistę.
set -euo pipefail

# Allowlista jest OBOWIAZKOWA. Wariant "brak argumentu -> PASS informacyjny"
# istnial wczesniej i byl bezuzyteczny: sonde dalo sie wywolac bez niczego
# i dostac zielone, nie zmierzywszy polityki. Kto nie potrafi wypisac
# dozwolonych portow, ten nie wie, co host wystawia.
ALLOWED_PORTS="${1:-}"
if [ -z "$ALLOWED_PORTS" ]; then
  echo "FAIL: ISC-5 — brak listy dozwolonych portow (argument 1)."
  echo "      Uzycie: probe-firewalld.sh \"22/tcp 3306/tcp 4567/udp ...\""
  exit 2
fi

STATE=$(firewall-cmd --state 2>/dev/null || echo "not-running")
if [ "$STATE" != "running" ]; then
  echo "FAIL: ISC-5 — firewalld is '$STATE', expected running"
  exit 1
fi

# Wyłącznie strefy AKTYWNE — z przypiętym interfejsem albo źródłem. Strefy
# nieaktywne (home, internal, trusted...) niosą domyślne usługi dystrybucji
# (cockpit, dhcp, dns, mdns), ale nie wpuszczają ruchu, bo nic do nich nie
# należy. Skanowanie `--list-all-zones` zapalałoby się na nich zawsze.
# Gdy public jest strefą domyślną, firewalld pisze `public (default)`. Bierzemy
# wyłącznie pierwsze pole, inaczej `--zone='(default)'` zwraca INVALID_ZONE (112)
# i `set -e` ubija sondę bez jednego słowa na stdout.
ACTIVE=$(firewall-cmd --get-active-zones 2>/dev/null | awk 'NF && $0 !~ /^[[:space:]]/ {print $1}' || true)
if [ -z "$ACTIVE" ]; then
  echo "FAIL: ISC-5 — brak aktywnej strefy firewalld; żaden interfejs nie jest objęty polityką"
  exit 1
fi

ZONES=""
for z in $ACTIVE; do
  ZONES="${ZONES}$(firewall-cmd --zone="$z" --list-all 2>/dev/null)
"
done

# Strefa związana ze źródłem wyprzedza strefę interfejsu — obejście allowlisty.
if printf '%s\n' "$ZONES" | grep -qE '^[[:space:]]*sources:[[:space:]]+\S'; then
  echo "FAIL: ISC-5 — aktywna strefa z przypisaniem source wyprzedza public i omija allowlistę:"
  printf '%s\n' "$ZONES" | grep -E '^[[:space:]]*sources:[[:space:]]+\S' | sed 's/^/    /'
  exit 1
fi

# Porty proste ORAZ porty z rich-rule — oba wpuszczają ruch.
PLAIN_PORTS=$(printf '%s\n' "$ZONES" | sed -n 's/^[[:space:]]*ports:[[:space:]]*//p' \
  | tr ' ' '\n' | grep -E '^[0-9]+/(tcp|udp)$' || true)
RICH_PORTS=$(printf '%s\n' "$ZONES" | grep -oE 'port port="[0-9]+" protocol="(tcp|udp)"' \
  | sed -E 's/port port="([0-9]+)" protocol="(tcp|udp)"/\1\/\2/' || true)
OPEN_PORTS=$(printf '%s\n%s\n' "$PLAIN_PORTS" "$RICH_PORTS" | grep -E '^[0-9]+/' | sort -u || true)

SERVICES=$(printf '%s\n' "$ZONES" | sed -n 's/^[[:space:]]*services:[[:space:]]*//p' \
  | tr ' ' '\n' | grep -vE '^$' | sort -u || true)

# Allowlista podana, a nie widać ani jednego portu => sonda niczego nie zmierzyła.
if [ -z "$OPEN_PORTS" ]; then
  echo "FAIL: ISC-5 — nie wykryto żadnego portu mimo podanej allowlisty;"
  echo "      polityka jest niewidoczna dla sondy zamiast być pusta (próżny PASS)."
  exit 1
fi

FAIL=0
for port in $OPEN_PORTS; do
  if ! printf '%s\n' "$ALLOWED_PORTS" | tr ' ' '\n' | grep -qxF "$port"; then
    echo "FAIL: ISC-5 — port poza allowlistą: $port"
    FAIL=1
  fi
done

for svc in $SERVICES; do
  case "$svc" in
    dhcpv6-client) ;;
    *)
      echo "FAIL: ISC-5 — usługa firewalld poza allowlistą: $svc"
      FAIL=1
      ;;
  esac
done

if [ "$FAIL" -eq 0 ]; then
  echo "PASS: ISC-5 — firewalld running; $(printf '%s\n' "$OPEN_PORTS" | wc -l | tr -d ' ') portów, wszystkie zadeklarowane; brak stref źródłowych"
  printf '%s\n' "$OPEN_PORTS" | sed 's/^/    /'
  exit 0
fi
exit 1
