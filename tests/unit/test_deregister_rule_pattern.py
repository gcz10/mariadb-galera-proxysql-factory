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

import hashlib
import os
import re
import unittest

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLAYBOOK = os.path.join(REPO, "playbooks", "cluster_deregister.yml")
ALERT_IDENTITY = os.path.join(REPO, "playbooks", "vars", "alert_identity.yml")


def render_pattern(cluster_label: str) -> str:
    """Renderuje `f15_dereg_pattern` bezposrednio z playbooka."""
    with open(PLAYBOOK, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    patterns = [
        variables["f15_dereg_pattern"]
        for play in doc
        if "f15_dereg_pattern" in (variables := (play.get("vars") or {}))
    ]
    if len(patterns) != 1:
        raise AssertionError(
            f"oczekiwano jednego f15_dereg_pattern, znaleziono {len(patterns)}"
        )
    raw = patterns[0]

    from jinja2 import Template

    with open(ALERT_IDENTITY, encoding="utf-8") as handle:
        identity = yaml.safe_load(handle)
    uid_template = Template(identity["f15_uid_prefix"])
    uid_template.environment.filters["hash"] = (
        lambda value, algorithm: hashlib.new(algorithm, value.encode()).hexdigest()
    )
    uid_prefix = uid_template.render(cluster_label=cluster_label).strip()
    return Template(raw).render(
        cluster_label=cluster_label,
        f15_uid_prefix=uid_prefix,
    )


class TestDeregisterRulePattern(unittest.TestCase):
    # `fc10-galera` bylo do 2026-08-21 ownerem warstwy wspolnej i jako jedyne
    # kasowalo tez reguly `isa-shared-*`. Zostaje tu jako etykieta testowa
    # WLASNIE dlatego: gdyby ktos przywrocil gałąź ownera, ten test ma paść.
    EX_OWNER = "fc10-galera"
    TENANT = "n11-galera"

    def test_pattern_is_valid_regex(self):
        """Niezbalansowany nawias to wlasnie ten blad — regexp MUSI sie kompilowac."""
        for label in (self.EX_OWNER, self.TENANT):
            with self.subTest(label=label):
                re.compile(render_pattern(label))

    def test_tenant_matches_only_own_rules(self):
        pat = re.compile(render_pattern(self.TENANT))
        self.assertTrue(pat.search("isa-n11-galera-node-loss"))
        self.assertTrue(pat.search("isa-n11-galera-tls-cert-expiring"))
        # Cudze reguly i wspoldzielone musza przezyc teardown najemcy.
        self.assertFalse(pat.search("isa-fc10-galera-node-loss"))
        self.assertFalse(pat.search("isa-shared-proxysql-down"))

    def test_no_tenant_can_delete_shared_rules(self):
        """Najmocniejsza gwarancja tego pliku po wyniesieniu warstwy wspolnej.

        Kazdy klaster jest teraz najemca, wiec ZADEN nie ma prawa skasowac
        `isa-shared-*`. Wczesniej owner to robil — i dokladnie stad brala sie
        klasa bledu, ktora repo naprawialo juz raz przy koncie MinIO: teardown
        jednego najemcy zabieral zasob calej floty.
        """
        for label in (self.EX_OWNER, self.TENANT):
            with self.subTest(label=label):
                pat = re.compile(render_pattern(label))
                self.assertFalse(
                    pat.search("isa-shared-proxysql-down"),
                    f"{label}: derejestracja najemcy kasuje reguly warstwy wspolnej",
                )
                self.assertTrue(pat.search(f"isa-{label}-node-loss"))

    def test_long_label_uses_the_same_hashed_prefix_as_alert_provisioning(self):
        label = "cassiopeiav10-r10"
        prefix = "isa-" + hashlib.sha256(label.encode()).hexdigest()[:12]
        pattern = re.compile(render_pattern(label))
        self.assertTrue(pattern.search(f"{prefix}-restore-drill-stale"))
        self.assertFalse(pattern.search(f"isa-{label}-restore-drill-stale"))

    def test_pattern_is_anchored(self):
        """Bez kotwicy `^` wzorzec lapalby UID-y z etykieta w srodku."""
        for label in (self.EX_OWNER, self.TENANT):
            with self.subTest(label=label):
                pat = re.compile(render_pattern(label))
                self.assertFalse(pat.search(f"legacy-isa-{label}-node-loss"))


if __name__ == "__main__":
    unittest.main()
