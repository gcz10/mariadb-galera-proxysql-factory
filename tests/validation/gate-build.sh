#!/usr/bin/env bash
# gate-build.sh — POLITYKA bramki cluster-build, wydzielona z Makefile (F4).
#
# Makefile zostaje orkiestracja: kolejnosc celow i bramki sekretow. Ten skrypt
# trzyma logike, ktora w shell-inside-make rosla poza czytelnosc:
#   preflight — sprzezenie seed->backup (pominiecie seedu bez backupu daje
#               restore drill bez czego przywracac),
#   steps     — mapa krok warunkowy -> cele make, z pomijaniem przez BUILD_SKIP.
#
# Zmienne najemcy (CLUSTER, ANSIBLE_OPTS, zmienne srodowiskowe) przeplywaja
# przez MAKEFLAGS — skrypt wolia ten sam binarny make, ktory wystartowal bramke.
set -euo pipefail

usage() {
	echo "usage: $0 preflight <backup_enabled> <build_skip> <existing_data>" >&2
	echo "       $0 steps <build_skip>" >&2
	exit 2
}

# Sprzezenie seed->backup: jedyny legalny zbior skrotow, ktory pomija seed a
# zostawia backup, to jawnie zadeklarowane dane uzytkownika (EXISTING_DATA=yes).
preflight() {
	local backup_enabled="$1" build_skip="$2" existing_data="$3"
	[ "$backup_enabled" = "true" ] || return 0
	case " $build_skip " in
		*" seed "*)
			case " $build_skip " in
				*" backup "*) ;;
				*)
					if [ "$existing_data" != "yes" ]; then
						echo "ERROR: BUILD_SKIP pomija seed, ale nie backup — restore drill nie mialby czego przywrocic." >&2
						echo '       Pomin tez backup (BUILD_SKIP="seed backup") albo zadeklaruj EXISTING_DATA=yes.' >&2
						exit 1
					fi
					;;
			esac
			;;
	esac
}

# Kroki warunkowe budowy. Kolejnosc jest kontraktem: seed zasilaj backup,
# backup musi istniec zanim drill go odtworzy, a metryki swiezosci odswiezaja
# sie PO drille. `make` przerywa na pierwszym bledzie (set -e).
steps() {
	local build_skip="$1"
	local make_bin="${MAKE:-make}"
	local step
	for step in seed backup alerts app-host; do
		case " $build_skip " in
			*" $step "*) continue ;;
		esac
		case $step in
			seed)
				"$make_bin" lab-seed-smoke
				;;
			backup)
				"$make_bin" cluster-backup-configure
				"$make_bin" cluster-backup
				"$make_bin" cluster-restore-drill CONFIRM=yes
				"$make_bin" cluster-monitoring-refresh
				;;
			alerts)
				"$make_bin" cluster-alerts
				;;
			app-host)
				"$make_bin" cluster-app-host
				;;
		esac
	done
}

[ "$#" -ge 1 ] || usage
case "$1" in
	preflight) [ "$#" -eq 4 ] || usage; shift; preflight "$@" ;;
	steps) [ "$#" -eq 2 ] || usage; shift; steps "$@" ;;
	*) usage ;;
esac
