#!/usr/bin/env python3
"""validate-inventory.py — walidacja invariantów inventory klastra (audit#17).

Sprawdza to, czego schema cluster.yml NIE obejmuje:
  - liczebność grup vs <group>.nodes_expected (gdy podano cluster.yml)
  - unikalność adresów (ansible_host, *_node_address) w obrębie i między grupami
  - galera_node_idx / proxysql_node_idx unikalne i prawidłowe (dokładnie jeden proxysql idx=1)
  - rozłączność grupy restore względem galera/proxysql (ochrona przed restore na żywym klastrze)
  - VIP endpoint nie koliduje z adresem żadnego węzła
  - każdy węzeł galera/proxysql ma swój *_node_address

Użycie:
  validate-inventory.py <inventory.yml> [cluster.yml]
Wyjście: 0 = OK, !=0 = naruszenie (lista wypisana na stderr).
"""
import sys
import yaml


def collect_hosts(node, out):
    """Rekursywnie zbiera {hostname: vars} z drzewa inventory Ansible."""
    if not isinstance(node, dict):
        return
    for h, hv in (node.get("hosts") or {}).items():
        out[h] = hv or {}
    for child in (node.get("children") or {}).values():
        collect_hosts(child, out)


def main():
    if len(sys.argv) < 2:
        print("usage: validate-inventory.py <inventory.yml> [cluster.yml]", file=sys.stderr)
        return 2
    inv_path = sys.argv[1]
    cluster = {}
    if len(sys.argv) >= 3:
        with open(sys.argv[2]) as f:
            cluster = yaml.safe_load(f) or {}

    with open(inv_path) as f:
        inv = yaml.safe_load(f) or {}

    # Zbuduj grupy -> {host: vars}
    groups = {}
    all_root = inv.get("all", inv)
    for gname, gval in (all_root.get("children") or {}).items():
        hosts = {}
        collect_hosts(gval, hosts)
        groups[gname] = hosts
    # hosts定义 bezpośrednio w all (bez children)
    collect_hosts(all_root, groups.setdefault("_root", {}))

    errors = []

    galera = groups.get("galera", {})
    proxysql = groups.get("proxysql", {})
    restore = groups.get("restore", {})

    # 1) liczebność vs nodes_expected
    for gname, hosts in (("galera", galera), ("proxysql", proxysql)):
        expected = (cluster.get(gname) or {}).get("nodes_expected")
        if expected is not None and len(hosts) != int(expected):
            errors.append(
                f"group '{gname}' ma {len(hosts)} hostów, a {gname}.nodes_expected={expected}"
            )

    # 2) galera_node_idx unikalne i ciągłe 1..N
    gidx = sorted(h.get("galera_node_idx") for h in galera.values() if h.get("galera_node_idx") is not None)
    if len(gidx) != len(galera):
        errors.append("nie każdy węzeł galera ma galera_node_idx")
    elif gidx != list(range(1, len(galera) + 1)):
        errors.append(f"galera_node_idx nie są ciągłe 1..N: {gidx}")

    # 3) proxysql_node_idx unikalne, dokładnie jeden idx==1 (MASTER Keepalived)
    pidx = [h.get("proxysql_node_idx") for h in proxysql.values() if h.get("proxysql_node_idx") is not None]
    if len(pidx) != len(proxysql):
        errors.append("nie każdy węzeł proxysql ma proxysql_node_idx")
    elif len(set(pidx)) != len(pidx):
        errors.append(f"duplikat proxysql_node_idx (muszą być unikalne): {pidx}")
    master_count = sum(1 for i in pidx if int(i) == 1)
    if len(proxysql) >= 1 and master_count != 1:
        errors.append(
            f"oczekiwano dokładnie jednego proxysql_node_idx==1 (MASTER), znaleziono {master_count}: {pidx}"
        )

    # 4) rozłączność restore vs galera/proxysql (restore = destrukcyjny!)
    for h in restore:
        if h in galera:
            errors.append(f"host '{h}' jest w grupach restore AND galera — restore zniszczy żywy klaster")
        if h in proxysql:
            errors.append(f"host '{h}' jest w grupach restore AND proxysql")

    # 5) unikalność adresów w obrębie i między grupami (ansible_host + *_node_address)
    addr_owner = {}
    # 5a) tożsamość POŁĄCZENIA = ansible_host + ansible_port.
    # Lab Docker legalnie współdzieli 127.0.0.1 i różni się portem — sam host nie wystarczy.
    conn_owner = {}
    for gname, hosts in groups.items():
        if gname == "_root":
            continue
        for h, hv in hosts.items():
            ah = (hv or {}).get("ansible_host")
            if not ah:
                continue
            conn = "{}:{}".format(ah, (hv or {}).get("ansible_port", 22))
            if conn in conn_owner and conn_owner[conn] != (gname, h):
                errors.append(
                    f"połączenie {conn} współdzielone przez '{h}' [{gname}]"
                    f" i '{conn_owner[conn][1]}' [{conn_owner[conn][0]}]"
                )
            else:
                conn_owner[conn] = (gname, h)

    # 5b) adresy KLASTROWE (*_node_address) muszą być globalnie unikalne
    for gname, hosts in groups.items():
        if gname == "_root":
            continue
        for h, hv in hosts.items():
            for key in ("galera_node_address", "proxysql_node_address",
                        "restore_node_address", "infra_node_address"):
                a = (hv or {}).get(key)
                if not a:
                    continue
                if a in addr_owner and addr_owner[a] != (gname, h):
                    errors.append(
                        f"adres {a} ({key}) użyty przez '{h}' [{gname}] i '{addr_owner[a][1]}' [{addr_owner[a][0]}]"
                    )
                else:
                    addr_owner[a] = (gname, h)

    # 6) VIP endpoint nie koliduje z adresem węzła
    vip = ((cluster.get("proxysql") or {}).get("endpoint") or {}).get("address")
    if vip and vip in addr_owner:
        g, h = addr_owner[vip]
        errors.append(f"VIP endpoint {vip} koliduje z adresem węzła '{h}' [{g}]")

    if errors:
        print(f"INVARIANT FAIL ({inv_path}):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"INVARIANT OK: galera={len(galera)} proxysql={len(proxysql)} restore={len(restore)}"
          f" (adresy unikalne, idx poprawne, restore rozłączne)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
