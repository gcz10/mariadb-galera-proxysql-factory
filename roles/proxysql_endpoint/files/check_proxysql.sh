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

# 2. Wymagaj co najmniej jednego ONLINE writera w aktywnych grupach Galera.
# JOIN z runtime_mysql_galera_hostgroups (g.active=1) zapewnia:
# - węzły w offline_hostgroup nie są liczone jako zdrowe writery,
# - brak aktywnych grup (pusty runtime) daje count=0 (unhealthy),
# - przy wspólnym ProxySQL awaria jednego klastra nie zrzuca VIP drugiemu klastrowi.
if [ -f /etc/proxysql/admin-check.cnf ]; then
  online_writers=$(timeout 2 mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf \
    --connect-timeout=1 -h127.0.0.1 -P6032 -uadmin -N -B -e \
    "SELECT COUNT(*) FROM runtime_mysql_servers s
       JOIN runtime_mysql_galera_hostgroups g
         ON s.hostgroup_id = g.writer_hostgroup
      WHERE g.active = 1 AND s.status = 'ONLINE'" 2>/dev/null) || exit 1
  [ "${online_writers:-0}" -ge 1 ] || exit 1
fi

exit 0
