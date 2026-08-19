#!/usr/bin/env python3
"""Wzorzec UID uzywany przez cluster_deregister.yml do kasowania regul alertowych.

POWSTAL PO CICHEJ AWARII. `f15_dereg_pattern` byl kiedys sklejany bezposrednio
w wyrazeniu `loop`, z nawiasami dokladanymi warunkowo. Wychodzil regexp
niezbalansowany w OBU wariantach:

    konsument -> ^isa-(n11-galera))-
    owner     -> ^isa-(fc10-galera)|(shared))-

Zadanie ma `no_log: true`, wiec Ansible ocenzurowal komunikat bledu i jedynym
sladem bylo `failed=1` w PLAY RECAP. Derejestracja nie kasowala NICZEGO, a
operator niszczyl maszyny w przekonaniu, ze posprzatal — reguly zostawaly w
Grafanie jako sieroty (zmierzone: 9 regul po newclaude11-r9).

Ten test czyta wzorzec Z PLAYBOOKA, a nie z kopii w tescie: kopia rozjechalaby
sie z oryginalem przy pierwszej zmianie i test dalej swiecilby na zielono.
"""

import os
import re
import unittest

import yaml

PLAYBOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "playbooks", "cluster_deregister.yml",
)


def render_pattern(cluster_label: str, role: str) -> str:
    """Renderuje `f15_dereg_pattern` bezposrednio z playbooka."""
    with open(PLAYBOOK, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    raw = doc[0]["vars"]["f15_dereg_pattern"]

    from jinja2 import Template

    return Template(raw).render(
        cluster_label=cluster_label,
        proxysql={"role": role},
    )


class TestDeregisterRulePattern(unittest.TestCase):
    OWNER = "fc10-galera"
    CONSUMER = "n11-galera"

    def test_pattern_is_valid_regex(self):
        """Niezbalansowany nawias to wlasnie ten blad — regexp MUSI sie kompilowac."""
        for label, role in ((self.OWNER, "owner"), (self.CONSUMER, "consumer")):
            with self.subTest(role=role):
                re.compile(render_pattern(label, role))

    def test_consumer_matches_only_own_rules(self):
        pat = re.compile(render_pattern(self.CONSUMER, "consumer"))
        self.assertTrue(pat.search("isa-n11-galera-node-loss"))
        self.assertTrue(pat.search("isa-n11-galera-tls-cert-expiring"))
        # Cudze reguly i wspoldzielone musza przezyc teardown konsumenta.
        self.assertFalse(pat.search("isa-fc10-galera-node-loss"))
        self.assertFalse(pat.search("isa-shared-proxysql-down"))

    def test_owner_also_matches_shared_rules(self):
        pat = re.compile(render_pattern(self.OWNER, "owner"))
        self.assertTrue(pat.search("isa-fc10-galera-node-loss"))
        self.assertTrue(pat.search("isa-shared-proxysql-down"))
        # Ale NIE reguly innego najemcy — owner sprzata warstwe wspolna i siebie,
        # nie cudze klastry.
        self.assertFalse(pat.search("isa-n11-galera-node-loss"))

    def test_pattern_is_anchored(self):
        """Bez kotwicy `^` wzorzec lapalby UID-y z etykieta w srodku."""
        for label, role in ((self.OWNER, "owner"), (self.CONSUMER, "consumer")):
            with self.subTest(role=role):
                pat = re.compile(render_pattern(label, role))
                self.assertFalse(pat.search(f"legacy-isa-{label}-node-loss"))


if __name__ == "__main__":
    unittest.main()
