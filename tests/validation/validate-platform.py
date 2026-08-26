#!/usr/bin/env python3
"""
Walidator warstwy wspolnej — schema platform.yml + invarianty inwentarza platformy.

Warstwa wspolna (para ProxySQL + VIP, host infra, host aplikacyjny) jest
niezalezna od klastrow Galera. Ten walidator pilnuje obie strony tej niezaleznosci:

  1. platform.yml miesci sie w platform/schema/platform.schema.json (m.in. PELNY
     material proxysql.frontend_tls — platforma jest jedynym dostawca tozsamosci
     wspolnego endpointu, bo cluster.schema.json odrzuca cert/klucz u najemcy).
  2. inwentarz platformy NIE zawiera grup galera/restore — ich obecnosc oznaczalaby,
     ze warstwa wspolna wchlonela klaster bazy danych (restore to z definicji host
     destrukcyjny i nie ma czego szukac w warstwie, ktora sluzy calej flocie).
  3. liczba hostow w grupie proxysql == proxysql.nodes_expected (rozjazd oznacza
     ze HA pary zdefiniowane w platform.yml nie pokrywa inwentarza).
  4. adres VIP nie pokrywa sie z adresem ZADNEGO hosta — keepalived z VRRP na
     adresie istniejacego hosta to szamotanina o ruch bez zadnego bledu w ansible.
  5. proxysql_node_idx unikalne z dokladnie jednym idx==1 (MASTER) — dwa Mastery
     to pojedynek o VIP, zero Mastery to VIP, ktory nigdy nie wstaje.

Kontrola lustrzana na zywym hoscie robi tests/lab/probe-platform.py — dublura
offline/runtime jest celowa, nie redundancja do usuniecia.

Uzycie:
  validate-platform.py [platform.yml] [platform.schema.json] [inventory.yml]
  (bez argumentow: szablon platform/example — Makefile ZAWSZE podaje jawne
   sciezki $(PLATFORM_DIR), wiec domyslne sluza tylko odpaleniu recznemu)
Wyjście: 0 = PASS, 1 = FAIL, 2 = błąd użycia.
"""
import json
import sys
from pathlib import Path

import yaml
from jsonschema import validate, ValidationError

DEFAULT_PLATFORM = "platform/example/platform.yml"
DEFAULT_SCHEMA = "platform/schema/platform.schema.json"
DEFAULT_INVENTORY = "platform/example/inventory.yml"

# Adresy klastrowe inwentarza — kazdy z nich jest tozsamosci sieciowa hosta pod
# utrzymaniem warstwy wspolnej, wiec kazdy koliduje z VIP.
NODE_ADDRESS_KEYS = (
    "proxysql_node_address",
    "infra_node_address",
    "app_node_address",
    "galera_node_address",
    "restore_node_address",
)

FORBIDDEN_GROUPS = ("galera", "restore")


def collect_hosts(node, out):
    """Rekursywnie zbiera {hostname: vars} z drzewa inventory Ansible."""
    if not isinstance(node, dict):
        return
    for host, host_vars in (node.get("hosts") or {}).items():
        out[host] = host_vars or {}
    for child in (node.get("children") or {}).values():
        collect_hosts(child, out)


def inventory_groups(inventory):
    """Grupa -> {host: vars}; struktura identyczna jak w validate-inventory.py."""
    groups = {}
    root = inventory.get("all", inventory)
    for group_name, group in (root.get("children") or {}).items():
        hosts = {}
        collect_hosts(group, hosts)
        groups[group_name] = hosts
    # Hosty zdefiniowane bezposrednio w all (bez children) — bez rekursji:
    # collect_hosts(root) wciagnalby wezly grup potomnych i kazdy host stalby
    # sie "dubletem" samego siebie w kontroli unikalnosci adresow.
    direct = {host: hv or {} for host, hv in (root.get("hosts") or {}).items()}
    if direct:
        groups["_root"] = direct
    return groups


def semantic_errors(platform, groups):
    """Kontrole, ktorych JSON Schema nie wyrazi (relacje platform.yml <-> inwentarz)."""
    errors = []

    # Grupy klastrowe w inwentarzu platformy = powrot sprzezenia wlasnosciowego.
    for group in FORBIDDEN_GROUPS:
        if group in groups:
            errors.append(
                f"grupa '{group}' w inwentarzu platformy — warstwa wspolna nie moze "
                f"wladac wezlami klastra Galera; to wlasnie ten blad rozbieralismy "
                f"przy wyciaganiu warstwy z cluster.yml"
            )

    proxysql_hosts = groups.get("proxysql", {})
    expected = (platform.get("proxysql") or {}).get("nodes_expected")
    if expected is not None and len(proxysql_hosts) != int(expected):
        errors.append(
            f"grupa 'proxysql' ma {len(proxysql_hosts)} hostow, a "
            f"proxysql.nodes_expected={expected}"
        )

    # idx: unikalnosc + dokladnie jeden MASTER keepalived (mirror validate-inventory.py).
    idx_values = [
        host_vars.get("proxysql_node_idx")
        for host_vars in proxysql_hosts.values()
        if host_vars.get("proxysql_node_idx") is not None
    ]
    if proxysql_hosts and len(idx_values) != len(proxysql_hosts):
        errors.append("nie kazdy wezel proxysql ma proxysql_node_idx")
    elif len(set(idx_values)) != len(idx_values):
        errors.append(f"duplikat proxysql_node_idx (musza byc unikalne): {idx_values}")
    else:
        masters = sum(1 for idx in idx_values if int(idx) == 1)
        if proxysql_hosts and masters != 1:
            errors.append(
                f"oczekiwano dokladnie jednego proxysql_node_idx==1 (MASTER "
                f"keepalived), znaleziono {masters}: {idx_values}"
            )

    # Unikalnosc adresow: powtorzony ansible_host:port albo *_node_address to
    # ciche rozdwojenie hosta (dwa wezly keepalived na jednym adresie itp.).
    conn_owner = {}
    addr_owner = {}
    for group, hosts in groups.items():
        for host, host_vars in hosts.items():
            ansible_host = host_vars.get("ansible_host")
            if ansible_host:
                conn = f"{ansible_host}:{host_vars.get('ansible_port', 22)}"
                if conn in conn_owner and conn_owner[conn] != (group, host):
                    errors.append(
                        f"polaczenie {conn} wspoldzielone przez '{host}' [{group}] "
                        f"i '{conn_owner[conn][1]}' [{conn_owner[conn][0]}]"
                    )
                else:
                    conn_owner[conn] = (group, host)
            for key in NODE_ADDRESS_KEYS:
                address = host_vars.get(key)
                if not address:
                    continue
                if address in addr_owner and addr_owner[address] != (group, host):
                    errors.append(
                        f"adres {address} ({key}) uzyty przez '{host}' [{group}] "
                        f"i '{addr_owner[address][1]}' [{addr_owner[address][0]}]"
                    )
                else:
                    addr_owner[address] = (group, host)

    vip = ((platform.get("proxysql") or {}).get("endpoint") or {}).get("address")
    if vip and vip in addr_owner:
        group, host = addr_owner[vip]
        errors.append(f"VIP endpoint {vip} koliduje z adresem wezla '{host}' [{group}]")

    return errors


def main():
    if len(sys.argv) > 4:
        print(
            "usage: validate-platform.py [platform.yml] [platform.schema.json] [inventory.yml]",
            file=sys.stderr,
        )
        return 2

    platform_path = Path(sys.argv[1]) if len(sys.argv) >= 2 else Path(DEFAULT_PLATFORM)
    schema_path = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path(DEFAULT_SCHEMA)
    inventory_path = Path(sys.argv[3]) if len(sys.argv) >= 4 else Path(DEFAULT_INVENTORY)

    for label, path in (
        ("platform file", platform_path),
        ("schema file", schema_path),
        ("inventory file", inventory_path),
    ):
        if not path.exists():
            print(f"FAIL: {label} not found: {path}", file=sys.stderr)
            return 1

    with open(schema_path, encoding="utf-8") as handle:
        schema = json.load(handle)
    with open(platform_path, encoding="utf-8") as handle:
        platform = yaml.safe_load(handle) or {}
    with open(inventory_path, encoding="utf-8") as handle:
        inventory = yaml.safe_load(handle) or {}

    try:
        validate(instance=platform, schema=schema)
    except ValidationError as exc:
        print(f"FAIL: {platform_path}")
        print(f"  path: {'/'.join(str(part) for part in exc.absolute_path) or '(root)'}")
        print(f"  error: {exc.message}")
        return 1

    errors = semantic_errors(platform, inventory_groups(inventory))
    if errors:
        print(f"FAIL: {platform_path} / {inventory_path}")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(
        f"PASS: {platform_path} — schema valid, inwentarz platformy zgodny "
        f"(proxysql={len(inventory_groups(inventory).get('proxysql', {}))}, "
        f"bez grup klastrowych, VIP bez kolizji)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
