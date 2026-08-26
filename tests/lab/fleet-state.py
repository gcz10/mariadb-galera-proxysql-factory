#!/usr/bin/env python3
"""Stan floty odczytany z rzeczywistosci, nie przepisany do pliku.

DLACZEGO KOMENDA, A NIE DOKUMENT: `docs/infrastructure-state.md` ogłaszał jako
aktywny stack, ktorego maszyn nie bylo juz od dwoch dni, i wyliczal najemcow
skasowanych tydzien wczesniej. Kazde recznie wpisane zdjecie floty gnije w
godziny, bo w tym repo klastry powstaja i znikaja w kazdej sesji. Dowod skali:
w chwili pisania repo mialo 17 definicji klastrow, z czego zyly dwie.

PODZIAL ZRODEL PRAWDY, ktorego ten skrypt pilnuje w praktyce:
  * zamiar        -> `clusters/<nazwa>/` i `platform/<nazwa>/` (walidowane schematem),
  * rzeczywistosc -> hypervisor (tutaj),
  * historia      -> `docs/records/<data>-*.md` (zamrozone, nigdy nie aktualizowane).

ZLACZENIE IDZIE PO NAZWIE MASZYNY, nie po adresie: inwentarz najemcy nazywa
hosta `l6g1` i Proxmox trzyma te sama nazwe przy VMID. Adres jest zlym kluczem,
bo zwolniony adres bywa nadany kolejnej maszynie, wiec martwa definicja
"rozpoznalaby sie" w cudzej VM.

Skrypt niczego nie zapisuje i niczego nie zmienia. Nie jest bramka: brak
maszyn dla definicji to normalny stan archiwum, nie blad. Konczy sie kodem 2
tylko wtedy, gdy nie da sie ZMIERZYC (brak poswiadczen, hypervisor nieosiagalny)
- nigdy cicho.

Wymaga PROXMOX_VE_ENDPOINT i PROXMOX_VE_API_TOKEN. Pula wlasnosci: FLEET_POOL
(domyslnie `claude-isa`).
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
POOL = os.environ.get("FLEET_POOL", "claude-isa")
EXIT_OK = 0
EXIT_UNDETERMINED = 2

# Nazwy szablonow. Nie sa instancjami, wiec nie opisuja zadnej floty.
TEMPLATES = {"example-cluster", "example", "schema"}


def api(path: str):
    endpoint = os.environ.get("PROXMOX_VE_ENDPOINT", "").strip().rstrip("/")
    token = os.environ.get("PROXMOX_VE_API_TOKEN", "").strip()
    if not endpoint or not token:
        raise RuntimeError(
            "brak PROXMOX_VE_ENDPOINT lub PROXMOX_VE_API_TOKEN — "
            "stanu floty nie da sie zmierzyc"
        )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        f"{endpoint}/api2/json{path}",
        headers={"Authorization": f"PVEAPIToken={token}"},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        return json.load(resp)["data"]


def load_yaml(path: Path):
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}


# Najemca WSPOLDZIELI warstwe: jego inwentarz wymienia `hp1`/`happ`/`hmon`,
# ktorych nie jest wlascicielem. Liczenie ich jako swoich maszyn dawalo dwa
# falszywe odczyty naraz: zywy najemca raportowal 7/7 zamiast 3/3, a definicja
# `newclaude16-r9` — ktorej wezly Galery dawno nie istnieja — wychodzila
# "zatrzymana", bo rozpoznawala sie w cudzych, wciaz obecnych maszynach
# warstwy. Dlatego liczymy WYLACZNIE grupy, ktorych definicja jest wlascicielem.
OWNED_GROUPS = {
    "klaster": ("galera", "restore"),
    "platforma": ("proxysql", "app", "infra"),
}


def inventory_hosts(path: Path, kind: str) -> list[str]:
    """Maszyny NALEZACE do definicji — bez hostow cudzej warstwy wspolnej."""
    data = load_yaml(path)
    hosts: list[str] = []
    children = (data.get("all") or {}).get("children") or {}
    for group_name in OWNED_GROUPS[kind]:
        for host in ((children.get(group_name) or {}).get("hosts") or {}):
            if host not in hosts:
                hosts.append(host)
    return hosts


def definitions() -> list[dict]:
    """Kazda definicja w repo: skad pochodzi, jakie maszyny deklaruje, jaki endpoint."""
    found = []
    for kind, root, config_name in (
        ("platforma", REPO_ROOT / "platform", "platform.yml"),
        ("klaster", REPO_ROOT / "clusters", "cluster.yml"),
    ):
        if not root.is_dir():
            continue
        for entry in sorted(p for p in root.iterdir() if p.is_dir()):
            if entry.name in TEMPLATES:
                continue
            config = load_yaml(entry / config_name)
            if not config:
                continue
            proxysql = config.get("proxysql") or {}
            endpoint = proxysql.get("endpoint") or {}
            base = proxysql.get("hostgroup_base")
            found.append(
                {
                    "kind": kind,
                    "name": entry.name,
                    "hosts": inventory_hosts(entry / "inventory.yml", kind),
                    "vip": endpoint.get("address"),
                    "port": endpoint.get("port"),
                    # Kolejnosc jest kontraktem, nie konwencja: writer/backup/
                    # reader/offline = base, +10, +20, +30 (probe-proxysql-tenancy.py).
                    # Drukujemy ja z legenda, bo cztery gole liczby czyta sie
                    # opacznie, a oba ID realnie istnieja — pomylka writera
                    # z backupem nie rzucilaby sie w oczy w samym raporcie.
                    "hostgroups": (
                        "/".join(str(base + n) for n in (0, 10, 20, 30))
                        if isinstance(base, int)
                        else None
                    ),
                    "app_user": proxysql.get("app_user"),
                }
            )
    return found


def endpoint_reachable(address: str, port: int) -> bool:
    try:
        with socket.create_connection((address, int(port)), timeout=3):
            return True
    except OSError:
        return False


def main() -> int:
    try:
        resources = api("/cluster/resources?type=vm")
    except (RuntimeError, urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print(f"UNDETERMINED: {exc}")
        return EXIT_UNDETERMINED

    live = {
        str(vm.get("name")): {
            "vmid": vm.get("vmid"),
            "status": vm.get("status"),
            "node": vm.get("node"),
        }
        for vm in resources
        if vm.get("pool") == POOL
    }

    defs = definitions()
    claimed: set[str] = set()

    print(f"# maszyny w puli `{POOL}` ({len(live)})")
    owner_of = {}
    for d in defs:
        for host in d["hosts"]:
            owner_of.setdefault(host, f"{d['kind']}/{d['name']}")
    for name, vm in sorted(live.items(), key=lambda kv: kv[1]["vmid"]):
        print(
            f"  {vm['vmid']:<6} {name:<10} {vm['status']:<9} "
            f"{owner_of.get(name, '— poza definicjami w repo')}"
        )

    print()
    print(f"# definicje w repo ({len(defs)})  —  hg: writer/backup/reader/offline")
    for d in defs:
        present = [h for h in d["hosts"] if h in live]
        running = [h for h in present if live[h]["status"] == "running"]
        claimed.update(present)
        if not d["hosts"]:
            state = "BEZ INWENTARZA"
        elif not present:
            state = "ARCHIWUM     "
        elif len(running) == len(d["hosts"]):
            state = "ZYWA         "
        elif running:
            state = "CZESCIOWA    "
        else:
            state = "ZATRZYMANA   "
        detail = f"{len(running)}/{len(d['hosts'])} running"
        extra = ""
        if d["vip"]:
            extra = f"  endpoint {d['vip']}:{d['port']}"
        if d["hostgroups"]:
            extra += f"  hg {d['hostgroups']}"
        if d["app_user"]:
            extra += f"  user {d['app_user']}"
        print(f"  {state} {d['kind']:<9} {d['name']:<18} {detail:<14}{extra}")

    orphans = sorted(set(live) - claimed)
    if orphans:
        print()
        print("# maszyny w puli bez definicji w repo")
        for name in orphans:
            print(f"  {live[name]['vmid']:<6} {name}")

    endpoints: dict[tuple, list[str]] = {}
    for d in defs:
        if d["vip"] and any(h in live for h in d["hosts"]):
            endpoints.setdefault((d["vip"], d["port"]), []).append(d["name"])
    if endpoints:
        print()
        print("# wspolne endpointy zywych definicji")
        for (address, port), users in sorted(endpoints.items()):
            reach = "odpowiada" if endpoint_reachable(address, port) else "NIE odpowiada"
            print(f"  {address}:{port}  {reach}  <- {', '.join(sorted(users))}")

    print()
    print(
        f"OK: zmierzone na zywo — {sum(1 for v in live.values() if v['status'] == 'running')} "
        f"maszyn running, {len(defs)} definicji w repo"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
