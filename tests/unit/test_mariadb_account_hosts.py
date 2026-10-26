"""Filtr `mariadb_account_hosts` zamienia zrodla logowania na kolumne Host.

Bledna wartosc tutaj = konto otwarte szerzej niz deklaracja albo zrodlo bez
konta, dlatego kazdy niejednoznaczny wpis ma konczyc render bledem.
"""

import importlib.util
import unittest
from pathlib import Path

from ansible.errors import AnsibleFilterError

PLUGIN = Path(__file__).resolve().parents[2] / "playbooks" / "filter_plugins" / "mariadb_accounts.py"
_spec = importlib.util.spec_from_file_location("mariadb_accounts", PLUGIN)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
hosts = _module.mariadb_account_hosts


class AccountHostTests(unittest.TestCase):
    def test_single_addresses_stay_exact(self):
        self.assertEqual(
            hosts(["192.168.1.71", "192.168.1.70/32", "127.0.0.1"]),
            ["192.168.1.71", "192.168.1.70", "127.0.0.1"],
        )

    def test_ipv4_network_uses_mariadb_netmask_form(self):
        self.assertEqual(hosts(["10.20.0.0/16"]), ["10.20.0.0/255.255.0.0"])

    def test_duplicates_collapse_in_order(self):
        self.assertEqual(hosts(["192.168.1.71", "192.168.1.71/32", "192.168.1.72"]),
                         ["192.168.1.71", "192.168.1.72"])

    def test_rejects_entries_that_would_widen_or_break_access(self):
        for bad in (
            ["%"],                 # wzorzec zamiast adresu
            ["x17p1"],             # nazwa: przy skip_name_resolve nigdy nie pasuje
            ["192.168.1.5/24"],    # host bits ustawione: niejednoznaczny zamiar
            ["2001:db8::/64"],     # MariaDB nie stosuje masek IPv6
            [],
            "192.168.1.71",        # napis zamiast listy
        ):
            with self.subTest(bad=bad), self.assertRaises(AnsibleFilterError):
                hosts(bad)

    def test_single_ipv6_address_is_allowed(self):
        self.assertEqual(hosts(["2001:db8::5/128"]), ["2001:db8::5"])


if __name__ == "__main__":
    unittest.main()
