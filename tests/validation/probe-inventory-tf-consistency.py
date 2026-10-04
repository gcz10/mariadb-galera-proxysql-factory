#!/usr/bin/env python3
"""Bramka statyczna: kazdy host z grup zarządzanych przez TF musi miec swoj
klucz w mapie `vms` wlasciwego roota terraform — i na odwrot.

Powstala z dwoch realnych defektow (audyt 2026-08-28):
1. `o8r1` (.129) zyl w `clusters/orionv8-r9/inventory.yml` (grupa restore),
   ale NIE istnial w `terraform/orionv8-r9/main.tf`. Skutek:
   `make galera-rebuild` buduje liste wezlow z INWENTARZA i podaje ja do
   `terraform destroy -target=...o8r1` — twardy blad PO przejsciu bramki
   CONFIRM; `infra-teardown` bierze VMID-y wylacznie z `terraform output`,
   wiec nigdy nie sprzatal tej maszyny ani jej sierot ZFS.
2. `cassiopeiav8-r9` byl zyjacym najemca bez zadnego roota TF — cele infra-*
   przechodzily cluster_guard i CONFIRM, po czym padaly na surowym
   `cd terraform/<nazwa>: No such file`.

Dlaczego statycznie (parsowanie main.tf), nie `terraform output`: sonda
dziala w CI bez hypervisora i bez stanu. `main.tf` jest zrodlem prawdy o
INTENCJI zbioru — a wlasnie intencja tu jest pilnowana. Rozjezd intencji ze
stanem sprawdza sam terraform przy planie.

Zasieg:
- najemca: grupy `galera` + `restore` z `clusters/<x>/inventory.yml` kontra
  `terraform/<x>/main.tf` (konwencja floty: wezel restore zarzadza najemca,
  nie platforma — por. `o9r1`/`c9r1` w roocie v9);
- platforma: grupy `proxysql` + `app` + `infra` z `platform/<p>/inventory.yml`
  kontra `terraform/<p>/main.tf`;
- kierunek odwrotny: klucz w `vms` bez hosta w inwentarzu = sierota, ktorej
  zaden playbook nie konwerguje (i ktorej `cluster-*` nie zobacza).

Wyjete: deklaracje platform i klastrow z jawnym `terraform_managed: false`
(bez roota `terraform/<nazwa>/main.tf` — flaga przy istniejacym roocie to
sprzeczna wlasnosc i naruszenie), katalogi bez hostow w tych grupach oraz
szablony `example-cluster` i `platform/example`. Wlasnosc hostow spoza
Terraforma pozostaje po stronie ich dostawcy (runbook machines-from-elsewhere).

Uzycie: probe-inventory-tf-consistency.py [katalog-repo]
(brak argumentu = repo, z ktorego wywolano; argument pozwala falsyfikowac
sonde na kopii drzewa — ta sama zasada co w probe-proxysql-tenancy).
"""

import re
import sys
from pathlib import Path

import yaml

TENANT_GROUPS = ("galera", "restore")
PLATFORM_GROUPS = ("proxysql", "app", "infra")
TEMPLATE_DIRS = {"example-cluster", "example"}

VMS_OPEN = re.compile(r"^\s+vms\s*=\s*\{\s*$")
VMS_CLOSE = re.compile(r"^\s*\}\s*$")
VMS_KEY = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*\{")


def parse_vms_map(main_tf: Path) -> set[str]:
    """Klucze mapy `vms` z pliku roota.

    Blok `vms = { ... }` parzymy wiersz po wierszu (konwencja floty: jeden
    klucz na wiersz), nie calym plikiem regexem — ten drugi lapalby rowniez
    mapy z `modules/`, ktore nie sa zbiorami VM zadnego roota.
    """
    keys: set[str] = set()
    inside = False
    for line in main_tf.read_text(encoding="utf-8").splitlines():
        if not inside:
            inside = bool(VMS_OPEN.match(line))
            continue
        if VMS_CLOSE.match(line):
            break
        match = VMS_KEY.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def inventory_hosts(inventory: Path, groups: tuple[str, ...]) -> dict[str, str]:
    """hostname -> ansible_host dla zadanych grup (nieobecna/pusta grupa = brak)."""
    data = yaml.safe_load(inventory.read_text(encoding="utf-8")) or {}
    children = (data.get("all") or {}).get("children") or {}
    hosts: dict[str, str] = {}
    for group in groups:
        members = (children.get(group) or {}).get("hosts") or {}
        for name, fields in members.items():
            hosts[name] = str((fields or {}).get("ansible_host", "?"))
    return hosts


def check_definition(root: Path, kind: str, def_dir: Path, violations: list[str]) -> None:
    groups = TENANT_GROUPS if kind == "najemca" else PLATFORM_GROUPS
    hosts = inventory_hosts(def_dir / "inventory.yml", groups)
    if not hosts:
        return

    rel_def = def_dir.relative_to(root)
    tf_main = root / "terraform" / def_dir.name / "main.tf"
    tf_rel = tf_main.relative_to(root)

    # Brak pola zachowuje wymog roota; tylko jawne false oznacza hosty
    # dostarczone poza Terraformem. Wlasnosc czytamy z definicji tej warstwy.
    config_name = "cluster.yml" if kind == "najemca" else "platform.yml"
    config_path = def_dir / config_name
    config = {}
    if config_path.is_file():
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if config.get("terraform_managed") is False:
        # Cele infra-*, galera-rebuild i provisioning nie czytaja flagi i dalej
        # dzialaja na istniejacym roocie. Wyjecie zostawiloby jego mape `vms`
        # bez kontroli, wiec flaga przy istniejacym roocie jest naruszeniem.
        if tf_main.is_file():
            violations.append(
                f"{rel_def}/{config_name}: terraform_managed: false, ale istnieje "
                f"{tf_rel} — cele infra-* i galera-rebuild nadal uzywaja tego "
                f"roota bez kontroli jego mapy `vms`; usun root albo ustaw "
                f"terraform_managed: true"
            )
        return

    if not tf_main.is_file():
        violations.append(
            f"{rel_def}: grupy {', '.join(groups)} maja hosty "
            f"({', '.join(sorted(hosts))}), ale brak {tf_rel} — infra-* przejda "
            f"cluster_guard i CONFIRM, po czym padna na surowym 'cd', a "
            f"galera-rebuild zbuduje liste wezlow z inwentarza bez szansy wykonania"
        )
        return

    vm_keys = parse_vms_map(tf_main)
    for host in sorted(set(hosts) - vm_keys):
        violations.append(
            f"{tf_rel}: {host} (ansible_host {hosts[host]}, grupa "
            f"{'galera/restore' if kind == 'najemca' else 'platformowa'}) istnieje "
            f"w inwentarzu ({rel_def}/inventory.yml), nie w mapie `vms` — "
            f"galera-rebuild celuje w nieistniejacy destroy-target, a "
            f"infra-teardown zostawia te maszyne i jej sieroty ZFS na zawsze"
        )
    for host in sorted(vm_keys - set(hosts)):
        violations.append(
            f"{rel_def}/inventory.yml: `vms` w {tf_rel} zarzadza {host}, ktorego "
            f"nie ma w grupach {', '.join(groups)} — sierota, ktorej zaden "
            f"playbook nie konwerguje"
        )


def scan(root: Path) -> list[str]:
    violations: list[str] = []
    for kind, subdir, pattern in (
        ("najemca", "clusters", "*/inventory.yml"),
        ("platforma", "platform", "*/inventory.yml"),
    ):
        base = root / subdir
        if not base.is_dir():
            continue
        for inventory in sorted(base.glob(pattern)):
            def_dir = inventory.parent
            if def_dir.name in TEMPLATE_DIRS:
                continue
            check_definition(root, kind, def_dir, violations)
    return violations


def self_test(root: Path) -> int:
    """Falsyfikuj reguly na izolowanych danych, niezaleznie od providera floty."""
    import tempfile

    results = [("czyste drzewo repo przechodzi", not scan(root))]
    with tempfile.TemporaryDirectory() as tmp:
        for kind, subdir, config_name, group, host in (
            ("najemca", "clusters", "cluster.yml", "galera", "db_probe"),
            ("platforma", "platform", "platform.yml", "proxysql", "proxy_probe"),
        ):
            work = Path(tmp) / kind
            name = f"managed-{kind}"
            definition = work / subdir / name
            definition.mkdir(parents=True)
            (definition / config_name).write_text(
                "terraform_managed: true\n", encoding="utf-8"
            )
            inventory = definition / "inventory.yml"
            inventory.write_text(
                yaml.safe_dump(
                    {"all": {"children": {group: {"hosts": {host: {"ansible_host": "172.24.10.11"}}}}}}
                ),
                encoding="utf-8",
            )
            tf_dir = work / "terraform" / name
            tf_dir.mkdir(parents=True)
            main_tf = tf_dir / "main.tf"
            tf_text = f"locals {{\n  vms = {{\n    {host} = {{}}\n  }}\n}}\n"
            main_tf.write_text(tf_text, encoding="utf-8")
            results.append((f"{kind}: zgodne inventory i mapa vms przechodza", not scan(work)))

            main_tf.write_text(
                tf_text.replace(f"    {host} = {{}}\n", ""), encoding="utf-8"
            )
            results.append(
                (
                    f"{kind}: usuniecie hosta z mapy vms zapala FAIL",
                    any(host in violation for violation in scan(work)),
                )
            )

            main_tf.write_text(tf_text, encoding="utf-8")
            inventory_text = inventory.read_text(encoding="utf-8")
            inventory.write_text(
                inventory_text.replace(f"{host}:", f"{host}X:"), encoding="utf-8"
            )
            violations = scan(work)
            results.append(
                (
                    f"{kind}: dryf inventory–Terraform zapala FAIL w obu kierunkach",
                    any(f"{host}X" in violation for violation in violations)
                    and any(host in violation for violation in violations),
                )
            )

            inventory.write_text(inventory_text, encoding="utf-8")
            main_tf.unlink()
            results.append(
                (
                    f"{kind}: brak wymaganego roota Terraform zapala FAIL",
                    any(f"brak terraform/{name}" in violation for violation in scan(work)),
                )
            )

            main_tf.write_text(tf_text, encoding="utf-8")
            (definition / config_name).write_text(
                "terraform_managed: false\n", encoding="utf-8"
            )
            results.append(
                (
                    f"{kind}: terraform_managed: false przy istniejacym roocie zapala FAIL",
                    any("terraform_managed: false" in violation for violation in scan(work)),
                )
            )

    passed = all(ok for _, ok in results)
    for description, ok in results:
        print(f"  {'OK ' if ok else 'ZONK'} {description}")
    print(f"{'PASS' if passed else 'FAIL'}: samo-test bramki inventory<->TF")
    return 0 if passed else 1


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--self-test":
        return self_test(Path.cwd())
    root = Path(argv[0]).resolve() if argv else Path.cwd()
    violations = scan(root)
    if violations:
        for violation in violations:
            print(f"  FAIL {violation}")
        print(
            f"FAIL: inwentarz i terraform rozeszly sie ({len(violations)} "
            f"naruszen) — uzycie i uzasadnienie w naglowku pliku"
        )
        return 1
    print("PASS: kazdy host z grup TF ma swoj klucz w `vms` wlasciwego roota")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
