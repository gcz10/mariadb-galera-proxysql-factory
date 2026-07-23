# Runbook: Rolling Upgrade

**Status:** Aktualny (F12 complete)
**Powiązane ISC:** ISC-50, ISC-51, ISC-52, ISC-53, ISC-54, ISC-55, ISC-56, ISC-57

## Przeznaczenie

Patch i major upgrade klastra Galera + ProxySQL z zachowaniem dostępności.

## Patch (minor/patch upgrade) — ISC-52/55/57

```bash
# Rolling patch z canary (non-writer pierwszy) + bramą zdrowia (ISC-52/55).
# ProxySQL aktualizuje się osobno, jedną instancję naraz (ISC-57).
make cluster-patch CLUSTER=<name>

# Zweryfikuj wzorzec canary patch (ISC-52/55/57)
make lab-patch-verify CLUSTER=<name>
```

## Major upgrade — ISC-53/54/56

```bash
# 1. Plan (read-only) — ISC-53/54: oficjalna ścieżka MariaDB/Galera (11.4 → 11.8 LTS)
#    Generuje docs/plans/major-upgrade-plan.md. Nie modyfikuje hostów.
make cluster-upgrade-plan CLUSTER=<name>

# 2. Warunki wstępne:
#    - świeży backup (make cluster-backup) + udany restore test (make cluster-restore-drill)
#    - zatwierdzony maintenance window
#    - warunki stopu zapisane przed wykonaniem (ISC-55)

# 3. Zweryfikuj plan + anti-downgrade guard (ISC-53/54/56)
make lab-upgrade-plan-verify CLUSTER=<name>

# 4. Wykonaj rolling upgrade zgodnie z planem (canary non-writer → serial:1 → writer ostatni)
```

## Rolling restart (bez upgrade) — ISC-50/51

```bash
# serial:1, non-writer pierwszy, brama zdrowia (Synced+Primary+size) przed kolejnym węzłem
make cluster-rolling-restart CLUSTER=<name>
make lab-rolling-restart-verify CLUSTER=<name>
```

## Anti-criteria

- ISC-56: Major rollback NIE wykonuje downgrade istniejącego datadir (forward-incompatible)
- ISC-55: Upgrade zatrzymuje się po utracie zdrowia klastra (brama zdrowia)
- ISC-57: ProxySQL aktualizuje się osobno, jedną instancję naraz (serial:1 + SAVE TO DISK)
- ISC-31: Żaden playbook nie restartuje wszystkich węzłów jednocześnie

## Ścieżka major upgrade (ISC-54)

- Musi pochodzić z oficjalnej dokumentacji MariaDB/Galera (mariadb.com/kb/en/upgrading-galera-cluster)
- In-place `mariadb-upgrade --skip-write-binlog`, Galera 4 wspiera rolling
- Mixed-version cluster tylko przez ograniczony czas
- Rollback przez stary klaster lub restore (NIE downgrade datadir — ISC-56)
