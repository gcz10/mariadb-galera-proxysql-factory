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

# 2. At least one ONLINE backend (skip if admin-check.cnf absent → port-only fallback).
if [ -f /etc/proxysql/admin-check.cnf ]; then
  online=$(mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf \
    -h127.0.0.1 -P6032 -uadmin -N -B -e \
    "SELECT COUNT(*) FROM runtime_mysql_servers WHERE status='ONLINE'" 2>/dev/null) || exit 1
  [ "${online:-0}" -ge 1 ] || exit 1
fi

exit 0
