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

Wyjete: katalogi bez hostow w tych grupach oraz szablony
(`example-cluster`, `platform/example`) — ich maszyny moga pochodzic zewsad
(BYO-hosts to sciezka dokumentowana w README), a adresy 10.0.x sa fikcyjne
celowo.

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

    # Maszyny z poza Terraformu (machines-from-elsewhere): cluster.yml z
    # `terraform_managed: false` to jawne, kontrolowane wyjecie. Definicja bez
    # tego pola nadal wymaga roota — wyjecie nie moze powstac przez przypadek.
    cluster_cfg_path = def_dir / "cluster.yml"
    if cluster_cfg_path.is_file():
        cluster_cfg = yaml.safe_load(cluster_cfg_path.read_text(encoding="utf-8")) or {}
        if cluster_cfg.get("terraform_managed") is False:
            return

    rel_def = def_dir.relative_to(root)
    tf_main = root / "terraform" / def_dir.name / "main.tf"
    tf_rel = tf_main.relative_to(root)

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
    """Falsyfikacja wlasnych regul na kopii drzewa — nigdy na plikach repo."""
    import shutil
    import tempfile

    results = []
    ignore = shutil.ignore_patterns(".terraform", "*.tfstate", "*.tfstate.backup")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for part in ("clusters", "platform", "terraform"):
            if (root / part).is_dir():
                shutil.copytree(root / part, work / part, ignore=ignore)

        results.append(("czyste drzewo repo przechodzi", not scan(work)))

        # dryf w strone "inwentarz > TF": usun klucz z vms
        orion = work / "terraform" / "orionv8-r9" / "main.tf"
        text = orion.read_text(encoding="utf-8")
        orion.write_text(
            "\n".join(
                line for line in text.splitlines() if "o8r1" not in line
            ) + "\n",
            encoding="utf-8",
        )
        violations = scan(work)
        results.append(
            (
                "usuniecie o8r1 z mapy vms zapala FAIL",
                any("o8r1" in v for v in violations),
            )
        )

        # dryf w druga strone: TF > inwentarz
        orion.write_text(text, encoding="utf-8")
        inv = work / "clusters" / "orionv8-r9" / "inventory.yml"
        inv_text = inv.read_text(encoding="utf-8")
        inv.write_text(
            inv_text.replace('        o8r1:\n', '        o8rX:\n'),
            encoding="utf-8",
        )
        violations = scan(work)
        results.append(
            (
                "rebrand hosta w inwentarzu bez TF zapala FAIL w obu kierunkach",
                any("o8rX" in v for v in violations) and any("o8r1" in v for v in violations),
            )
        )

        # brak roota TF przy zywych hostach
        inv.write_text(inv_text, encoding="utf-8")
        shutil.rmtree(work / "terraform" / "cassiopeiav8-r9")
        violations = scan(work)
        results.append(
            (
                "najemca bez roota TF zapala FAIL",
                any("brak terraform/cassiopeiav8-r9" in v for v in violations),
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
