#!/usr/bin/env python3
"""ISC-22 musi być rozstrzygalny na DOWOLNEJ adresacji, nie tylko na naszej.

Kryterium („port admina 6032 nieosiągalny z sieci aplikacyjnej") jest wyrażalne
tylko wtedy, gdy deklaracje rozróżniają obie sieci. Przy `application_cidrs`
zawartym w `administration_cidrs` firewall — robiąc dokładnie to, co mu
zadeklarowano — otwiera 6032 dla aplikacji. Sonda ma to nazwać, a nie zgłaszać
awarii, której nikt nie naprawi kodem.

Testowana jest sama decyzja, więc kontrakt jest przypięty niezależnie od sieci,
w której akurat stoi flota — kod ma trafić także na inne infrastruktury.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "tests" / "lab" / "probe-admin-isolation.py"

sys.path.insert(0, str(PROBE.parent))
_spec = importlib.util.spec_from_file_location("probe_admin_isolation_under_test", PROBE)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class AdminIsolationContractTests(unittest.TestCase):
    def test_separate_networks_make_the_criterion_measurable(self):
        ok, reason = probe.contract_is_expressible(["10.20.0.0/24"], ["10.99.0.0/24"])
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_narrow_admin_inside_the_application_subnet_still_isolates(self):
        """Docelowa deklaracja: aplikacja w podsieci, administracja z kilku /32.

        Zawężenie administracji do stacji — nawet leżących w tej samej podsieci —
        daje realną izolację dla POZOSTAŁYCH adresów, więc kryterium jest
        mierzalne. Pierwsza wersja reguły odrzucała ten układ i tym samym
        wykluczała jedyną tanią drogę do domknięcia ISC-22.
        """
        ok, reason = probe.contract_is_expressible(
            ["10.20.0.0/24"], ["10.20.0.9/32", "10.20.0.10/32"]
        )
        self.assertTrue(ok, reason)

    def test_application_host_that_is_also_an_admin_station_cannot_measure(self):
        admin = ["10.20.0.9/32", "10.99.0.0/24"]
        self.assertTrue(probe.host_is_administrative("10.20.0.9", admin))
        self.assertTrue(probe.host_is_administrative("10.99.0.7", admin))
        self.assertFalse(probe.host_is_administrative("10.20.0.30", admin))
        self.assertFalse(probe.host_is_administrative("", admin))

    def test_many_admin_sources_are_supported(self):
        """Pole jest listą: administracja może iść z kilku miejsc naraz."""
        ok, reason = probe.contract_is_expressible(
            ["10.20.0.0/24"],
            ["10.99.0.5/32", "10.99.1.0/28", "172.16.4.0/24"],
        )
        self.assertTrue(ok, reason)

    def test_one_overlapping_admin_source_spoils_the_isolation(self):
        """Wystarczy JEDEN wpis obejmujący aplikację — reszta nie ma znaczenia."""
        ok, reason = probe.contract_is_expressible(
            ["10.20.0.0/24"],
            ["10.99.0.5/32", "10.20.0.0/16"],
        )
        self.assertFalse(ok)
        self.assertIn("10.20.0.0/24", reason)

    def test_flat_network_is_named_as_unexpressible(self):
        """Stan obecnej floty: obie role na tej samej płaskiej sieci."""
        ok, reason = probe.contract_is_expressible(["192.168.1.0/24"], ["192.168.1.0/24"])
        self.assertFalse(ok)
        self.assertIn("administration_cidrs", reason)

    def test_missing_declaration_is_not_silently_expressible(self):
        for app, adm in ([], ["10.0.0.0/8"]), (["10.0.0.0/8"], []), ([], []):
            with self.subTest(app=app, adm=adm):
                ok, reason = probe.contract_is_expressible(app, adm)
                self.assertFalse(ok)
                self.assertTrue(reason)

    def test_ipv6_declarations_are_handled(self):
        ok, _ = probe.contract_is_expressible(["2001:db8:1::/64"], ["2001:db8:9::/64"])
        self.assertTrue(ok)
        ok, _ = probe.contract_is_expressible(["2001:db8:1::/64"], ["2001:db8::/32"])
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
