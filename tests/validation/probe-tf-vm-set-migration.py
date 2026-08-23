#!/usr/bin/env python3
"""Migracja VM do modulu terraform/modules/pve_vm_set nie moze niszczyc maszyn.

Rooty stanu floty korzystaja ze wspolnego modulu VM zamiast wlasnych kopii
`proxmox_virtual_environment_vm`.
Sonda jest bramka bezpieczenstwa tej migracji: dla kazdego roota wykonuje
`terraform plan -refresh=false` (zero polaczen do PVE, zero apply) i wymaga,
ze plan NIE zawiera akcji destroy/create. Kazde "replace" to utrata zywej VM.

Dlaczego -refresh=false: refresh rozmawia z API hypervisora, a ta sonda ma
dowodzic wylacznie ekwiwalencji konfiguracji ze stanem po przeniesieniu
adresow (`moved`), a nie aktualnego driftu infrastruktury.

Sonda jest lokalna: wymaga plikow terraform.tfstate (gitignored) i binarki
terraform, wiec nie jest podpieta do CI. Uwierzytelnienie providera jest
fikcyjne — przy -refresh=false klient PVE nie wykonuje zadnego wywolania.

Dodatkowo sonda kontroluje, ze stan roota nie zmienial sie podczas przebiegu
(serial przed = serial po), czyli ze przypadkiem nie odpalono apply/refresh.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = REPO_ROOT / "terraform" / "modules" / "pve_vm_set"

# Rooty stanu floty. Kazdy ma osobny state i musi zostac osobnym rootem —
# warstwa wspoldzielona nie moze przypadkiem polaczyc sie z klastrem konsumenta.
ROOTS = ("shared", "finalclaude-r10")
# Root zbudowany od zera na module (n17): pusty state jest normą do momentu
# pierwszego apply, wiec sonda planu go pomija — brak destroy/create jest tu
# gwarantowany konstrukcyjnie, a nie przez migracje adresow.
FRESH_ROOTS = ("newclaude17-r9",)

# Tylko te rooty MIGROWALY z wlasnej kopii zasobu do modulu, wiec tylko one
# maja blok `moved`. Root zalozony od razu na module go nie ma i miec nie
# powinien: `moved` bez historii adresu jest martwym kodem, ktory sugeruje
# migracje, ktorej nigdy nie bylo.
MIGRATED_ROOTS = ("shared", "finalclaude-r10")

# Fikcyjne uwierzytelnienie: plan -refresh=false nie laczy sie z API, ale
# provider i tak wymaga zmiennych podczas konfiguracji klienta.
FAKE_ENV = {
    "PROXMOX_VE_ENDPOINT": "https://127.0.0.1:8006",
    "PROXMOX_VE_INSECURE": "true",
    "PROXMOX_VE_API_TOKEN": "root@pam!plan=00000000-0000-0000-0000-000000000000",
}

# Akcje planu oznaczajace utrate maszyny: "delete" (usuniecie) oraz "create"
# (nowy zasob tam, gdzie mial byc moved — czyli ktorys adres nie zostal
# przeniesiony i Terraform buduje VM od zera).
FATAL_ACTIONS = ("delete", "create")


def run(cmd, cwd, env=None):
    """Zwraca (rc, stdout+stderr). Nie rzuca — rc analizuje wolaczacy."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        cmd, cwd=cwd, env=full_env, capture_output=True, text=True, timeout=600
    )
    return proc.returncode, proc.stdout + proc.stderr


def state_serial(root_dir):
    """Serial stanu roota (int) albo None, gdy stan nie istnieje."""
    state = root_dir / "terraform.tfstate"
    if not state.exists():
        return None
    with open(state, encoding="utf-8") as handle:
        return json.load(handle).get("serial")


def check_module_files(errors):
    expected = ("main.tf", "variables.tf", "outputs.tf")
    if not MODULE_DIR.is_dir():
        errors.append(f"brak katalogu modulu: {MODULE_DIR.relative_to(REPO_ROOT)}")
        return
    for name in expected:
        if not (MODULE_DIR / name).is_file():
            errors.append(f"brak pliku modulu: {MODULE_DIR.name}/{name}")


def check_root_config(root, errors):
    """Statyczna kontrola migracji roota: modul + moved, zero kopii zasobu."""
    main_tf = (REPO_ROOT / "terraform" / root / "main.tf").read_text(encoding="utf-8")

    if 'source = "../modules/pve_vm_set"' not in main_tf:
        errors.append(f"{root}: main.tf nie korzysta z modulu pve_vm_set")
    if re.search(r'^resource\s+"proxmox_virtual_environment_vm"', main_tf, re.M):
        errors.append(
            f"{root}: main.tf definiuje wlasny proxmox_virtual_environment_vm "
            "(migracja do modulu zakonczona tylko przy jednym autorze zasobu)"
        )
    moved = re.search(r"moved\s*\{(.*?)\}", main_tf, re.S)
    if root not in MIGRATED_ROOTS:
        if moved:
            errors.append(
                f"{root}: root zalozony na module nie migrowal, wiec nie moze "
                "miec bloku moved"
            )
        return
    if not moved:
        errors.append(f"{root}: brak bloku moved — plan pokaze destroy/create")
        return
    body = moved.group(1)
    # terraform fmt wyrownuje `from`/`to` wielokrotnoscia spacji, wiec
    # dopasowanie musi byc odporne na wciecia po znaku rownosci.
    if not re.search(r"from\s*=\s*proxmox_virtual_environment_vm\.node", body):
        errors.append(f"{root}: moved.from != proxmox_virtual_environment_vm.node")
    if not re.search(r"to\s*=\s*module\.vms\.proxmox_virtual_environment_vm\.node", body):
        errors.append(
            f"{root}: moved.to != module.vms.proxmox_virtual_environment_vm.node"
        )


def plan_root(root, errors, warnings):
    """Plan offline jednego roota; True, gdy bez akcji destroy/create."""
    root_dir = REPO_ROOT / "terraform" / root
    serial_before = state_serial(root_dir)
    if serial_before is None:
        errors.append(f"{root}: brak terraform.tfstate — sonda wymaga stanu lokalnego")
        return False

    rc, out = run(["terraform", "init", "-input=false", "-lockfile=readonly"], root_dir)
    if rc != 0:
        errors.append(f"{root}: terraform init nieudane: {out.strip()[:400]}")
        return False

    with tempfile.TemporaryDirectory() as tmp:
        plan_file = str(Path(tmp) / "plan.tfplan")
        rc, out = run(
            [
                "terraform", "plan",
                "-refresh=false", "-input=false", "-lock=false",
                "-detailed-exitcode", f"-out={plan_file}",
            ],
            root_dir,
            env=FAKE_ENV,
        )
        # 0 = brak zmian, 2 = sa zmiany (akceptowalne, jesli bez destroy/create).
        if rc not in (0, 2):
            errors.append(f"{root}: terraform plan padl (rc={rc}): {out.strip()[:400]}")
            return False

        rc, out = run(["terraform", "show", "-json", plan_file], root_dir)
        if rc != 0:
            errors.append(f"{root}: terraform show -json padl: {out.strip()[:400]}")
            return False
        changes = json.loads(out).get("resource_changes", [])

    fatal, updates, moved_ok = [], [], True
    for change in changes:
        address = change.get("address", "?")
        actions = change.get("change", {}).get("actions", [])
        if any(a in FATAL_ACTIONS for a in actions):
            fatal.append(f"{address}: {'+'.join(actions)}")
        elif "update" in actions or "move" not in actions:
            # Sam "move" (bez update) to czyste przeniesienie adresu — dokladnie
            # cel migracji. Cokolwiek innego poza no-op wartuje uwage operatora.
            if actions != ["no-op"]:
                updates.append(f"{address}: {'+'.join(actions)}")
        if not address.startswith("module.vms."):
            moved_ok = False

    if fatal:
        errors.append(f"{root}: plan niszczy/tworzy zasoby: {'; '.join(fatal)}")
    if not moved_ok:
        errors.append(f"{root}: zasoby nie sa pod module.vms — moved nie zadzialal")
    if updates:
        warnings.append(f"{root}: plan zawiera zmiany in-place: {'; '.join(updates)}")

    serial_after = state_serial(root_dir)
    if serial_after != serial_before:
        errors.append(
            f"{root}: serial stanu zmienil sie podczas sondy ({serial_before} -> "
            f"{serial_after}) — sonda nie moze modyfikowac stanu"
        )
    return not fatal


def main():
    errors, warnings = [], []
    check_module_files(errors)
    for root in ROOTS:
        check_root_config(root, errors)
    for root in FRESH_ROOTS:
        check_root_config(root, errors)

    # Plan offline tylko, gdy statyczna struktura jest na miejscu — inaczej
    # komunikat o padlym init zaciemnialby faktyczna przyczyne (brak modulu).
    if not errors:
        for root in ROOTS:
            plan_root(root, errors, warnings)

    for warning in warnings:
        print(f"UWAGA: {warning}")
    if errors:
        for error in errors:
            print(f"BLAD: {error}")
        print(f"\nSONDA PADLA: {len(errors)} problemow")
        return 1
    print(f"OK: {len(ROOTS)} rooty planuja 0 destroy/create na module pve_vm_set; "
          f"{len(FRESH_ROOTS)} swiezy root (n17) sprawdzony statycznie")
    return 0


if __name__ == "__main__":
    sys.exit(main())
