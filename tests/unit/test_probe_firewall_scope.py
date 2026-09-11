#!/usr/bin/env python3
"""Sonda firewalla musi mierzyc RÓWNIEZ warstwe wspolna, nie tylko najemce.

`owned_groups()` deklaruje obsluge obu zakresow (najemca: galera/restore,
platforma: proxysql/infra/app), ale sekcja osiagalnosci indeksowala
`GROUPS['galera']` na sztywno. Na definicji platformy sonda wywalala sie
`KeyError: 'galera'` ZANIM cokolwiek zmierzyla — polityka firewalla warstwy
wspolnej nie miala jak przejsc ani nie miala jak polec.
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tests" / "lab" / "probe-firewall.py"

TENANT_INVENTORY = {
    "all": {"children": {
        "galera": {"hosts": {"g1": {"ansible_host": "192.0.2.11"}}},
        "restore": {"hosts": {"r1": {"ansible_host": "192.0.2.12"}}},
        "proxysql": {"hosts": {"p1": {"ansible_host": "192.0.2.21"}}},
        "infra": {"hosts": {"i1": {"ansible_host": "192.0.2.31"}}},
    }},
}
PLATFORM_INVENTORY = {
    "all": {"children": {
        "proxysql": {"hosts": {"p1": {"ansible_host": "192.0.2.21"}}},
        "infra": {"hosts": {"i1": {"ansible_host": "192.0.2.31"}}},
        "app": {"hosts": {"a1": {"ansible_host": "192.0.2.41"}}},
    }},
}
CONFIG = {
    "network": {
        "administration_cidrs": ["192.0.2.0/24"],
        "database_cluster_cidrs": ["192.0.2.0/24"],
        "monitoring_cidrs": ["192.0.2.70/32"],
        "application_cidrs": ["192.0.2.0/24"],
    },
}


def load_probe(inventory, config):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "inventory.yml").write_text(yaml.safe_dump(inventory), encoding="utf-8")
        (root / "cluster.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("probe_firewall_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {
            "CLUSTER_CONFIG": str(root / "cluster.yml"),
            "CLUSTER_INVENTORY": str(root / "inventory.yml"),
        }):
            spec.loader.exec_module(module)
    return module


class ProbeFirewallScopeTests(unittest.TestCase):
    def test_tenant_scope_measures_its_own_nodes(self):
        module = load_probe(TENANT_INVENTORY, CONFIG)
        galera, proxy, infra = module.reach_targets(module.GROUPS, module.OWNED_GROUPS)
        self.assertEqual(galera, "192.0.2.11")
        # ZMIANA ZACHOWANIA wzgledem poprzedniej wersji sondy, swiadoma: para
        # ProxySQL i host infra naleza do warstwy wspolnej. Najemca ich polityki
        # nie wlada, wiec cudzy czerwony nie moze ladowac w jego wyniku; mierzy
        # je przebieg `PLATFORM=...` (test nizej), ktory wczesniej sie wywalal.
        self.assertIsNone(proxy)
        self.assertIsNone(infra)

    def test_platform_scope_measures_shared_nodes_instead_of_crashing(self):
        config = dict(CONFIG, platform={"name": "probe-platform"})
        module = load_probe(PLATFORM_INVENTORY, config)
        galera, proxy, infra = module.reach_targets(module.GROUPS, module.OWNED_GROUPS)
        self.assertIsNone(galera)
        self.assertEqual(proxy, "192.0.2.21")
        self.assertEqual(infra, "192.0.2.31")

    def test_owner_group_without_hosts_yields_nothing_to_measure(self):
        config = dict(CONFIG, platform={"name": "probe-platform"})
        module = load_probe({"all": {"children": {"galera": {"hosts": {}}}}}, config)
        self.assertEqual(
            module.reach_targets(module.GROUPS, module.OWNED_GROUPS),
            (None, None, None),
        )

    def run_main(self, module, broken_docker=False, reachable=None):
        queried = []

        def command(pattern, command, timeout=120):
            queried.append(pattern)
            owned = set().union(*(module.hosts(group) for group in module.OWNED_GROUPS))
            if command == "firewall-cmd --list-all":
                return {
                    host: "public (active)\n  target: default\n  ports:\n  sources:\n"
                    "  services: dhcpv6-client\n"
                    + "\n".join("  " + rule for rule in module.expected_rules(host))
                    for host in owned
                }
            if command == "systemctl is-enabled firewalld":
                return dict.fromkeys(owned, "enabled")
            if command == "firewall-cmd --get-active-zones":
                return dict.fromkeys(owned, "public\n  interfaces: eth0")
            if pattern != "infra":
                raise AssertionError(f"unexpected target {pattern}: {command}")
            if command.startswith("find /lib/modules"):
                return {"i1": "/lib/modules/test/xt_conntrack.ko"}
            if command == "iptables -S ISA-INFRA":
                rules = [f"-A ISA-INFRA --ctorigdstport {p} -j DROP"
                         for p in (80, 443, 9000, 9001, 8025)]
                if not broken_docker:
                    rules.append("-A ISA-INFRA -j DROP")
                return {"i1": "\n".join(rules)}
            if command == "iptables -S DOCKER-USER":
                return {"i1": "-A DOCKER-USER --ctorigdst 192.0.2.31 -j ISA-INFRA"}
            if command == "iptables -S":
                return {"i1": "-N ISA-INFRA"}
            if command == "systemctl is-enabled isa-docker-firewall.service":
                return {"i1": "enabled"}
            if command.startswith("systemctl show docker.service"):
                return {"i1": "Requires=isa-docker-firewall.service\nAfter=isa-docker-firewall.service"}
            if command == "ss -ltnH":
                return {"i1": "\n".join(f"LISTEN 0 128 192.0.2.31:{p}"
                                       for p in (80, 443, 9000, 9001, 8025))}
            raise AssertionError(command)

        with (
            mock.patch.object(module, "run_command", side_effect=command),
            mock.patch.object(module, "source_address", return_value="192.0.2.70"),
            mock.patch.object(
                module, "reachable",
                side_effect=reachable or (lambda host, port: port in (22, 3306, 6033, 443)),
            ),
            mock.patch("builtins.print") as printed,
        ):
            code = module.main()
        return code, "\n".join(str(call) for call in printed.call_args_list), queried

    def test_tenant_main_does_not_query_or_certify_shared_docker_policy(self):
        module = load_probe(TENANT_INVENTORY, CONFIG)
        code, output, queried = self.run_main(module, broken_docker=True)
        self.assertEqual(code, 0, output)
        self.assertNotIn("infra", queried)
        self.assertNotIn("Docker ingress filter and address binding verified", output)

    def test_platform_main_measures_docker_and_rejects_missing_drop(self):
        module = load_probe(PLATFORM_INVENTORY, dict(CONFIG, platform={"name": "probe-platform"}))
        for broken, expected in ((False, 0), (True, 1)):
            with self.subTest(broken_docker=broken):
                code, output, queried = self.run_main(module, broken_docker=broken)
                self.assertEqual(code, expected, output)
                self.assertIn("infra", queried)
                self.assertIn("Docker chain has no final fail-closed rule" if broken
                              else "Docker ingress filter and address binding verified", output)


class ProbeMeasurementPathTests(unittest.TestCase):
    """„Nie zmierzono" i „zmierzono, jest zle" musza dawac rozne kody.

    Host kontrolny bez dostepu do sieci laboratorium (macOS 26 blokuje gniazda
    LAN binariom spoza systemu) dostawal FAIL „controller cannot reach SSH after
    policy" — sonda oskarzala flote o defekt lezacy po stronie operatora, choc
    Ansible w tej samej sekundzie wykonywal tam polecenia po SSH.
    """

    def test_dead_measurement_path_is_undetermined_not_failure(self):
        module = load_probe(TENANT_INVENTORY, CONFIG)
        code, output, _ = ProbeFirewallScopeTests().run_main(
            module, reachable=lambda host, port: False
        )
        self.assertEqual(code, 2, output)
        self.assertIn("UNDETERMINED", output)
        self.assertIn("FIREWALL_PROBE_VANTAGE", output)


class ProbePartialFailureTests(unittest.TestCase):
    """Jeden potykajacy sie host nie moze skasowac pomiaru calej floty.

    `run_command` rzucalo `RuntimeError` na kazdym niezerowym rc `ansible`.
    Dwa najczestsze niezerowe rc to nie awaria narzedzia, tylko WYNIK pomiaru
    albo czesciowa niedostepnosc: `systemctl is-enabled firewalld` zwraca rc=1
    dokladnie wtedy, gdy firewalld jest wylaczony (czyli w sytuacji, ktora ta
    sonda ma raportowac), a jeden nieosiagalny wezel zabieral wyniki wszystkich
    pozostalych.
    """

    SAMPLE = (
        "g1 | SUCCESS | rc=0 >>\n"
        "enabled\n"
        "g2 | UNREACHABLE! => {\"changed\": false, \"msg\": \"timed out\"}\n"
        "g3 | FAILED | rc=1 >>\n"
        "disabled"
    )

    def run_probe_command(self, stdout, returncode):
        module = load_probe(TENANT_INVENTORY, CONFIG)
        module.COMMAND_FAILURES.clear()
        fake = mock.Mock(returncode=returncode, stdout=stdout, stderr="")
        with mock.patch.object(module.subprocess, "run", return_value=fake):
            return module, module.run_command("galera", "systemctl is-enabled firewalld")

    def test_nonzero_rc_still_yields_the_measurement(self):
        module, output = self.run_probe_command(self.SAMPLE, 2)
        self.assertEqual(output.get("g1"), "enabled")
        # rc=1 z `is-enabled` NIESIE wynik: firewalld jest wylaczony. Sonda ma
        # go zmierzyc i zglosic, a nie wywrocic sie na kodzie wyjscia.
        self.assertEqual(output.get("g3"), "disabled")

    def test_unreachable_host_is_recorded_as_failure_not_silence(self):
        module, output = self.run_probe_command(self.SAMPLE, 2)
        self.assertNotIn("g2", output)
        self.assertTrue(
            any("g2" in failure for failure in module.COMMAND_FAILURES),
            "nieosiagalny host zniknal bez sladu — sonda skonczylaby sie zielono",
        )

    def test_call_returning_nothing_at_all_is_reported(self):
        module, output = self.run_probe_command("", 4)
        self.assertEqual(output, {})
        self.assertEqual(len(module.COMMAND_FAILURES), 1)

    def test_module_failure_does_not_contaminate_previous_host_output(self):
        module, output = self.run_probe_command(
            'g1 | SUCCESS | rc=0 >>\nenabled\n'
            'g2 | FAILED! => {"msg": "module failed"}\n', 2
        )
        self.assertEqual(output, {"g1": "enabled"})
        self.assertTrue(any("g2" in failure for failure in module.COMMAND_FAILURES))

    def test_dead_ansible_path_reports_connectivity_not_invented_findings(self):
        """Gdy zaden host nie odpowiada, wynikiem jest brak lacznosci — nic wiecej.

        Wczesniej sonda dopisywala tu „brak modulu xt_conntrack": wniosek z
        CISZY, nie z pomiaru. Kod pozostaje 1, bo porazki lacznosci sa realne,
        ale zmyslone findingi o polityce Dockera znikaja z raportu.
        """
        module = load_probe(PLATFORM_INVENTORY, dict(CONFIG, platform={"name": "probe-platform"}))
        fake = mock.Mock(returncode=4, stdout="g1 | UNREACHABLE! => {}\n", stderr="")
        with (
            mock.patch.object(module.subprocess, "run", return_value=fake),
            mock.patch.object(module, "source_address", return_value="192.0.2.70"),
            mock.patch.object(module, "reachable", return_value=False),
            mock.patch("builtins.print") as printed,
        ):
            code = module.main()
        self.assertEqual(code, 1)
        messages = "\n".join(str(call) for call in printed.call_args_list)
        self.assertIn("g1: nieosiagalny", messages)
        self.assertNotIn("xtables", messages)

    def test_clean_run_passes_with_summary(self):
        module = load_probe(TENANT_INVENTORY, CONFIG)
        module.COMMAND_FAILURES.clear()
        with mock.patch("builtins.print") as printed:
            code = module.report([], "PASS: wszystko zmierzone")
        self.assertEqual(code, 0)
        printed.assert_called_once_with("PASS: wszystko zmierzone")


if __name__ == "__main__":
    unittest.main()
