#!/usr/bin/env bash
# Generator certyfikatow TLS dla klastra laboratoryjnego.
#
# POWSTAL, BO GO BRAKOWALO. `playbooks/tls_certs.yml` wylacznie DYSTRYBUUJE to,
# na co wskazuje `tls.certificate_reference` w cluster.yml. Katalogi `fc9/` i
# `lab2/` byly gotowymi artefaktami bez zapisanego sposobu wytworzenia, wiec
# nowy klaster z TLS nie dal sie postawic od zera bez recznego openssl-a.
#
# SAN-y MUSZA pokrywac zarowno nazwy hostow, jak i ich adresy IP: Galera laczy
# sie miedzy wezlami po adresie (`wsrep_cluster_address`), a klienci i sondy
# uzywaja nazw. Cert bez jednego z tych zbiorow daje bledy weryfikacji, ktore
# wygladaja jak awaria replikacji.
#
# Uzycie:
#   pki/generate.sh <nazwa> <SAN>[,<SAN>...]
# Przyklad:
#   pki/generate.sh nc9-galera ncg1,ncg2,ncg3,192.168.1.160,192.168.1.161,192.168.1.162
#
# Wynik: pki/<nazwa-bez-sufiksu>/{ca.pem,server-cert.pem,server-key.pem}
# Katalog `pki/` jest gitignorowany — artefakty zostaja lokalnie.
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "Uzycie: $0 <cn> <san1,san2,...>" >&2
  echo "  <cn>  np. nc9-galera        (CA dostanie CN '<cn> CA')" >&2
  echo "  <san> nazwy hostow i adresy IP, po przecinku" >&2
  exit 2
fi

CN="$1"
SAN_INPUT="$2"
OUT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${CN%%-*}"

# LEAF nazywa PLIKI liscia w katalogu CA. Domyslnie `server` — czyli dotychczasowe
# `server-cert.pem`/`server-key.pem`. Osobna nazwa jest potrzebna, gdy pod jednym
# CA warstwy zyje WIECEJ niz jedna usluga: `pki/generate.sh xenonv12-pmm ...`
# trafialoby do tego samego katalogu (prefiks przed pierwszym `-`) i NADPISALOBY
# certyfikat endpointu ProxySQL. Wtedy: LEAF=pmm REUSE_CA=1 pki/generate.sh ...
LEAF="${LEAF:-server}"
if [[ ! "$LEAF" =~ ^[A-Za-z0-9_-]+$ ]] || [ "$LEAF" = "ca" ]; then
  echo "FAIL: nieprawidlowa lub niebezpieczna nazwa liscia LEAF='$LEAF' (wymagany bezpieczny basename [A-Za-z0-9_-], nie moze nadpisywac CA)" >&2
  exit 1
fi

# Rozdziel SAN-y na DNS: i IP: — openssl wymaga jawnego typu, a wpisanie adresu
# jako DNS: sprawia, ze weryfikacja po IP cicho nie dziala.
san_list=""
IFS=',' read -ra parts <<< "$SAN_INPUT"
for p in "${parts[@]}"; do
  p="$(echo "$p" | tr -d '[:space:]')"
  [ -z "$p" ] && continue
  if [[ "$p" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    san_list="${san_list},IP:${p}"
  else
    san_list="${san_list},DNS:${p}"
  fi
done
san_list="${san_list#,}"

if [ -z "$san_list" ]; then
  echo "FAIL: pusta lista SAN" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
chmod 0700 "$OUT_DIR"
umask 0077

# REUSE_CA=1 wystawia SAM certyfikat serwera pod istniejacym CA — tryb rotacji
# liscia, bezprzestojowy przez `make cluster-tls-rotate`.
#
# UWAGA na wczesniejsze, BLEDNE brzmienie tego komentarza: pisalo, ze wymiany CA
# nie da sie przeprowadzic bez zerwania zaufania. Dokumentacja ma na to osobna
# strone (galera-cluster/galera-security/cluster-ca-rotation.md) i mowi cos
# innego: wymiana CA tez jest bezprzestojowa, przez OKNO PODWOJNEGO ZAUFANIA —
# "trusting both CAs, reissuing node certificates from the new CA one node at a
# time, and finally retiring the old CA. Do not remove the old CA from trust
# until every node has been reissued". Nosnikiem tego okna jest bundle: wg
# socket.ssl_ca "the CA file may contain multiple concatenated certificates".
# Procedure realizuje pki/rotate-ca.sh (trzy fazy) + `make cluster-tls-rotate`.
if [ "${REUSE_CA:-0}" = "1" ]; then
  if [ ! -r "$OUT_DIR/ca.pem" ] || [ ! -r "$OUT_DIR/ca-key.pem" ]; then
    echo "FAIL: REUSE_CA=1, ale brakuje $OUT_DIR/ca.pem albo ca-key.pem" >&2
    exit 1
  fi
  # CA wystawione przed dodaniem subjectKeyIdentifier nie pozwala liscia zwiazac
  # z wystawca po kluczu, a strict-weryfikacja (m.in. ansible.builtin.uri) odrzuca
  # wtedy lancuch: "Missing Authority Key Identifier". Certyfikat CA jest wtedy
  # wystawiany PONOWNIE na TYM SAMYM kluczu i z ta sama nazwa, wiec wczesniej
  # podpisane liscie pozostaja wazne — to odswiezenie certyfikatu, nie rotacja CA.
  if ! openssl x509 -in "$OUT_DIR/ca.pem" -noout -text | grep -q "Subject Key Identifier"; then
    echo "== CA: uzupelniam subjectKeyIdentifier (ten sam klucz i ta sama nazwa)"
    ca_subject_before="$(openssl x509 -in "$OUT_DIR/ca.pem" -noout -subject)"
    # `x509 -signkey` podpisuje PONOWNIE istniejacy certyfikat, wiec subject i
    # issuer biora sie z pliku — zadnego przepisywania DN, ktore potrafi zmienic
    # nazwe wystawcy i uniewaznic wszystkie wczesniej podpisane liscie.
    openssl x509 -in "$OUT_DIR/ca.pem" -signkey "$OUT_DIR/ca-key.pem" -sha256 -days 1095 \
      -out "$OUT_DIR/ca.refreshed.pem" \
      -extfile <(printf 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n') 2>/dev/null
    if [ "$(openssl x509 -in "$OUT_DIR/ca.refreshed.pem" -noout -subject)" != "$ca_subject_before" ]; then
      rm -f "$OUT_DIR/ca.refreshed.pem"
      echo "FAIL: odswiezenie CA zmienilo nazwe wystawcy — przerwane przed nadpisaniem" >&2
      exit 1
    fi
    mv "$OUT_DIR/ca.refreshed.pem" "$OUT_DIR/ca.pem"
  fi
  echo "== CA: uzywam istniejacego $OUT_DIR/ca.pem (rotacja liscia)"
elif [ -e "$OUT_DIR/ca.pem" ] || [ -e "$OUT_DIR/ca-key.pem" ]; then
  if [ "${FORCE_NEW_CA:-0}" = "1" ]; then
    echo "== CA: FORCE_NEW_CA=1 — nadpisuje istniejacy CA w $OUT_DIR"
    openssl req -x509 -newkey rsa:4096 -sha256 -days 1095 -nodes \
      -keyout "$OUT_DIR/ca-key.pem" -out "$OUT_DIR/ca.pem" \
      -subj "/CN=${CN} CA" \
      -addext "basicConstraints=critical,CA:TRUE" \
      -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -addext "subjectKeyIdentifier=hash" 2>/dev/null
  else
    echo "FAIL: $OUT_DIR/ca.pem lub ca-key.pem juz istnieje. Rerun generate.sh nie moze po cichu nadpisac istniejacego CA." >&2
    echo "      Uzyj REUSE_CA=1 (wystawienie nowego liscia '${LEAF}' pod istniejacym CA)" >&2
    echo "      lub FORCE_NEW_CA=1 (celowe wygenerowanie nowego CA i uniewaznienie dotychczasowego zaufania)." >&2
    exit 1
  fi
else
  echo "== CA: CN=${CN} CA"
  openssl req -x509 -newkey rsa:4096 -sha256 -days 1095 -nodes \
    -keyout "$OUT_DIR/ca-key.pem" -out "$OUT_DIR/ca.pem" \
    -subj "/CN=${CN} CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" 2>/dev/null
fi

echo "== Lisc '${LEAF}': CN=${CN}, SAN=${san_list}"
openssl req -newkey rsa:4096 -sha256 -nodes \
  -keyout "$OUT_DIR/${LEAF}-key.pem" -out "$OUT_DIR/${LEAF}.csr" \
  -subj "/CN=${CN}" 2>/dev/null

# extendedKeyUsage z OBIEMA rolami: ten sam cert obsluguje polaczenia
# przychodzace (serwer) i wychodzace do innych wezlow (klient) — tak jak
# istniejacy cert fc9, ktory ma "TLS Web Server Authentication,
# TLS Web Client Authentication".
openssl x509 -req -in "$OUT_DIR/${LEAF}.csr" -sha256 -days 1095 \
  -CA "$OUT_DIR/ca.pem" -CAkey "$OUT_DIR/ca-key.pem" -CAcreateserial \
  -out "$OUT_DIR/${LEAF}-cert.pem" \
  -extfile <(printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth,clientAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n' "$san_list") \
  2>/dev/null

rm -f "$OUT_DIR/${LEAF}.csr" "$OUT_DIR/ca.srl"
chmod 0600 "$OUT_DIR"/*.pem

echo "== Weryfikacja"
openssl verify -CAfile "$OUT_DIR/ca.pem" "$OUT_DIR/${LEAF}-cert.pem"
# `-ext subjectAltName` nie istnieje w LibreSSL (macOS), a host kontrolny moze
# byc jednym albo drugim. `-text | grep` dziala wszedzie.
openssl x509 -in "$OUT_DIR/${LEAF}-cert.pem" -noout -text \
  | grep -A1 "Subject Alternative Name" | sed 's/^/   /'
echo "OK: $OUT_DIR"
