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


if __name__ == "__main__":
    unittest.main()
