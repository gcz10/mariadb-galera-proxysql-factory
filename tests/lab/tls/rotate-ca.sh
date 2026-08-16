#!/usr/bin/env bash
# Rotacja urzedu certyfikacji (CA) klastra Galera BEZ przestoju.
#
# Realizuje procedure z galera-cluster/galera-security/cluster-ca-rotation.md:
#   "Migrating to a new Certificate Authority without downtime relies on a
#    dual-trust window, during which nodes trust both the old and the new CA.
#    This involves trusting both CAs, reissuing node certificates from the new CA
#    one node at a time, and finally retiring the old CA. Do not remove the old CA
#    from trust until every node has been reissued from the new CA, as doing so
#    early will reject nodes still presenting old-CA certificates."
#
# Nosnikiem okna podwojnego zaufania jest bundle: wg dokumentacji socket.ssl_ca
# "the CA file may contain multiple concatenated certificates, forming a CA bundle".
#
# Ten skrypt przygotowuje WYLACZNIE material na hoscie kontrolnym. Wdrozenie kazdej
# fazy to osobny, jawny krok operatora:
#
#   tests/lab/tls/rotate-ca.sh <cn> <san1,san2,...> trust-both
#   make cluster-tls-rotate CLUSTER=<klaster>          # wezly ufaja STAREMU i NOWEMU
#
#   tests/lab/tls/rotate-ca.sh <cn> <san1,san2,...> reissue
#   make cluster-tls-rotate CLUSTER=<klaster>          # lisc od NOWEGO CA
#
#   tests/lab/tls/rotate-ca.sh <cn> <san1,san2,...> retire-old
#   make cluster-tls-rotate CLUSTER=<klaster>          # zaufanie tylko do NOWEGO
#
# Rozdzielenie faz jest celowe: miedzy nimi nalezy potwierdzic zdrowie klastra.
# `make cluster-tls-rotate` sam w sobie dowodzi, ze wezel serwuje nowy material
# i przechodzi brame zdrowia, a takze aktualizuje kopie CA na wezlach ProxySQL.
set -euo pipefail

if [ $# -ne 3 ]; then
  echo "Uzycie: $0 <cn> <san1,san2,...> <trust-both|reissue|retire-old>" >&2
  exit 2
fi

CN="$1"
SAN_INPUT="$2"
PHASE="$3"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${CN%%-*}"

[ -d "$DIR" ] || { echo "FAIL: brak katalogu $DIR" >&2; exit 1; }
umask 0077

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

case "$PHASE" in
  trust-both)
    [ -r "$DIR/ca.pem" ] || { echo "FAIL: brak $DIR/ca.pem" >&2; exit 1; }
    if [ -r "$DIR/ca-next.pem" ]; then
      echo "== Nowe CA juz istnieje ($DIR/ca-next.pem) — nie nadpisuje"
    else
      echo "== Tworze NOWE CA: CN=${CN} CA (next)"
      openssl req -x509 -newkey rsa:4096 -sha256 -days 1095 -nodes \
        -keyout "$DIR/ca-next-key.pem" -out "$DIR/ca-next.pem" \
        -subj "/CN=${CN} CA next" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
    fi
    # Zachowujemy stare CA osobno: faza retire-old musi wiedziec, co usuwa,
    # a audyt musi umiec odtworzyc, czemu ufal klaster w oknie przejsciowym.
    [ -r "$DIR/ca-previous.pem" ] || cp "$DIR/ca.pem" "$DIR/ca-previous.pem"
    cat "$DIR/ca-previous.pem" "$DIR/ca-next.pem" > "$DIR/ca.pem"
    echo "== ca.pem = bundle (stare + nowe): $(grep -c 'BEGIN CERTIFICATE' "$DIR/ca.pem") certyfikaty"
    ;;

  reissue)
    [ -r "$DIR/ca-next.pem" ] && [ -r "$DIR/ca-next-key.pem" ] \
      || { echo "FAIL: brak nowego CA — najpierw faza trust-both" >&2; exit 1; }
    grep -q 'BEGIN CERTIFICATE' "$DIR/ca.pem" || { echo "FAIL: pusty ca.pem" >&2; exit 1; }
    if [ "$(grep -c 'BEGIN CERTIFICATE' "$DIR/ca.pem")" -lt 2 ]; then
      echo "FAIL: ca.pem nie jest bundlem dwoch CA — wdroz faze trust-both zanim" >&2
      echo "      wystawisz lisc od nowego CA, inaczej peery go nie zweryfikuja." >&2
      exit 1
    fi
    echo "== Nowy lisc podpisany NOWYM CA: CN=${CN}, SAN=${san_list}"
    openssl req -newkey rsa:4096 -sha256 -nodes \
      -keyout "$DIR/server-key.pem" -out "$DIR/server.csr" \
      -subj "/CN=${CN}" 2>/dev/null
    openssl x509 -req -in "$DIR/server.csr" -sha256 -days 1095 \
      -CA "$DIR/ca-next.pem" -CAkey "$DIR/ca-next-key.pem" -CAcreateserial \
      -out "$DIR/server-cert.pem" \
      -extfile <(printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth,clientAuth\nbasicConstraints=CA:FALSE\n' "$san_list")
    rm -f "$DIR/server.csr" "$DIR/ca-next.srl"
    openssl verify -CAfile "$DIR/ca-next.pem" "$DIR/server-cert.pem"
    ;;

  retire-old)
    [ -r "$DIR/ca-next.pem" ] || { echo "FAIL: brak $DIR/ca-next.pem" >&2; exit 1; }
    # Brama z dokumentacji: "Do not remove the old CA from trust until every node
    # has been reissued". Sprawdzamy to na materiale: lisc MUSI juz weryfikowac
    # sie samym nowym CA, inaczej zdjecie starego odetnie wezly.
    if ! openssl verify -CAfile "$DIR/ca-next.pem" "$DIR/server-cert.pem" >/dev/null 2>&1; then
      echo "FAIL: server-cert.pem NIE weryfikuje sie nowym CA — faza reissue nie" >&2
      echo "      zostala wykonana albo wdrozona. Zdjecie starego CA odcieloby wezly." >&2
      exit 1
    fi
    cp "$DIR/ca-next.pem" "$DIR/ca.pem"
    cp "$DIR/ca-next-key.pem" "$DIR/ca-key.pem"
    rm -f "$DIR/ca-next.pem" "$DIR/ca-next-key.pem" "$DIR/ca-previous.pem"
    echo "== ca.pem = tylko NOWE CA; stare wycofane"
    ;;

  *)
    echo "FAIL: nieznana faza '$PHASE' (trust-both|reissue|retire-old)" >&2
    exit 2
    ;;
esac

chmod 0600 "$DIR"/*.pem
echo "OK: faza $PHASE przygotowana w $DIR — wdroz przez 'make cluster-tls-rotate CLUSTER=<klaster>'"
