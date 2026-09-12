#!/bin/bash
# ISC-26: report healthy ONLY when ProxySQL actually serves traffic to a live
# backend — not merely when the client port is open.
#
# A port-only check passes for a ProxySQL whose backends are all SHUNNED/OFFLINE,
# leaving the VIP pointing at an instance that returns errors to every client.
# Backend-awareness needs the admin interface; /etc/proxysql/admin-check.cnf
# (0600 root, deployed by F7) supplies admin creds without argv exposure.
# Without that file the check CANNOT establish backend health, so it fails
# closed: the old "degrade to a TCP-open probe (legacy behavior)" contract
# cleared the VIP onto an instance that answered every client query with an
# error, because a port-only verdict cannot tell an empty platform apart from
# one whose backends are all SHUNNED.
# This does NOT deadlock a fresh platform build: `make platform-build` runs
# platform-proxysql (deploys admin-check.cnf) BEFORE platform-endpoint (deploys
# keepalived + this gate), so the credential file exists before the gate runs.
set -o pipefail

# 1. Client port (6033) must accept TCP connections.
timeout 2 bash -c 'true </dev/tcp/127.0.0.1/6033' 2>/dev/null || exit 1

# 2. Rozroznij DWA rozne stany, ktore stara wersja sklejala w jeden:
#
#    (a) ZERO aktywnych grup Galera — warstwa wspolna stoi, ale nie ma jeszcze
#        ZADNEGO najemcy. To legalny stan swiezo zbudowanej platformy: nie ma
#        klienta, ktoremu mozna by zle skierowac ruch. Stara wersja zwracala
#        tu 1, wiec Keepalived nigdy nie bral VIP-a i `make platform-build`
#        nie mogl sie skonczyc bez uprzedniego zarejestrowania klastra —
#        czyli warstwa NIE byla niezalezna od najemcow, wbrew swojej tezie.
#        Wykryte dopiero przy odbudowie pary od zera (2026-08-21).
#
#    (b) SA aktywne grupy, ale zaden writer nie jest ONLINE — to awaria, ktorej
#        ISC-26 ma pilnowac: VIP nie moze wskazywac instancji odpowiadajacej
#        bledem na kazde zapytanie. Ten warunek zostaje bez zmian.
#
# Przy wspolnym ProxySQL nadal wystarczy JEDEN zdrowy najemca — awaria jednego
# klastra nie zrzuca VIP-a drugiemu.
# Sciezka poswiadczen jest nadpisywalna WYLACZNIE po to, by testy mogly wykonac
# te sama bramke, ktora uruchamia Keepalived (bez env dostaje ten sam domyslny
# plik). Wczesniej zgodnosc bramki z sonda pilnowal test czytajacy TEKST obu
# plikow — przechodzil takze wtedy, gdy liczyly rozne rzeczy.
ADMIN_CNF=${PROXYSQL_ADMIN_CNF:-/etc/proxysql/admin-check.cnf}
# Brak pliku = brak mozliwosci ustalenia stanu backendow. Sama otwartosc portu
# nie odroznia pustej platformy od takiej, w ktorej WSZYSTKIE backendy sa
# SHUNNED — a to wlasnie ten drugi przypadek jest "czarna dziura", przed ktora
# broni ISC-26. Fail-closed zamiast cichej degradacji do sondy TCP.
if [ ! -f "$ADMIN_CNF" ]; then
  echo "check_proxysql: brak $ADMIN_CNF - nie da sie ustalic stanu backendow ProxySQL (warstwa wspolna wdrazana przez 'make platform-proxysql')" >&2
  exit 1
fi
row=$(timeout 2 mariadb --defaults-extra-file="$ADMIN_CNF" \
  --connect-timeout=1 -h127.0.0.1 -P6032 -uadmin -N -B -e \
  "SELECT
     (SELECT COUNT(*) FROM runtime_mysql_galera_hostgroups WHERE active = 1),
     (SELECT COUNT(*) FROM runtime_mysql_servers s
        JOIN runtime_mysql_galera_hostgroups g
          ON s.hostgroup_id = g.writer_hostgroup
       WHERE g.active = 1 AND s.status = 'ONLINE')" 2>/dev/null) || exit 1
# shellcheck disable=SC2086 # celowy split po tabulatorze z -N -B
set -- $row
active_groups=${1:-0}
online_writers=${2:-0}
if [ "$active_groups" -gt 0 ]; then
  [ "$online_writers" -ge 1 ] || exit 1
fi

exit 0
