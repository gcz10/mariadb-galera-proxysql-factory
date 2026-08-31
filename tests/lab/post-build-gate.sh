#!/usr/bin/env bash
# Bramka po budowie (lab-post-build-gate) — cialo wyjete z Makefile (F4, code review).
#
# Jedno polecenie po zbudowaniu klastra: wszystkie sondy STANU USTALONEGO.
# Kazda sonda jest fail-closed (tests/lab/_probe_common.py): brak odpowiedzi
# hosta to UNDETERMINED (exit 2), nie zielone "wszystko OK". Pierwszy niezerowy
# kod konczy bramke — nie ma sensu mierzyc dalej na klastrze, ktory nie
# przeszedl kontraktu.
#
# Uruchamiana wylacznie przez `make lab-post-build-gate CLUSTER=<name>`: cel
# doklada straznik CLUSTER oraz CLUSTER_CONFIG/CLUSTER_INVENTORY (TARGET_ENV),
# ktore przez srodowisko trafiaja tu i do kazdej sondy ponizej.
set -euo pipefail
cd "$(dirname "$0")/../.."

# Straznicy sekretow PRZED sondami: brak zmiennej wychodzil dopiero w 13. sondzie,
# po kilkunastu minutach pracy calej bramki. Kazdy sekret uzywany nizej jest
# sprawdzany tutaj, nawet jesli jego sonda stoi na koncu listy.
: "${APP_DB_PASSWORD:?Ustaw APP_DB_PASSWORD poza repozytorium}"
: "${PMM_ADMIN_PASSWORD:?Ustaw PMM_ADMIN_PASSWORD poza repozytorium}"

# PIERWSZA, nie ostatnia: uruchamia converge po raz drugi, wiec wszystkie
# sondy ponizej mierza stan JUZ PO nim. Odwrotna kolejnosc dawalaby zielone
# swiatlo stanowi, ktorego nikt potem nie sprawdzil. CoP stawia ten warunek
# bezwarunkowo, a repo mialo tu dziure mimo 501 testow jednostkowych.
tests/lab/probe-idempotence.py
tests/lab/probe-galera-cluster.py
tests/lab/probe-proxysql.py
tests/lab/probe-endpoint.py
tests/lab/probe-hardening.py
APP_DB_PASSWORD="$APP_DB_PASSWORD" tests/lab/probe-app-conformance.py
tests/lab/probe-backup.py
tests/lab/probe-restore.py
tests/lab/probe-rolling-restart.py
tests/lab/probe-upgrade-plan.py
tests/lab/probe-patch.py
tests/lab/probe-drift.py
tests/lab/probe-gcache.py
PMM_ADMIN_PASSWORD="$PMM_ADMIN_PASSWORD" tests/lab/probe-pmm-native.py

echo "PASS: brama po budowie — wszystkie sondy stanu ustalonego zmierzone i zielone"
