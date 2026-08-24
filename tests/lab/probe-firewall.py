#!/usr/bin/env python3
"""Verify the role-scoped host and Docker ingress policy end to end."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import sys

import yaml

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/example-cluster/cluster.yml")
INVENTORY_PATH = os.environ.get("CLUSTER_INVENTORY", "clusters/example-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")

with open(CONFIG_PATH, encoding="utf-8") as config_file:
    CONFIG = yaml.safe_load(config_file)
with open(INVENTORY_PATH, encoding="utf-8") as inventory_file:
    INVENTORY = yaml.safe_load(inventory_file)

GROUPS = INVENTORY["all"]["children"]
NETWORK = CONFIG["network"]

# Wlascicielem polityki hosta jest ta warstwa, ktora go tworzy. Definicja
# najemcy deklaruje wspolne fcp1/fcp2/fcinfra/fcapp, zeby sie do nich laczyc —
# sprawdzanie ICH regul przeciw CIDR-om najemcy cementowaloby blad, ktory
# bramka wlasciciela w playbooks/firewall.yml wlasnie zamyka.
TENANT_GROUPS = ("galera", "restore")
SHARED_GROUPS = ("proxysql", "infra", "app")


def owned_groups(config: dict) -> tuple[str, ...]:
    platform = config.get("platform") or {}
    return SHARED_GROUPS if platform.get("name") else TENANT_GROUPS


OWNED_GROUPS = owned_groups(CONFIG)
OWNED_PATTERN = ":".join(OWNED_GROUPS)


def run_command(pattern: str, command: str, timeout: int = 120) -> dict[str, str]:
    result = subprocess.run(
        [
            ANSIBLE,
            pattern,
            "-i",
            INVENTORY_PATH,
            "-m",
            "ansible.builtin.command",
            "-a",
            command,
            "--fork",
            "10",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)

    output: dict[str, str] = {}
    current_host: str | None = None
    current_lines: list[str] = []
    header = re.compile(r"^(\S+)\s+\|\s+(?:CHANGED|SUCCESS)\s+\|\s+rc=\d+\s+>>\s*$")
    for line in result.stdout.splitlines():
        match = header.match(line)
        if match:
            if current_host is not None:
                output[current_host] = "\n".join(current_lines).strip()
            current_host = match.group(1)
            current_lines = []
        elif current_host is not None:
            current_lines.append(line)
    if current_host is not None:
        output[current_host] = "\n".join(current_lines).strip()
    return output


def hosts(group: str) -> set[str]:
    return set(GROUPS.get(group, {}).get("hosts", {}))


def port_rule(cidr: str, port: int, protocol: str = "tcp") -> str:
    return (
        f'rule family="ipv4" source address="{cidr}" '
        f'port port="{port}" protocol="{protocol}" accept'
    )


def protocol_rule(cidr: str, protocol: int) -> str:
    return f'rule family="ipv4" source address="{cidr}" protocol value="{protocol}" accept'


def expected_rules(host: str) -> set[str]:
    rules = {port_rule(cidr, 22) for cidr in NETWORK["administration_cidrs"]}

    if host in hosts("galera"):
        for cidr in NETWORK["database_cluster_cidrs"]:
            rules.update(
                {
                    port_rule(cidr, 3306),
                    port_rule(cidr, 4444),
                    port_rule(cidr, 4567),
                    port_rule(cidr, 4568),
                    port_rule(cidr, 4567, "udp"),
                }
            )
        for cidr in NETWORK["monitoring_cidrs"]:
            rules.update({port_rule(cidr, 3306), port_rule(cidr, 9100)})

    if host in hosts("proxysql"):
        rules.update(port_rule(cidr, 6033) for cidr in NETWORK["application_cidrs"])
        rules.update(port_rule(cidr, 6032) for cidr in NETWORK["administration_cidrs"])
        for cidr in NETWORK["monitoring_cidrs"]:
            rules.update({port_rule(cidr, 6070), port_rule(cidr, 9100)})
        rules.update(protocol_rule(cidr, 112) for cidr in NETWORK["database_cluster_cidrs"])

    if host in hosts("infra"):
        for cidr in NETWORK["administration_cidrs"]:
            rules.update(
                {
                    port_rule(cidr, 80),
                    port_rule(cidr, 443),
                    port_rule(cidr, 9001),
                    port_rule(cidr, 8025),
                }
            )
        rules.update(port_rule(cidr, 443) for cidr in NETWORK["monitoring_cidrs"])
        rules.update(port_rule(cidr, 9000) for cidr in NETWORK["database_cluster_cidrs"])

    return rules


def field(body: str, name: str) -> str:
    match = re.search(rf"^[ \t]*{re.escape(name)}:[ \t]*(.*)$", body, re.MULTILINE)
    return match.group(1).strip() if match else "<missing>"


def source_address(target: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((target, 22))
        return probe.getsockname()[0]


def in_cidrs(address: str, cidrs: list[str]) -> bool:
    ip = ipaddress.ip_address(address)
    return any(ip in ipaddress.ip_network(cidr) for cidr in cidrs)


def reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def main() -> int:
    failures: list[str] = []
    owned_hosts = set().union(*(hosts(group) for group in OWNED_GROUPS))
    inventory_hosts = {
        host: values.get("ansible_host", host)
        for group in GROUPS.values()
        for host, values in group.get("hosts", {}).items()
    }

    policies = run_command(OWNED_PATTERN, "firewall-cmd --list-all")
    enabled = run_command(OWNED_PATTERN, "systemctl is-enabled firewalld")
    active_zones = run_command(OWNED_PATTERN, "firewall-cmd --get-active-zones")
    check(
        set(policies) == owned_hosts,
        "not every owned host returned firewalld policy",
        failures,
    )

    for host in sorted(owned_hosts):
        body = policies.get(host, "")
        actual_rules = set(re.findall(r'^\s*(rule family="ipv4".*)$', body, re.MULTILINE))
        expected = expected_rules(host)
        # Naglowek to "public (active)" albo "public (default, active)" — zaleznie od tego,
        # czy public jest takze strefa domyslna. Sprawdzamy sam fakt aktywnosci.
        check(
            re.search(r"^public \([^)]*active[^)]*\)", body, re.MULTILINE) is not None,
            f"{host}: public zone is not active",
            failures,
        )
        check(enabled.get(host, "").strip() == "enabled", f"{host}: firewalld not enabled", failures)
        check(field(body, "target") == "default", f"{host}: public zone target is not default", failures)
        check(field(body, "ports") == "", f"{host}: unconditional ports remain: {field(body, 'ports')}", failures)
        check(field(body, "sources") == "", f"{host}: source-bound zones remain: {field(body, 'sources')}", failures)
        check(field(body, "services") == "dhcpv6-client", f"{host}: unexpected services: {field(body, 'services')}", failures)
        check(actual_rules == expected, f"{host}: rich-rule diff actual={sorted(actual_rules)} expected={sorted(expected)}", failures)
        check(
            not re.search(r"(?m)^\s*sources:\s+\S", active_zones.get(host, "")),
            f"{host}: active source-bound zone can bypass public policy",
            failures,
        )

    first_galera = next(iter(GROUPS["galera"]["hosts"].values()))["ansible_host"]
    first_proxy = next(iter(GROUPS["proxysql"]["hosts"].values()))["ansible_host"]
    infra_host = next(iter(GROUPS["infra"]["hosts"].values()))["ansible_host"]
    controller_ip = source_address(first_galera)

    check(reachable(first_galera, 22), "controller cannot reach SSH after policy", failures)
    check(reachable(first_galera, 3306), "controller cannot reach allowed Galera client port", failures)
    check(reachable(first_proxy, 6033), "controller cannot reach allowed ProxySQL client port", failures)
    check(reachable(infra_host, 443), "controller cannot reach allowed PMM port", failures)
    check(not reachable(first_galera, 111), "unexpected rpcbind port 111 reachable", failures)
    check(not reachable(first_proxy, 6132), "unexpected ProxySQL TLS admin port 6132 reachable", failures)
    check(not reachable(first_proxy, 6133), "unexpected ProxySQL TLS client port 6133 reachable", failures)

    if not in_cidrs(controller_ip, NETWORK["monitoring_cidrs"]):
        check(not reachable(first_galera, 9100), "node_exporter reachable outside monitoring CIDRs", failures)
        check(not reachable(first_proxy, 6070), "ProxySQL metrics reachable outside monitoring CIDRs", failures)

    # Filtr ingress Dockera opiera sie na module xt_conntrack (match --ctorigdst).
    # Kernele bez modulow xtables (np. Rocky 10 / 6.12) nie moga go zrealizowac —
    # wtedy zamiast lawiny krypticznych bledow iptables raportujemy jedna,
    # jednoznaczna porazke z konsekwencja bezpieczenstwa.
    xtables_probe = run_command(
        "infra", "find /lib/modules -name xt_conntrack.ko* -print -quit"
    )
    xtables_missing = [
        host for host in hosts("infra") if not xtables_probe.get(host, "").strip()
    ]
    for host in xtables_missing:
        check(
            False,
            f"{host}: ISC-5 NIESPELNIONE — kernel bez modulow xtables (xt_conntrack), "
            "wiec filtr ingress Dockera (ISA-INFRA) nie moze powstac. Porty publikowane "
            "przez Dockera NIE sa ograniczone do skonfigurowanych CIDR: strefa 'docker' "
            "ma target=ACCEPT i omija rich rules strefy 'public'. "
            "Wymaga implementacji filtra natywnie w nftables.",
            failures,
        )
    # Zapytania o chain padaja na hostach bez xtables (chain nie istnieje), wiec
    # odpytujemy wylacznie hosty, na ktorych filtr moze w ogole dzialac.
    if xtables_missing:
        return failures

    docker_chain = run_command("infra", "iptables -S ISA-INFRA")
    docker_hook = run_command("infra", "iptables -S DOCKER-USER")
    docker_filter = run_command("infra", "iptables -S")
    docker_firewall_enabled = run_command("infra", "systemctl is-enabled isa-docker-firewall.service")
    docker_dependencies = run_command(
        "infra", "systemctl show docker.service --property=Requires --property=After"
    )
    listeners = run_command("infra", "ss -ltnH")
    for host in hosts("infra"):
        chain = docker_chain.get(host, "")
        hook = docker_hook.get(host, "")
        all_filter_rules = docker_filter.get(host, "")
        bound = listeners.get(host, "")
        hook_rules = [line for line in hook.splitlines() if line.startswith("-A DOCKER-USER ")]
        managed_hooks = [line for line in hook_rules if "-j ISA-INFRA" in line]
        check(
            len(managed_hooks) == 1
            and hook_rules
            and hook_rules[0] == managed_hooks[0]
            and f"--ctorigdst {inventory_hosts[host]}" in managed_hooks[0],
            f"{host}: ISA-INFRA must be the single address-scoped rule at DOCKER-USER head",
            failures,
        )
        check(
            not any(
                line.startswith("-N ISA-INFRA-")
                for line in all_filter_rules.splitlines()
            ),
            f"{host}: stale Docker firewall generation remains",
            failures,
        )
        check(
            all(
                "--ctorigsrc " in line
                for line in chain.splitlines()
                if "--ctstate ESTABLISHED" in line
            ),
            f"{host}: established-session rule is not scoped to an allowed original source",
            failures,
        )
        check(
            chain.splitlines()[-1:] == ["-A ISA-INFRA -j DROP"],
            f"{host}: Docker chain has no final fail-closed rule",
            failures,
        )
        check(
            docker_firewall_enabled.get(host, "").strip() == "enabled",
            f"{host}: isa-docker-firewall.service is not enabled",
            failures,
        )
        dependencies = docker_dependencies.get(host, "")
        check(
            "isa-docker-firewall.service" in dependencies,
            f"{host}: docker.service does not require/order after the ingress filter",
            failures,
        )
        for port in (80, 443, 9000, 9001, 8025):
            check(f"--ctorigdstport {port} -j DROP" in chain, f"{host}: Docker port {port} has no deny fallback", failures)
            check(f"{inventory_hosts[host]}:{port}" in bound, f"{host}: Docker port {port} not bound to inventory address", failures)
        check("0.0.0.0:443" not in bound and "[::]:443" not in bound, f"{host}: PMM published on a wildcard address", failures)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print(
        f"PASS: firewalld exact role policy on {len(owned_hosts)} owned hosts "
        f"({OWNED_PATTERN}); unexpected listeners blocked; "
        "Docker ingress filter and address binding verified"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
