#!/usr/bin/env bash
# gate-build.sh — POLITYKA bramki cluster-build, wydzielona z Makefile (F4).
#
# Makefile zostaje orkiestracja: kolejnosc celow i bramki sekretow. Ten skrypt
# trzyma logike, ktora w shell-inside-make rosla poza czytelnosc:
#   preflight — odmowa PRZED budowa: sprzezenie seed->backup (lab/staging) albo
#               wymagane kroki i backup (production),
#   steps     — mapa krok warunkowy -> cele make, z pomijaniem przez BUILD_SKIP.
#
# SRODOWISKO (cluster.environment z cluster.yml) jest jawnym argumentem obu
# podkomend — skrypt niczego nie zgaduje i nie ma wartosci domyslnej: pusty albo
# nieznany argument to odmowa, wiec nieczytelny cluster.yml nie zamienia sie
# po cichu w bramke laboratoryjna.
#
#                      laboratory / staging     production
#   seed               tak (lab-seed-smoke)     NIGDY
#   app-host           tak                      wymagany
#   backup             configure, backup,       configure, backup,
#                      restore drill, refresh   refresh (BEZ restore drill)
#   alerts             tak                      wymagany
#   BUILD_SKIP         dowolny krok             tylko `seed` (i tak nie biegnie)
#   backup.enabled     cele same to pomijaja    false = odmowa
#
# Restore drill kasuje datadir hosta grupy restore, a seed pisze dane
# uzytkownika — oba sa czynnosciami laboratoryjnymi i produkcyjna bramka ich
# nie uruchamia. Produkcja nie ma za to prawa pominac backupu, alertow ani
# hosta aplikacyjnego: pominiety krok wygladalby po budowie jak zielona bramka
# bez tych krokow, wiec BUILD_SKIP jest tam odrzucany z gory.
#
# Zmienne najemcy (CLUSTER, ANSIBLE_OPTS, zmienne srodowiskowe) przeplywaja
# przez MAKEFLAGS — skrypt wolia ten sam binarny make, ktory wystartowal bramke.
set -euo pipefail
# BUILD_SKIP dzielimy po bialych znakach; bez tego `*` w nim rozwinelby sie
# w nazwy plikow katalogu roboczego.
set -f

usage() {
	echo "usage: $0 preflight <environment> <backup_enabled> <build_skip> <existing_data>" >&2
	echo "       $0 steps <environment> <build_skip>" >&2
	echo "       environment: laboratory | staging | production" >&2
	exit 2
}

require_environment() {
	case "$1" in
		laboratory | staging | production) ;;
		*)
			echo "ERROR: nieznane srodowisko '$1' — dozwolone: laboratory, staging, production (cluster.environment w cluster.yml)." >&2
			exit 1
			;;
	esac
}

# is_skipped <build_skip> <krok> — jedno parsowanie BUILD_SKIP dla preflight
# i steps, zeby oba widzialy te same tokeny niezaleznie od rodzaju bialych znakow.
is_skipped() {
	local token
	for token in $1; do
		[ "$token" = "$2" ] && return 0
	done
	return 1
}

# Sprzezenie seed->backup (lab/staging): jedyny legalny zbior skrotow, ktory
# pomija seed a zostawia backup, to jawnie zadeklarowane dane uzytkownika
# (EXISTING_DATA=yes). Na klastrze bez backupu drill nie istnieje, wiec
# sprzezenie jest wtedy rozbrojone.
preflight_seed_coupling() {
	local backup_enabled="$1" build_skip="$2" existing_data="$3"
	[ "$backup_enabled" = "true" ] || return 0
	if is_skipped "$build_skip" seed && ! is_skipped "$build_skip" backup && [ "$existing_data" != "yes" ]; then
		echo "ERROR: BUILD_SKIP pomija seed, ale nie backup — restore drill nie mialby czego przywrocic." >&2
		echo '       Pomin tez backup (BUILD_SKIP="seed backup") albo zadeklaruj EXISTING_DATA=yes.' >&2
		exit 1
	fi
}

# Production: app-host, backup i alerts sa WYMAGANE. `seed` w BUILD_SKIP jest
# dozwolony, bo produkcja i tak go nie uruchamia (pominiecie niczego nie
# zmienia); kazdy inny token, takze literowka, jest odmowa — nieznany token
# wygladalby na pominiety krok, ktorego nikt nie pominal.
# Zwraca 1 po wypisaniu WSZYSTKICH naruszen, zeby operator poprawil je naraz.
production_skip_refusals() {
	local token refused=0
	for token in $1; do
		case "$token" in
			seed) ;;
			app-host | backup | alerts)
				echo "ERROR: BUILD_SKIP=$token jest niedozwolone w production — krok jest wymagany." >&2
				refused=1
				;;
			*)
				echo "ERROR: BUILD_SKIP zawiera nieznany krok '$token' — w production dozwolone jest wylacznie 'seed'." >&2
				refused=1
				;;
		esac
	done
	return "$refused"
}

# backup.enabled=false w cluster.yml oznaczaloby, ze cele backupu koncza sie
# zielonym "SKIP" — production tego nie przepuszcza. Analogiczna deklaracje
# monitoringu (alerty, metryki swiezosci) egzekwuje walidator profilu produkcji
# w cluster-validate; ten skrypt dostaje tylko deklaracje backupu.
preflight_production() {
	local backup_enabled="$1" build_skip="$2" refused=0
	if [ "$backup_enabled" != "true" ]; then
		echo "ERROR: production wymaga backup.enabled=true w cluster.yml — bez backupu bramka nie ma czego potwierdzic." >&2
		refused=1
	fi
	production_skip_refusals "$build_skip" || refused=1
	[ "$refused" -eq 0 ] || exit 1
}

preflight() {
	local environment="$1" backup_enabled="$2" build_skip="$3" existing_data="$4"
	require_environment "$environment"
	if [ "$environment" = "production" ]; then
		preflight_production "$backup_enabled" "$build_skip"
	else
		preflight_seed_coupling "$backup_enabled" "$build_skip" "$existing_data"
	fi
}

# Kolejnosc krokow jest kontraktem: seed zasilaj backup, app-host (material TLS
# i konto aplikacyjne na hoscie app) przed testami danych — bramka
# app-conformance nie ma prawa padac na braku infrastruktury, co zmierzone
# 2026-08-31 na orionv13-r10 (drill padl na pustym buckecie, app-host nigdy nie
# wstal). Backup musi istniec zanim cokolwiek go odtworzy, a metryki swiezosci
# odswiezaja sie PO nim (w lab/staging: po drille).
plan() {
	case "$1" in
		production) echo "app-host backup alerts" ;;
		*) echo "seed app-host backup alerts" ;;
	esac
}

# run_step <srodowisko> <krok> — cele make jednego kroku. `make` przerywa na
# pierwszym bledzie (set -e).
run_step() {
	local environment="$1" step="$2"
	local make_bin="${MAKE:-make}"
	case "$step" in
		seed)
			"$make_bin" lab-seed-smoke
			;;
		app-host)
			"$make_bin" cluster-app-host
			;;
		backup)
			"$make_bin" cluster-backup-configure
			"$make_bin" cluster-backup
			if [ "$environment" != "production" ]; then
				"$make_bin" cluster-restore-drill CONFIRM=yes
			fi
			"$make_bin" cluster-monitoring-refresh
			;;
		alerts)
			"$make_bin" cluster-alerts
			;;
	esac
}

steps() {
	local environment="$1" build_skip="$2" step
	require_environment "$environment"
	# Odmowa PRZED pierwszym celem make: steps bywa wolane bez preflight, a
	# pominiety wymagany krok produkcji nie moze zaczac sie od polowy pracy.
	if [ "$environment" = "production" ] && ! production_skip_refusals "$build_skip"; then
		exit 1
	fi
	for step in $(plan "$environment"); do
		if is_skipped "$build_skip" "$step"; then
			continue
		fi
		run_step "$environment" "$step"
	done
}

[ "$#" -ge 1 ] || usage
case "$1" in
	preflight) [ "$#" -eq 5 ] || usage; shift; preflight "$@" ;;
	steps) [ "$#" -eq 3 ] || usage; shift; steps "$@" ;;
	*) usage ;;
esac
