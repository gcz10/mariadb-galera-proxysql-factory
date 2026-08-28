"""Kontrakt bramki tożsamości komponentu w f13 remove-node (audyt T4-light).

Plan (f13_remove_node_plan.yml) zapisuje `cluster_state_uuid`; executor
MUSI porównać go z żywym stanem klastra przed operacją destrukcyjną.
Bez tego plan wygenerowany dla komponentu X nadal odblokowywał remove-node
po przebudowie komponentu (restart PC/Bootstrap — state UUID się zmienia).

Test WYKONUJE warunki asserta z playbooka (jinja na macierzy stdout/plan),
nie pinuje tekstu — tej samej zasadzie co ClusterRecoverSelectionLogicTests.
"""

import re
import unittest
from pathlib import Path

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "playbooks" / "f13_remove_node.yml"

UUID_MATCH = "9a1b2c3d-1111-2222-3333-444455556666"
UUID_OTHER = "0f0e0d0c-aaaa-bbbb-cccc-ddddeeeeffff"


def blocking_assert_conditions():
    """`that:` z zadania blokującego remove-node (play 0, ostatni assert)."""
    plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    play0 = plays[0]
    for task in play0["tasks"]:
        name = task.get("name", "")
        if name.startswith("Blokuj remove-node"):
            return task["ansible.builtin.assert"]["that"]
    raise AssertionError("brak zadania 'Blokuj remove-node...' w play 0 f13")


def render_conditions(conditions, stdout, plan_uuid):
    """Wykonaj warunki asserta jak Ansible: True = przechodzi, False = blokuje."""
    env = jinja2.Environment()
    env.tests["search"] = lambda value, pattern: re.search(pattern, value) is not None
    context = {
        "f13_health_before": {"stdout": stdout},
        "f13_plan": {"cluster_state_uuid": plan_uuid},
        "groups": {"galera": ["gnode1", "gnode2", "gnode3"]},
    }
    rendered = []
    for condition in conditions:
        template = env.from_string("{{ " + condition + " }}")
        # render zwraca STRING "True"/"False" — bool("False") w Pythonie to
        # True, wiec porownujemy z tekstem (Ansible castuje wynik jawnie).
        rendered.append(template.render(**context).strip().lower() == "true")
    return rendered


def healthy_stdout(uuid):
    return (
        "wsrep_local_state\t4\n"
        "wsrep_cluster_status\tPrimary\n"
        "wsrep_ready\tON\n"
        "wsrep_cluster_size\t3\n"
        "wsrep_cluster_state_uuid\t" + uuid + "\n"
    )


class F13PlanIdentityGateTests(unittest.TestCase):
    def setUp(self):
        self.conditions = blocking_assert_conditions()

    def test_gate_compares_plan_state_uuid_with_live_cluster(self):
        """Bramka bez porównania state UUID = teflon (dokładnie defekt z audytu)."""
        joined = "\n".join(self.conditions)
        self.assertIn("wsrep_cluster_state_uuid", joined)
        self.assertIn("f13_plan.cluster_state_uuid", joined)

    def test_same_component_passes(self):
        results = render_conditions(self.conditions, healthy_stdout(UUID_MATCH), UUID_MATCH)
        self.assertTrue(all(results), f"zdrowy klaster w komponencie planu odrzucony: {results}")

    def test_component_rebuilt_since_plan_blocks(self):
        """Klaster zdrowy, ale w INNYM komponencie niż plan — MUSI zablokować."""
        results = render_conditions(self.conditions, healthy_stdout(UUID_OTHER), UUID_MATCH)
        uuid_conditions = [
            ok for condition, ok in zip(self.conditions, results)
            if "wsrep_cluster_state_uuid" in condition
        ]
        self.assertFalse(all(uuid_conditions), "plan starego komponentu przeszedł bramkę")

    def test_plan_without_uuid_blocks(self):
        """Plan bez state UUID (ręcznie podrobiony/stary) nie może przejść."""
        results = render_conditions(self.conditions, healthy_stdout(UUID_MATCH), "")
        self.assertFalse(all(results), "plan bez state UUID przeszedł bramkę")


if __name__ == "__main__":
    unittest.main()
