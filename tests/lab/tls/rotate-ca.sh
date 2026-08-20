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
#                                                       (server-cert i wszystkie
#                                                       liscie per wezel)
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
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="$BASE/${CN%%-*}"

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
    # Liscie PER WEZEL wdrazane sa z tego katalogu (tls_certs.yml wyprowadza
    # node-<host>-cert.pem z inventory_hostname), wiec NOWE CA musi podpisac
    # rowniez je — inaczej po retire-old wezly prezentowalyby lisc, ktorego
    # juz nikt nie ufa. Tozsamosc (host=ip) czytamy ze STAREGO liscia: SAN to
    # zawsze "DNS:<host>, IP:<ip>" (issue-node-certs.sh), a tekst z -text dziala
    # i na LibreSSL, i na OpenSSL 3 (-ext subjectAltName nie ma w LibreSSL).
    node_pairs=""
    shopt -s nullglob
    for cert in "$DIR"/node-*-cert.pem; do
      host="$(basename "$cert" | sed 's/^node-//; s/-cert\.pem$//')"
      ip="$(openssl x509 -in "$cert" -noout -text \
        | sed -n '/Subject Alternative Name/{n;p;}' | tr -d ' ' | tr ',' '\n' \
        | sed -n 's/^IPAddress://p' | head -1)"
      [ -n "$ip" ] || { echo "FAIL: $cert nie ma SAN IP — nie umiem odtworzyc host=ip do reissue" >&2; exit 1; }
      node_pairs="${node_pairs},${host}=${ip}"
    done
    shopt -u nullglob
    if [ -n "$node_pairs" ]; then
      echo "== Reissue lisci per wezel z NOWEGO CA:${node_pairs}"
      # issue-node-certs.sh to kanoniczny wystawiacz lisci per wezel; jawnie
      # wskazujemy CA_FILE/CA_KEY na nowe CA (skrypt odmawia bundle'y).
      CA_FILE="$DIR/ca-next.pem" CA_KEY="$DIR/ca-next-key.pem" \
        "$BASE/issue-node-certs.sh" "${DIR##*/}" "${node_pairs#,}" >/dev/null
    fi
    openssl verify -CAfile "$DIR/ca-next.pem" "$DIR/server-cert.pem"
    ;;

  retire-old)
    [ -r "$DIR/ca-next.pem" ] || { echo "FAIL: brak $DIR/ca-next.pem" >&2; exit 1; }
    # Brama z dokumentacji: "Do not remove the old CA from trust until every node
    # has been reissued". Sprawdzamy CALY material, ktory po tej fazie wdrozy
    # tls_certs.yml: w trybie per-node liscie node-<host>-cert.pem (WSZYSTKIE —
    # jeden niedoreissue'niety wezel wystarczy, by odciac go od klastra),
    # w trybie wspolnym server-cert.pem.
    verified=0
    shopt -s nullglob
    for cert in "$DIR"/node-*-cert.pem; do
      if ! openssl verify -CAfile "$DIR/ca-next.pem" "$cert" >/dev/null 2>&1; then
        echo "FAIL: $cert NIE weryfikuje sie nowym CA — ten lisc jest wdrazany na" >&2
        echo "      wezel, a faza reissue najwyrazniej go pominela. Zdjecie starego" >&2
        echo "      CA odcieloby ten wezel od klastra." >&2
        exit 1
      fi
      verified=1
    done
    shopt -u nullglob
    if [ -r "$DIR/server-cert.pem" ]; then
      if ! openssl verify -CAfile "$DIR/ca-next.pem" "$DIR/server-cert.pem" >/dev/null 2>&1; then
        echo "FAIL: server-cert.pem NIE weryfikuje sie nowym CA — faza reissue nie" >&2
        echo "      zostala wykonana albo wdrozona. Zdjecie starego CA odcieloby wezly." >&2
        exit 1
      fi
      verified=1
    fi
    [ "$verified" -eq 1 ] || { echo "FAIL: brak materialu do sprawdzenia (ani server-cert.pem, ani lisci per wezel) — najpierw faza reissue" >&2; exit 1; }
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
