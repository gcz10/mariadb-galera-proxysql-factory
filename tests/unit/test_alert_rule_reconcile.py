#!/usr/bin/env python3
"""Kontrakt cyklu zycia regul alertowych ISC-47.

Dwa niezalezne znaleziska z 2026-08-21, oba zmierzone na newclaude16-r9.

1. REGULA, KTORA NIE MOGLA ZADZIALAC. `no-writer` brzmiala
   `count(...{hostgroup=<writer>} == 1) < 1`, czyli "hostgroup writera jest
   pusty". Nie udalo sie jej zapalic w ZADNYM z trzech scenariuszy: jeden wezel
   poza kworum, calkowita utrata kworum (wszystkie non-Primary), wszystkie
   backendy nieosiagalne dla monitora. Za kazdym razem ProxySQL zostawial jeden
   wezel ONLINE w hostgroupie writera — polityka "last man standing", ktorej
   dokumentacja produktu nie opisuje (kryteria sa tylko na schematach blokowych).
   Wynik: 361 probek, ZERO zapalen. Zastapiona przez `backends-offline`, ktora
   w tym samym oknie zapalila sie 20 razy, w kazdej z trzech awarii.

2. BRAK REKONCYLIACJI. Playbook tworzyl i aktualizowal reguly, ale nie kasowal
   tych, ktore zniknely z definicji. Po podmianie obie istnialy obok siebie,
   a stara nie miala juz wlasciciela. Krok sprzatajacy MUSI byc zawezony po
   `managed_by` I `cluster` — bez tego jeden najemca skasowalby reguly
   pozostalym oraz `isa-shared-*`.
"""

import os
import unittest

import yaml

RULES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "playbooks", "vars", "alert_rules.yml",
)
PLAYBOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "playbooks", "f15_alerts.yml",
)


def load_play() -> dict:
    with open(PLAYBOOK, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    play = doc[0] if doc else {}
    if "vars" not in play:
        play["vars"] = {}
    if os.path.isfile(RULES_FILE):
        with open(RULES_FILE, encoding="utf-8") as handle:
            rules_data = yaml.safe_load(handle) or {}
        play["vars"].update(rules_data)
    if "f15_cluster_rules" in play["vars"]:
        return play
    for p in doc:
        if "f15_cluster_rules" in (p.get("vars") or {}):
            return p
    raise AssertionError(f"nie znaleziono play z f15_cluster_rules w {RULES_FILE} ani {PLAYBOOK}")

class AlertRuleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.play = load_play()
        variables = cls.play["vars"]
        cls.cluster_rules = variables["f15_cluster_rules"]
        cls.shared_rules = variables.get("f15_shared_rules", [])
        cls.tasks = cls.play.get("tasks", [])

    def test_no_rule_waits_for_empty_writer_hostgroup(self):
        """Straznik regresji: ProxySQL NIGDY nie oprozni hostgrupy writera.

        Kazda regula oparta na tym zalozeniu jest z definicji martwa. Zmierzone
        pod trzema roznymi awariami — patrz naglowek pliku.
        """
        for scope, rules in (("cluster", self.cluster_rules), ("shared", self.shared_rules)):
            for rule in rules:
                with self.subTest(scope=scope, uid=rule.get("uid")):
                    expr = rule.get("expr", "")
                    dead = "galera_writer_hg" in expr and "< bool 1" in expr
                    self.assertFalse(
                        dead,
                        f"{rule.get('uid')}: regula czeka na oproznienie hostgrupy writera, "
                        f"czego ProxySQL nie robi — nie moze zadzialac",
                    )

    def test_backends_offline_rule_watches_offline_hostgroup(self):
        """Zastepstwo mierzy decyzje routingowa ProxySQL, ktora realnie sie zmienia."""
        matches = [r for r in self.cluster_rules if str(r.get("uid", "")).endswith("-backends-offline")]
        self.assertEqual(len(matches), 1, "oczekiwano jednej reguly backends-offline")
        rule = matches[0]
        self.assertIn("galera_offline_hg", rule["expr"])
        self.assertIn("or vector(0)", rule["expr"])
        self.assertEqual(rule["noDataState"], "Alerting")
        self.assertEqual(rule["severity"], "critical")

    def _prune_task(self) -> dict:
        matches = [
            t for t in self.tasks
            if t.get("ansible.builtin.uri", {}).get("method") == "DELETE"
            and "alert-rules" in str(t.get("ansible.builtin.uri", {}).get("url", ""))
            and "f15_effective_rules" in str(t.get("loop", ""))
        ]
        self.assertEqual(
            len(matches), 1,
            "musi istniec DOKLADNIE jeden krok kasujacy reguly spoza definicji",
        )
        return matches[0]

    def test_prune_is_scoped_to_own_managed_rules(self):
        """Najmocniejsza asercja pliku: najemca nie moze skasowac cudzych regul.

        Ta sama klasa bledu, ktora repo naprawialo przy wzorcu derejestracji
        (`^isa-<cluster_label>-`) i przy koncie MinIO.
        """
        loop = str(self._prune_task()["loop"])
        self.assertIn("managed_by", loop)
        self.assertIn("'ansible'", loop)
        self.assertIn("labels.cluster", loop)
        self.assertIn("cluster_label", loop)

    def test_quorum_loss_rule_survives_as_the_real_guard(self):
        """Utrate kworum pokrywa `quorum-loss` — zapalila sie tam, gdzie no-writer milczala."""
        matches = [r for r in self.cluster_rules if str(r.get("uid", "")).endswith("-quorum-loss")]
        self.assertEqual(len(matches), 1)
        self.assertIn("wsrep_cluster_status", matches[0]["expr"])


if __name__ == "__main__":
    unittest.main()
