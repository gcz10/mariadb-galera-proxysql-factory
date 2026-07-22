# Runbook: Rolling Upgrade

**Status:** STUB — do uzupełnienia w F12
**Powiązane ISC:** ISC-50, ISC-51, ISC-52, ISC-53, ISC-54, ISC-55, ISC-56, ISC-57

## Przeznaczenie

Patch i major upgrade klastra Galera + ProxySQL z zachowaniem dostępności.

## Patch (minor/patch upgrade)

```bash
# 1. Plan (read-only)
make cluster-patch-plan CLUSTER=<name>

# 2. Canary — jeden węzeł poza aktywnym writerem (ISC-52)
# 3. Drain, jedna zmiana naraz, serial:1 (ISC-50)
# 4. Synced + health przed kolejnym node (ISC-51)
# 5. Writer na końcu
make cluster-patch CLUSTER=<name>
```

## Major upgrade

```bash
# 1. Plan (read-only) — ISC-53
make cluster-upgrade-plan CLUSTER=<name>

# 2. Warunki wstępne:
#    - świeży backup + udany restore test
#    - zatwierdzony maintenance window
#    - warunki stopu zapisane przed wykonaniem

# 3. Canary
make cluster-upgrade-canary CLUSTER=<name>

# 4. Full upgrade (jeśli canary PASS)
make cluster-upgrade CLUSTER=<name>
```

## Anti-criteria

- ISC-56: Major rollback NIE wykonuje downgrade istniejącego datadir
- ISC-55: Upgrade zatrzymuje się po utracie zdrowia klastra
- ISC-57: ProxySQL aktualizuje się osobno, jedną instancję naraz
- ISC-31: Żaden playbook nie restartuje wszystkich węzłów jednocześnie

## Ścieżka major upgrade

- Musi pochodzić z oficjalnej dokumentacji MariaDB/Galera (ISC-54)
- Mixed-version cluster tylko przez ograniczony czas
- Rollback przez stary klaster lub restore (nie downgrade datadir)
