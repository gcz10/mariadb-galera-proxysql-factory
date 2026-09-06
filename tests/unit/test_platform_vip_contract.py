#!/usr/bin/env python3
"""Sonda warstwy i bramka Keepalived muszą opisywać ten sam stan tak samo.

`check_proxysql.sh` (ISC-26) rozróżnia dwa przypadki: zero aktywnych grup Galera
to świeża warstwa bez najemców — VIP zostaje; aktywne grupy bez writera ONLINE
to stan, w którym adres jest ŚWIADOMIE zdejmowany, bo nie ma dokąd kierować
ruchu. `probe-platform.py` żądała VIP-a bezwarunkowo, więc ten sam stan
opisywała jako naruszenie: na warstwie z zatrzymanymi najemcami
`platform-verify` padał zawsze i przestawał cokolwiek znaczyć (zmierzone
2026-09-05 na xenonv11).

Testowana jest decyzja, nie tekst komunikatu: kiedy brak VIP-a jest awarią,
a kiedy pomiarem niemożliwym do rozstrzygnięcia.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "tests" / "lab" / "probe-platform.py"
GATE = REPO / "roles" / "proxysql_endpoint" / "files" / "check_proxysql.sh"

# Sondy importuja `_probe_common` po sasiedzku, tak jak przy uruchomieniu
# z `tests/lab/` — bez tego modul nie da sie zaladowac w tescie.
sys.path.insert(0, str(PROBE.parent))
_spec = importlib.util.spec_from_file_location("probe_platform_under_test", PROBE)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

HOSTS = ["p1", "p2"]


def state(groups: int, writers: int, hosts=HOSTS) -> dict:
    # Odczyt jest per węzeł pary; obie instancje widzą tę samą konfigurację
    # runtime, więc rozkładamy zmierzone liczby na pierwszy węzeł.
    return {
        host: {"GROUPS": str(groups if i == 0 else 0), "WRITERS": str(writers if i == 0 else 0)}
        for i, host in enumerate(hosts)
    }


class PlatformVipContractTests(unittest.TestCase):
    def test_stopped_tenants_make_the_missing_vip_unmeasurable(self):
        """Aktywne grupy + zero writerów = bramka zdjęła adres celowo."""
        self.assertTrue(probe.withdrawal_is_expected(state(groups=4, writers=0), HOSTS))

    def test_live_writer_keeps_the_vip_mandatory(self):
        """Jest komu obsłużyć ruch — brak VIP-a pozostaje awarią."""
        self.assertFalse(probe.withdrawal_is_expected(state(groups=4, writers=1), HOSTS))

    def test_layer_without_tenants_still_must_hold_the_vip(self):
        """Zero grup to przypadek (a) bramki: adres ma być trzymany.

        Regresja z 2026-08-21: świeża para nie mogła wstać, bo bramka myliła
        „zero najemców" z „najemca bez writera".
        """
        self.assertFalse(probe.withdrawal_is_expected(state(groups=0, writers=0), HOSTS))

    def test_incomplete_pair_reading_grants_no_leniency(self):
        """Brakujący host mógł być właśnie tym, który trzymał adres."""
        partial = {"p1": {"GROUPS": "4", "WRITERS": "0"}}
        self.assertFalse(probe.withdrawal_is_expected(partial, HOSTS))
        self.assertFalse(probe.withdrawal_is_expected({}, HOSTS))

    def test_probe_and_gate_count_writers_the_same_way(self):
        """Oba pliki muszą pytać o TO SAMO, inaczej rozjadą się ponownie.

        Nie porównujemy tekstu zapytań, tylko warunki, które o werdykcie
        decydują: aktywna grupa, hostgroup writera i status ONLINE.
        """
        gate = GATE.read_text(encoding="utf-8")
        probe_src = PROBE.read_text(encoding="utf-8")
        for fragment in ("runtime_mysql_galera_hostgroups", "writer_hostgroup", "ONLINE"):
            self.assertIn(fragment, gate)
            self.assertIn(fragment, probe_src)
        # Bramka liczy writerów jako JOIN po hostgrupie writera aktywnych grup;
        # sonda musi liczyć tak samo, inaczej jedna z nich zobaczy inny świat.
        joins = re.findall(r"s\.hostgroup_id\s*=\s*g\.writer_hostgroup", probe_src)
        self.assertEqual(len(joins), 1, "sonda nie liczy writerów tak jak bramka")


if __name__ == "__main__":
    unittest.main()
