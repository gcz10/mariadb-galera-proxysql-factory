#!/usr/bin/env python3
"""Adresy wezlow klastra nie moga kolidowac z infrastruktura ani z innym klastrem.

POWSTALA PO REALNEJ AWARII (2026-08-16). Klastrowi `newclaude5-r9` przypisano
blok .180-.183, w ktorym `.181` jest adresem ZARZADZANIA hypervisora Proxmox
(`PROXMOX_VE_ENDPOINT`). Skutek nie wygladal jak kolizja adresow:

  * `terraform apply` przerwal sie w polowie z "failed to perform HTTP POST
    request" — bo wywolania API do hypervisora konkurowaly w ARP z wlasnie
    tworzona maszyna,
  * `ssh-keyscan` zapisywal klucz Debiana (hypervisor), a nie Rocky (VM),
  * `ansible` raportowal UNREACHABLE i "REMOTE HOST IDENTIFICATION HAS CHANGED".

Adres byl "wolny" wedlug konfiguracji Proxmoxa, bo hypervisor NIE jest maszyna
wirtualna i nie wystepuje w `nodes/pve/qemu`. Weryfikacja przez odpytanie listy
VM jest wiec strukturalnie niewystarczajaca.

Sprawdzane niezmienniki:

  1. Zaden adres wezla nie moze byc rowny adresowi hypervisora
     (host z `PROXMOX_VE_ENDPOINT`). Egzekwowane, gdy zmienna jest w srodowisku.

  2. Dwa rozne klastry nie moga przypisac tego samego adresu wezlowi WLASNEMU
     (grupy `galera`, `restore`). Grupy wspoldzielone (`proxysql`, `infra`)
     sa celowo identyczne miedzy klastrami — nie sa porownywane.

  3. Adres wezla nie moze byc rowny VIP-owi wlasnego klastra
     (`proxysql.endpoint.address`) — VIP podnosi Keepalived na wezlach ProxySQL.

  4. Adres wezla nie moze nalezec do `clusters/reserved-addresses.yml` — rejestru
     maszyn ZYWYCH, ale nie nalezacych do repo. Punkt 1 zamykal wylacznie
     przypadek hypervisora; zwykly cudzy host w tej samej sieci byl dla sondy
     niewidzialny, choc kolizja z nim wyglada IDENTYCZNIE (zerwane ARP,
     `ssh-keyscan` z cudzym kluczem, UNREACHABLE). Rejestr powstal, gdy skan pod
     newclaude11-r9 wykryl zywy `.189`, o ktorym nie wiedzial nikt.

PASS: brak kolizji w sprawdzonych wymiarach (raportuje, ktore sprawdzenia byly aktywne).
FAIL: kolizja adresu z hypervisorem, innym klastrem, wlasnym VIP-em albo rejestrem.
"""

import glob
import ipaddress
import os
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import yaml

# Grupy, ktorych maszyny NALEZA do klastra. Reszta (proxysql, infra) to warstwa
# wspoldzielona — te same adresy w wielu inwentarzach sa poprawne z definicji.
OWNED_GROUPS = ("galera", "restore")


def load_yaml(path):
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def owned_hosts(inventory):
    """Zwraca {nazwa_hosta: adres} dla grup nalezacych do klastra."""
    out = {}
    children = (inventory.get("all") or {}).get("children") or {}
    for group in OWNED_GROUPS:
        hosts = (children.get(group) or {}).get("hosts") or {}
        for host, attrs in hosts.items():
            addr = (attrs or {}).get("ansible_host")
            if addr:
                out[host] = str(addr)
    return out


def hypervisor_address():
    endpoint = os.environ.get("PROXMOX_VE_ENDPOINT", "").strip()
    if not endpoint:
        return None
    return urlparse(endpoint).hostname

RESERVED_PATH = "clusters/reserved-addresses.yml"


def reserved_addresses():
    """{adres: powod} z rejestru, z rozwinieciem blokow CIDR.

    Brak pliku jest BLEDEM, nie cichym pominieciem: sonda bez rejestru
    przepuszcza dokladnie te klase kolizji, dla ktorej rejestr powstal.
    """
    if not Path(RESERVED_PATH).exists():
        return None
    data = load_yaml(RESERVED_PATH)
    out = {}
    for item in data.get("reserved") or []:
        addr = str(item.get("address", "")).strip()
        if addr:
            out[addr] = " ".join(str(item.get("reason", "")).split())
    for item in data.get("reserved_ranges") or []:
        cidr = str(item.get("cidr", "")).strip()
        if not cidr:
            continue
        reason = " ".join(str(item.get("reason", "")).split())
        for ip in ipaddress.ip_network(cidr, strict=False):
            out.setdefault(str(ip), f"blok {cidr}: {reason}")
    return out


def main():
    errors = []
    clusters = {}

    for cfg_path in sorted(glob.glob("clusters/*/cluster.yml")):
        inv_path = Path(cfg_path).with_name("inventory.yml")
        if not inv_path.exists():
            continue
        try:
            cfg = load_yaml(cfg_path)
            inv = load_yaml(inv_path)
        except (OSError, yaml.YAMLError) as exc:
            print(f"FAIL: {cfg_path}: nie da sie wczytac ({exc})")
            return 1
        name = ((cfg.get("cluster") or {}).get("name")) or Path(cfg_path).parent.name
        clusters[name] = {
            "hosts": owned_hosts(inv),
            "vip": ((cfg.get("proxysql") or {}).get("endpoint") or {}).get("address"),
        }

    if not clusters:
        print("FAIL: nie znaleziono zadnego klastra w clusters/*/cluster.yml")
        return 1

    # --- 1. Kolizja z adresem zarzadzania hypervisora ---
    hyp = hypervisor_address()
    for name, data in sorted(clusters.items()):
        for host, addr in sorted(data["hosts"].items()):
            if hyp and addr == hyp:
                errors.append(
                    f"{name}: wezel {host} ma adres hypervisora Proxmox {addr} "
                    f"(PROXMOX_VE_ENDPOINT) — konflikt ARP zrywa API w trakcie apply"
                )

    # --- 2. Kolizja adresu wezla miedzy klastrami ---
    by_addr = defaultdict(list)
    for name, data in clusters.items():
        for host, addr in data["hosts"].items():
            by_addr[addr].append(f"{name}/{host}")
    for addr, owners in sorted(by_addr.items()):
        if len(owners) > 1:
            errors.append(f"adres {addr} przypisany wielu wezlom: {', '.join(sorted(owners))}")

    # --- 3. Kolizja adresu wezla z wlasnym VIP-em ---
    for name, data in sorted(clusters.items()):
        vip = data["vip"]
        if not vip:
            continue
        for host, addr in sorted(data["hosts"].items()):
            if addr == str(vip):
                errors.append(f"{name}: wezel {host} ma adres VIP-a {addr} klastra")

    # --- 4. Kolizja z rejestrem adresow zajetych POZA repozytorium ---
    reserved = reserved_addresses()
    if reserved is None:
        print(f"FAIL: brak {RESERVED_PATH} — sonda bez rejestru nie wykryje kolizji "
              f"z zywym hostem spoza repo, czyli tej klasy bledu, dla ktorej powstala")
        return 1
    for name, data in sorted(clusters.items()):
        for host, addr in sorted(data["hosts"].items()):
            if addr in reserved:
                errors.append(
                    f"{name}: wezel {host} ma adres {addr} zarezerwowany w "
                    f"{RESERVED_PATH} — {reserved[addr]}"
                )

    if errors:
        print(f"FAIL: znaleziono {len(errors)} kolizji adresow:")
        for err in errors:
            print(f"  - {err}")
        return 1

    checked = ["kolizje miedzy klastrami", "kolizje z VIP",
               f"rejestr {len(reserved)} adresow zajetych"]
    if hyp:
        checked.insert(0, f"kolizja z hypervisorem ({hyp})")
    else:
        print("UWAGA: PROXMOX_VE_ENDPOINT nie ustawione — kolizja z hypervisorem NIE sprawdzona")
    total = sum(len(d["hosts"]) for d in clusters.values())
    print(f"PASS: {total} adresow wezlow w {len(clusters)} klastrach bez kolizji ({'; '.join(checked)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
