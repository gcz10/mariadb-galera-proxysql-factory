# Runbook: Restore i Restore Drill

**Status:** Aktualny (F10 complete)
**Powiązane ISC:** ISC-36, ISC-37, ISC-38

## Przeznaczenie

Odtwarzanie bazy z backupu na izolowany host + okresowy restore drill.

## Procedura — Restore drill na izolowany host (rnode1)

```bash
# Restore drill odtwarza najnowszy backup na czysty izolowany host (rnode1),
# weryfikuje checksum (ISC-34), integralność CHECK TABLE (ISC-36) i liczbę wierszy.
# ISC-37: drill według restore_test_schedule (0 4 * * 0).
# CONFIRM=yes jest wymagane przez Makefile — drill kasuje datadir hosta restore.
make cluster-restore-drill CLUSTER=<name> CONFIRM=yes
```

## Wymagania (ISC)

- ISC-36: Restore na czysty izolowany host przechodzi test integralności (checksum + zapytanie)
- ISC-37: Restore drill według harmonogramu (cotygodniowo)
- ISC-38: Nieudany backup lub przeterminowany restore test generuje alert

## Restore Drill (automatyczny)

```bash
# Uruchamia restore na izolowanym hoście (rnode1), weryfikuje integralność, raportuje PASS/FAIL
# Nieudany drill → alert do monitorowanego kanału (ISC-38)
make cluster-restore-drill CLUSTER=<name> CONFIRM=yes

# Zweryfikuj stan restore drill (ISC-36/37)
make lab-restore-verify CLUSTER=<name>
```

## Anti

- Restore NIE na produkcję — destrukcyjne testy tylko w laboratorium (ISC-64)
- Restore NIE nadpisuje istniejący datadir produkcyjny
