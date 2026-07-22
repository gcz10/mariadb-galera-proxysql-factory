# Runbook: Restore i Restore Drill

**Status:** STUB — do uzupełnienia w F10
**Powiązane ISC:** ISC-36, ISC-37, ISC-38

## Przeznaczenie

Odtwarzanie bazy z backupu na izolowany host + okresowy restore drill.

## Procedura — Restore na izolowany host

```bash
# 1. Przygotuj czysty izolowany host (nie produkcyjny)
# 2. Odszyfruj backup
gpg -d <backup_file>.gpg | mariabackup --copy-back --target-dir=-

# 3. Uruchom MariaDB na izolowanym hoście
# 4. Test integralności
mariadb --socket=/var/lib/mysql/mysql.sock -e "CHECK TABLE <db>.*"
mariadb --socket=/var/lib/mysql/mysql.sock -e "SELECT COUNT(*) FROM <critical_table>"
```

## Wymagania (ISC)

- ISC-36: Restore na czysty izolowany host przechodzi test integralności (checksum + zapytanie)
- ISC-37: Restore drill według harmonogramu (cotygodniowo)
- ISC-38: Nieudany backup lub przeterminowany restore test generuje alert

## Restore Drill (automatyczny)

```bash
make cluster-restore-test CLUSTER=<name>
# Uruchamia restore na izolowanym hoście, weryfikuje integralność, raportuje PASS/FAIL
# Nieudany drill → alert do monitoring system (ISC-38)
```

## Anti

- Restore NIE na produkcję — destrukcyjne testy tylko w laboratorium (ISC-64)
- Restore NIE nadpisuje istniejący datadir produkcyjny
