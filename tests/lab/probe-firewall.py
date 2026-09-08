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
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
with open(os.path.join(REPO_ROOT, "playbooks", "vars", "infra_ingress.yml"), encoding="utf-8") as ingress_file:
    INGRESS = yaml.safe_load(ingress_file)["infra_ingress_tcp"]

# Wlascicielem polityki hosta jest ta warstwa, ktora go tworzy. Definicja
# najemcy deklaruje wspolne hosty warstwy (grupy proxysql/infra/app), zeby sie
# sprawdzanie ICH regul przeciw CIDR-om najemcy cementowaloby blad, ktory
# bramka wlasciciela w playbooks/firewall.yml wlasnie zamyka.
TENANT_GROUPS = ("galera", "restore")
SHARED_GROUPS = ("proxysql", "infra", "app")


def owned_groups(config: dict) -> tuple[str, ...]:
    platform = config.get("platform") or {}
    return SHARED_GROUPS if platform.get("name") else TENANT_GROUPS


OWNED_GROUPS = owned_groups(CONFIG)
OWNED_PATTERN = ":".join(OWNED_GROUPS)


# Hosty, ktorych nie udalo sie odpytac. Modul zbiera je tutaj, a `main()`
# dopisuje do `failures` razem z reszta wynikow.
COMMAND_FAILURES: list[str] = []


def run_command(pattern: str, command: str, timeout: int = 120) -> dict[str, str]:
    """Uruchom polecenie na hostach i zwroc wyjscie per host.

    NIE PRZERYWA PRZEBIEGU na niezerowym rc. Wczesniej kazdy niezerowy rc
    `ansible` konczyl sie `RuntimeError`, czyli sonda calej floty umierala na
    pierwszym potknieciu — a dwa najczestsze potkniecia to normalne wyniki
    pomiaru, nie awaria narzedzia:
      * `systemctl is-enabled firewalld` zwraca rc=1, gdy firewalld jest
        WYLACZONY. To jest dokladnie defekt, ktory ta sonda ma raportowac,
        a zamiast tego wywracala sie z komunikatem narzedziowym.
      * jeden nieosiagalny host kasowal wyniki wszystkich pozostalych.
    Fail-closed nie znaczy tu "rzuc wyjatkiem", tylko "zapisz porazke i policz
    ja na koncu" — wzorzec `check()` uzywany w calej reszcie tej sondy.
    """
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

    failures_before = len(COMMAND_FAILURES)
    output: dict[str, str] = {}
    current_host: str | None = None
    current_lines: list[str] = []
    # FAILED niesie wyjscie polecenia tak samo jak SUCCESS — dla `is-enabled`
    # to wlasnie wynik pomiaru ("disabled"), wiec musimy je sparsowac, nie
    # odrzucic. UNREACHABLE wyjscia nie ma: to porazka lacznosci.
    header = re.compile(r"^(\S+)\s+\|\s+(?:CHANGED|SUCCESS|FAILED)\s+\|\s+rc=-?\d+\s+>>\s*$")
    failed = re.compile(r"^(\S+)\s+\|\s+(UNREACHABLE|FAILED)!")
    for line in result.stdout.splitlines():
        match = header.match(line)
        if match:
            if current_host is not None:
                output[current_host] = "\n".join(current_lines).strip()
            current_host = match.group(1)
            current_lines = []
            continue
        gone = failed.match(line)
        if gone:
            if current_host is not None:
                output[current_host] = "\n".join(current_lines).strip()
                current_host = None
                current_lines = []
            reason = "nieosiagalny" if gone.group(2) == "UNREACHABLE" else "blad modulu"
            COMMAND_FAILURES.append(f"{gone.group(1)}: {reason} przy '{command}'")
            continue
        if current_host is not None:
            current_lines.append(line)
    if current_host is not None:
        output[current_host] = "\n".join(current_lines).strip()

    # Niezerowy rc bez ani jednego rozpoznanego hosta to awaria samego wywolania
    # (zly wzorzec, brak inwentarza, blad skladni) — wtedy nie ma czego mierzyc.
    # Licznik jest LOKALNY dla wywolania: gdyby patrzyl na pustke calej listy,
    # pierwszy nieosiagalny host wyciszylby te diagnostyke do konca przebiegu.
    if result.returncode != 0 and not output and len(COMMAND_FAILURES) == failures_before:
        COMMAND_FAILURES.append(
            f"'{command}' nie zwrocilo wyniku dla zadnego hosta wzorca '{pattern}': "
            f"{(result.stderr or result.stdout).strip()[:200]}"
        )
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
            rules.update(port_rule(cidr, port) for port in INGRESS["administration"])
        rules.update(
            port_rule(cidr, port)
            for cidr in NETWORK["monitoring_cidrs"]
            for port in INGRESS["monitoring"]
        )
        rules.update(
            port_rule(cidr, port)
            for cidr in NETWORK["database_cluster_cidrs"]
            for port in INGRESS["database_cluster"]
        )

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


def reach_targets(groups: dict, owned: tuple[str, ...]) -> tuple[str | None, str | None, str | None]:
    """Adresy do sond osiagalnosci — WYLACZNIE z grup nalezacych do wlasciciela.

    Wczesniej sonda indeksowala `groups['galera']` na sztywno, wiec uruchomiona
    na definicji PLATFORMY (grupy proxysql/infra/app) wywalala sie z KeyError
    zamiast cokolwiek zmierzyc — polityka firewalla warstwy wspolnej byla
    niesprawdzalna, mimo ze `owned_groups()` deklaruje jej obsluge.

    ZMIANA ZACHOWANIA, swiadoma: przy zakresie NAJEMCY sondy osiagalnosci do
    pary ProxySQL i hosta infra znikaja. Wczesniej najemca je odpytywal, choc
    nie wlada ich polityka — czerwone swiatlo na cudzym hoscie ladowalo w jego
    wyniku, a ten sam host mierzyla druga sonda. Polityke wspolnych hostow
    mierzy przebieg warstwy (`PLATFORM=...`), ktory po tej naprawie w ogole
    istnieje. Granica jest jedna dla asercji i dla sond: wlasciciel.
    """
    def first(group: str) -> str | None:
        if group not in owned:
            return None
        for name, values in (groups.get(group, {}).get("hosts") or {}).items():
            return (values or {}).get("ansible_host", name)
        return None

    return first("galera"), first("proxysql"), first("infra")


def report(failures: list[str], summary: str = "") -> int:
    """Report policy and command failures, including on early exits."""
    everything = failures + COMMAND_FAILURES
    if everything:
        for failure in everything:
            print(f"FAIL: {failure}")
        return 1
    print(summary or "PASS: firewalld policy verified")
    return 0


def main() -> int:
    COMMAND_FAILURES.clear()
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

    first_galera, first_proxy, infra_host = reach_targets(GROUPS, OWNED_GROUPS)
    reach_reference = first_galera or first_proxy or infra_host
    if reach_reference is None:
        failures.append("inwentarz nie ma zadnego hosta w grupach wlasciciela "
                        f"({', '.join(OWNED_GROUPS)}) — nie ma czego zmierzyc")
        return report(failures)
    controller_ip = source_address(reach_reference)

    check(reachable(reach_reference, 22), "controller cannot reach SSH after policy", failures)
    if first_galera:
        check(reachable(first_galera, 3306), "controller cannot reach allowed Galera client port", failures)
        check(not reachable(first_galera, 111), "unexpected rpcbind port 111 reachable", failures)
    if first_proxy:
        check(reachable(first_proxy, 6033), "controller cannot reach allowed ProxySQL client port", failures)
        check(not reachable(first_proxy, 6132), "unexpected ProxySQL TLS admin port 6132 reachable", failures)
        check(not reachable(first_proxy, 6133), "unexpected ProxySQL TLS client port 6133 reachable", failures)
    if infra_host:
        check(reachable(infra_host, 443), "controller cannot reach allowed PMM port", failures)

    if not in_cidrs(controller_ip, NETWORK["monitoring_cidrs"]):
        if first_galera:
            check(not reachable(first_galera, 9100), "node_exporter reachable outside monitoring CIDRs", failures)
        if first_proxy:
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
        return report(failures)

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

    return report(
        failures,
        f"PASS: firewalld exact role policy on {len(owned_hosts)} owned hosts "
        f"({OWNED_PATTERN}); unexpected listeners blocked; "
        "Docker ingress filter and address binding verified",
    )


if __name__ == "__main__":
    sys.exit(main())
