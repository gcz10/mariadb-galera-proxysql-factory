#!/usr/bin/env python3
"""Zakres derejestracji na WSPOLNYM PMM: najemca kasuje wylacznie swoje zasoby.

POWSTAL PO AUDYCIE 2026-09-07. `cluster_deregister.yml` wybieral reguly, uslugi
i wezly dopasowaniem PREFIKSOWYM (`search('^' ~ cluster_label ~ '-')`). Nazwa
najemcy jest dowolnym tekstem, wiec prefiks `tenant-` lapie rowniez zasoby
sasiada `tenant-2`: derejestracja jednego klastra kasowala monitoring drugiego,
ktory dzialal dalej i po cichu przestawal byc obserwowany.

Zaden wzorzec prefiksowy tego nie rozstrzyga — dlatego playbook dopasowuje
ROWNOSCIOWO: po etykiecie (`labels.cluster`, `cluster`, `custom_labels.cluster`)
nadawanej przy rejestracji, a dla materialu bez etykiety po dokladnej nazwie z
inwentarza albo UID folderu alertow (unikalnym per najemca).

Test renderuje wyrazenia WPROST Z PLAYBOOKA (nie z kopii w tescie) na atrapie
odpowiedzi PMM, w ktorej obok siebie stoja: nasz klaster, sasiad o nazwie
bedacej naszym prefiksem oraz zasoby warstwy wspolnej.
"""

import unittest
from pathlib import Path

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "playbooks" / "cluster_deregister.yml"

LABEL = "tenant"
SIBLING = "tenant-2"
HOSTS = ["g1", "g2", "px1"]


def _flatten(value):
    out = []
    for item in value:
        if isinstance(item, list):
            out.extend(_flatten(item))
        else:
            out.append(item)
    return out


def _union(left, right):
    merged = list(left)
    merged.extend(item for item in right if item not in merged)
    return merged


class _Response:
    def __init__(self, json):
        self.json = json


class DeregisterTenantScopeTests(unittest.TestCase):
    """Sasiad o dluzszej nazwie i warstwa wspolna musza przezyc derejestracje."""

    @classmethod
    def setUpClass(cls):
        cls.doc = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
        cls.play = next(
            play
            for play in cls.doc
            if "usun reguly alertow" in play.get("name", "")
        )
        cls.env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        cls.env.filters["flatten"] = _flatten
        cls.env.filters["union"] = _union

    def _task_expression(self, url_fragment):
        for task in self.play["tasks"]:
            url = str(task.get("ansible.builtin.uri", {}).get("url", ""))
            if url_fragment in url:
                return task["loop"]
        raise AssertionError(f"brak zadania z URL zawierajacym {url_fragment}")

    def _fact_expression(self, name):
        for task in self.play["tasks"]:
            fact = task.get("ansible.builtin.set_fact", {})
            if name in fact:
                return fact[name]
        raise AssertionError(f"brak set_fact ustawiajacego {name}")

    def _render(self, expression, **context):
        node_names = [f"{LABEL}-{host}" for host in HOSTS]
        service_names = [
            name + suffix
            for name in node_names
            for suffix in ("-mysql", "-mysql-agent", "-proxysql", "-node-exporter")
        ]
        base = {
            "cluster_label": LABEL,
            "f15_folder_uid": f"isa-alerts-{LABEL}",
            "pmm_dereg_node_names": node_names,
            "pmm_dereg_service_names": service_names,
        }
        base.update(context)
        return self.env.from_string(expression).render(**base)

    def test_sibling_tenant_alert_rules_survive(self):
        rules = [
            {"uid": f"isa-{LABEL}-node-loss", "labels": {"cluster": LABEL},
             "folderUID": f"isa-alerts-{LABEL}"},
            {"uid": f"isa-{SIBLING}-node-loss", "labels": {"cluster": SIBLING},
             "folderUID": f"isa-alerts-{SIBLING}"},
            {"uid": "isa-shared-endpoint-down", "labels": {"cluster": "shared"},
             "folderUID": "isa-alerts-shared"},
            # Regula sprzed wprowadzenia etykiet: rozstrzyga folder najemcy.
            {"uid": f"isa-{LABEL}-legacy", "folderUID": f"isa-alerts-{LABEL}"},
            {"uid": f"isa-{SIBLING}-legacy", "folderUID": f"isa-alerts-{SIBLING}"},
        ]
        expression = self._task_expression("alert-rules/{{ item.uid }}")
        selected = [
            rule["uid"]
            for rule in eval(  # noqa: S307 - renderowana lista literalow
                self._render(expression, f15_dereg_rules=_Response(rules))
            )
        ]
        self.assertEqual(selected, [f"isa-{LABEL}-node-loss", f"isa-{LABEL}-legacy"])

    def test_sibling_tenant_services_survive(self):
        services = {
            "mysql": [
                {"service_id": "own-mysql", "service_name": f"{LABEL}-g1-mysql",
                 "cluster": LABEL},
                {"service_id": "sibling-mysql", "service_name": f"{SIBLING}-g1-mysql",
                 "cluster": SIBLING},
            ],
            "proxysql": [
                {"service_id": "sibling-proxysql",
                 "service_name": f"{SIBLING}-px1-proxysql", "cluster": SIBLING},
            ],
            "external": [
                # Usluga bez pola `cluster` — rozstrzyga dokladna nazwa z inwentarza.
                {"service_id": "own-node-exporter",
                 "service_name": f"{LABEL}-g2-node-exporter"},
                {"service_id": "sibling-node-exporter",
                 "service_name": f"{SIBLING}-g2-node-exporter"},
            ],
            "postgresql": [],
        }
        expression = self._fact_expression("pmm_cluster_service_ids")
        selected = eval(  # noqa: S307
            self._render(expression, pmm_dereg_services=_Response(services))
        )
        self.assertEqual(sorted(selected), ["own-mysql", "own-node-exporter"])

    def test_sibling_tenant_nodes_survive(self):
        nodes = {
            "generic": [
                {"node_id": "own-node", "node_name": f"{LABEL}-g1",
                 "custom_labels": {"cluster": LABEL}},
                {"node_id": "sibling-node", "node_name": f"{SIBLING}-g1",
                 "custom_labels": {"cluster": SIBLING}},
                # Wezel z lokalnym agentem: `pmm-admin config` nie nadaje etykiety.
                {"node_id": "own-agent-node", "node_name": f"{LABEL}-px1",
                 "custom_labels": {}},
                {"node_id": "sibling-agent-node", "node_name": f"{SIBLING}-px1",
                 "custom_labels": {}},
            ],
            "container": [],
            "remote": [],
        }
        expression = self._fact_expression("pmm_cluster_node_ids")
        selected = eval(  # noqa: S307
            self._render(expression, pmm_dereg_nodes=_Response(nodes))
        )
        self.assertEqual(sorted(selected), ["own-agent-node", "own-node"])

    def test_expected_names_come_from_inventory_groups(self):
        """Nazwy musza byc wyprowadzone z grup inwentarza, nie z wolnego wzorca."""
        play_vars = self.play["vars"]
        rendered_hosts = self.env.from_string(play_vars["pmm_dereg_hosts"]).render(
            groups={"galera": ["g1", "g2"], "proxysql": ["px1"]}
        )
        self.assertEqual(sorted(eval(rendered_hosts)), ["g1", "g2", "px1"])  # noqa: S307


if __name__ == "__main__":
    unittest.main()
