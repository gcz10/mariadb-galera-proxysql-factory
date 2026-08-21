#!/usr/bin/env python3
"""Kontrakt reguly alertowej `node-silent` (wezel zarejestrowany, ale niemy).

POWSTALA PO ZMIERZONEJ AWARII (2026-08-21). Po twardej utracie maszyny wezel
`n16g3` wrocil, `systemctl is-active pmm-agent` mowil `active`, `is-enabled`
mowil `enabled` — a `pmm-admin status` pokazywal `Connected: false` i ZERO
eksporterow. Wezel milczal ~16 minut i **zaden istniejacy alert tego nie lapal**.

Dlaczego nie lapal: `node-loss` liczy `mysql_global_status_wsrep_cluster_size`,
czyli WLASNY widok Galery. Gdy jeden wezel oslepnie monitoringowo, pozostale
nadal raportuja pelny rozmiar klastra, wiec `min(...)` sie nie zmienia. Baza
jest zdrowa, monitoring slepy, alert cichy.

Regula `node-silent` liczy natomiast wezly, ktore REALNIE oddaly probke
(`node_boot_time_seconds` per `node_name`). Ten test broni tego rozroznienia:
gdyby ktos przepisal wyrazenie na metryke stanu Galery, luka wrocilaby po cichu,
a testy dalej bylyby zielone.

Zmierzona latencja wykrycia na zywym PMM: 302 s dla najemcy i 319 s dla warstwy
wspolnej — to staleness magazynu metryk, nie opoznienie reguly.
"""

import os
import unittest

import yaml

PLAYBOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "playbooks", "f15_alerts.yml",
)


def rule_sets() -> dict:
    """Zwraca `f15_cluster_rules` i `f15_shared_rules` prosto z playbooka."""
    with open(PLAYBOOK, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    for play in doc:
        variables = play.get("vars") or {}
        if "f15_cluster_rules" in variables:
            return {
                "cluster": variables["f15_cluster_rules"],
                "shared": variables.get("f15_shared_rules", []),
            }
    raise AssertionError(f"nie znaleziono f15_cluster_rules w {PLAYBOOK}")


class NodeSilentRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = rule_sets()

    def _silent_rule(self, scope: str) -> dict:
        matches = [r for r in self.rules[scope] if str(r.get("uid", "")).endswith("-node-silent")]
        self.assertEqual(
            len(matches), 1,
            f"zakres '{scope}' musi miec DOKLADNIE jedna regule node-silent, ma {len(matches)}",
        )
        return matches[0]

    def test_both_scopes_have_the_rule(self):
        """Luka dotyczy tak samo najemcy jak warstwy wspolnej — obie ja potrzebuja."""
        for scope in ("cluster", "shared"):
            with self.subTest(scope=scope):
                self._silent_rule(scope)

    def test_counts_reporting_nodes_not_galera_self_view(self):
        """Sedno reguly: liczy oddane probki, NIE stan raportowany przez Galere.

        Gdyby wyrazenie siegnelo po `wsrep_*`, wrocilaby dokladnie ta luka,
        ktora regula zamyka — i nikt by tego nie zauwazyl.
        """
        for scope in ("cluster", "shared"):
            with self.subTest(scope=scope):
                expr = self._silent_rule(scope)["expr"]
                self.assertIn("node_boot_time_seconds", expr)
                self.assertIn("count by (node_name)", expr)
                self.assertNotIn("wsrep_", expr)
                self.assertNotIn("mysql_global_status", expr)

    def test_scoped_by_cluster_label(self):
        """Bez zawezenia po `cluster` regula najemcy liczylaby cudze wezly."""
        for scope in ("cluster", "shared"):
            with self.subTest(scope=scope):
                self.assertIn('cluster="{{ cluster_label }}"', self._silent_rule(scope)["expr"])

    def test_fail_closed_on_missing_data(self):
        """Brak danych to wlasnie objaw awarii — nie moze oznaczac 'OK'."""
        for scope in ("cluster", "shared"):
            with self.subTest(scope=scope):
                rule = self._silent_rule(scope)
                self.assertEqual(rule["noDataState"], "Alerting")
                self.assertIn("or vector(0)", rule["expr"])
                self.assertEqual(rule["severity"], "critical")

    def test_expected_count_comes_from_definition(self):
        """Liczba oczekiwanych wezlow musi byc wyprowadzona, nie zaszyta."""
        self.assertIn("{{ galera.nodes_expected }}", self._silent_rule("cluster")["expr"])
        self.assertIn("{{ proxysql.nodes_expected }}", self._silent_rule("shared")["expr"])

    def test_node_loss_rule_still_reads_galera_state(self):
        """Obie reguly sa potrzebne i mierza CO INNEGO — to nie duplikat."""
        loss = [r for r in self.rules["cluster"] if str(r.get("uid", "")).endswith("-node-loss")]
        self.assertEqual(len(loss), 1)
        self.assertIn("wsrep_cluster_size", loss[0]["expr"])


if __name__ == "__main__":
    unittest.main()
