#!/usr/bin/env bash
# Wystawia LISC NA WEZEL pod istniejacym CA klastra.
#
# PO CO. `generate.sh` emituje JEDEN certyfikat serwera z SAN-ami wszystkich
# wezlow i JEDEN klucz prywatny, kopiowany na cala trojke. Model operatorski
# MariaDB robi inaczej: wspolne CA, ale osobny lisc i osobny klucz per wezel.
# Roznica jest praktyczna, nie estetyczna:
#   * wyciek klucza z jednego wezla nie daje tozsamosci pozostalych,
#   * lisc mozna wymienic na JEDNYM wezle, bez dotykania reszty klastra,
#   * cert niesie tozsamosc konkretnego hosta, wiec log TLS mowi KTO sie polaczyl,
#     a nie tylko "ktos z tego klastra".
#
# CA i jego klucz musza juz istniec — ten skrypt CELOWO nie potrafi ich stworzyc.
# Wystawianie nowego CA to zdarzenie wymagajace rotacji zaufania na calej flocie
# (tests/lab/tls/rotate-ca.sh, okno podwojnego zaufania); wymieszanie tego z
# rutynowym wystawianiem liscia konczy sie CA wygenerowanym przez pomylke.
#
# Uzycie:
#   tests/lab/tls/issue-node-certs.sh <katalog> <host=ip>[,<host=ip>...] [dni]
# Przyklad:
#   tests/lab/tls/issue-node-certs.sh n11 n11g1=192.168.1.185,n11g2=192.168.1.186 90
#
# Wynik: <katalog>/node-<host>-cert.pem oraz node-<host>-key.pem
set -euo pipefail

if [ $# -lt 2 ] || [ $# -gt 3 ]; then
  echo "Uzycie: $0 <katalog-tls> <host=ip[,host=ip...]> [dni]" >&2
  echo "  <katalog-tls>  katalog z ca.pem i ca-key.pem (np. n11)" >&2
  echo "  <host=ip>      nazwa wezla i jego adres, po przecinku" >&2
  echo "  [dni]          waznosc liscia; domyslnie 90" >&2
  exit 2
fi

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="$BASE/${1#tests/lab/tls/}"
NODES="$2"
# 90 dni to nie ostroznosc, tylko wymuszenie cwiczenia rotacji. Cert wazny 3 lata
# jest rotowany raz — w panice, przez osobe, ktora nigdy tego nie robila.
# Rotacja jest bezprzestojowa (FLUSH SSL), a `probe-hardening.py` pada na 30 dni
# przed koncem, wiec zostaje 60 dni marginesu i alert ISC-47 w PMM.
DAYS="${3:-90}"

for f in ca.pem ca-key.pem; do
  [ -r "$DIR/$f" ] || { echo "FAIL: brak $DIR/$f — najpierw generate.sh" >&2; exit 1; }
done

umask 0077
IFS=',' read -ra PAIRS <<< "$NODES"
[ "${#PAIRS[@]}" -gt 0 ] || { echo "FAIL: pusta lista wezlow" >&2; exit 1; }

for pair in "${PAIRS[@]}"; do
  host="${pair%%=*}"
  addr="${pair#*=}"
  if [ -z "$host" ] || [ -z "$addr" ] || [ "$host" = "$addr" ]; then
    echo "FAIL: '$pair' nie jest w formacie host=ip" >&2
    exit 1
  fi

  # SAN zawiera nazwe I adres tego JEDNEGO wezla. Galera laczy sie miedzy wezlami
  # po adresie (wsrep_cluster_address), a klienci i sondy po nazwie — brak
  # ktoregokolwiek daje bledy weryfikacji wygladajace jak awaria replikacji.
  openssl req -newkey rsa:4096 -sha256 -nodes \
    -keyout "$DIR/node-${host}-key.pem" -out "$DIR/node-${host}.csr" \
    -subj "/CN=${host}" 2>/dev/null

  openssl x509 -req -in "$DIR/node-${host}.csr" -sha256 -days "$DAYS" \
    -CA "$DIR/ca.pem" -CAkey "$DIR/ca-key.pem" -CAcreateserial \
    -out "$DIR/node-${host}-cert.pem" \
    -extfile <(printf 'subjectAltName=DNS:%s,IP:%s\nextendedKeyUsage=serverAuth,clientAuth\nbasicConstraints=CA:FALSE\n' "$host" "$addr") \
    2>/dev/null

  rm -f "$DIR/node-${host}.csr"
  chmod 0600 "$DIR/node-${host}-key.pem" "$DIR/node-${host}-cert.pem"

  openssl verify -CAfile "$DIR/ca.pem" "$DIR/node-${host}-cert.pem" >/dev/null \
    || { echo "FAIL: $host — lisc nie weryfikuje sie pod CA klastra" >&2; exit 1; }
  echo "== ${host}: CN=${host} SAN=DNS:${host},IP:${addr} waznosc ${DAYS}d — OK"
done

rm -f "$DIR/ca.srl"
echo "OK: liscie per wezel w $DIR"
