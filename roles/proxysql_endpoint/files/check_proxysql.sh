#!/bin/bash
# ISC-26: report healthy ONLY when ProxySQL actually serves traffic to a live
# backend — not merely when the client port is open.
#
# A port-only check passes for a ProxySQL whose backends are all SHUNNED/OFFLINE,
# leaving the VIP pointing at an instance that returns errors to every client.
# Backend-awareness needs the admin interface; /etc/proxysql/admin-check.cnf
# (0600 root, deployed by F7) supplies admin creds without argv exposure.
# If that file is absent, the check degrades to a TCP-open probe (legacy behavior).
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
if [ -f /etc/proxysql/admin-check.cnf ]; then
  row=$(timeout 2 mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf \
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
fi

exit 0
