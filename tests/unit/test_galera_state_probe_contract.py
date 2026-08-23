"""Kontrakt klasyfikatora stanu Galery: nic niejednoznacznego nie przechodzi.

Sonda `SHOW STATUS LIKE 'wsrep_cluster_status'` moze skonczyc sie na piec
sposobow, a stary klasyfikator znal tylko dwa: "stdout zawiera Primary" oraz
"stdout niezdefiniowany". Host osiagalny po SSH, ktorego klient bazy zwrocil
rc != 0 i pusty stdout, nie trafial do zadnej kategorii — znikal z decyzji.

Konsekwencje byly przeciwstawne i obie zle:
  * bootstrap.yml — niewidoczny wezel z zywym Primary => drugi Primary,
  * cluster_recover.yml — niewidoczny stan => zatrzymanie zdrowego klastra.

Testy renderuja WYRAZENIA WYJETE Z PLAYBOOKA, nie ich kopie.
"""

import copy
import re
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[2]
PROBE_TASKS = REPO / "playbooks" / "tasks" / "galera_state_probe.yml"
BOOTSTRAP = REPO / "playbooks" / "bootstrap.yml"
RECOVER = REPO / "playbooks" / "cluster_recover.yml"

NODES = ["gnode1", "gnode2", "gnode3"]
PRIMARY_STDOUT = "wsrep_cluster_status\tPrimary"
NON_PRIMARY_STDOUT = "wsrep_cluster_status\tnon-Primary"
SOCKET_ERROR = "ERROR 2002 (HY000): Can't connect to local server through socket"


def result(node, **overrides):
    payload = {"item": node}
    payload.update(overrides)
    return payload


def primary(node):
    return result(node, rc=0, stdout=PRIMARY_STDOUT, stderr="")


def non_primary(node):
    return result(node, rc=0, stdout=NON_PRIMARY_STDOUT, stderr="")


def down_verified(node):
    return result(node, rc=1, stdout="", stderr=SOCKET_ERROR)


def unreachable(node):
    return result(node, unreachable=True, msg="ssh timeout")


def probe_error(node):
    """Osiagalny po SSH, ale sonda nie odpowiedziala jednoznacznie."""
    return result(node, rc=1, stdout="", stderr="ERROR 1045 (28000): Access denied")


def empty_answer(node):
    """rc=0, ale sonda nic nie zwrocila — brak dowodu to nie dowod braku."""
    return result(node, rc=0, stdout="", stderr="")


def skipped_by_check_mode(node):
    """Ansible w --check zwraca syntetyczne rc=0 i puste stdout."""
    return result(
        node,
        rc=0,
        stdout="",
        stderr="",
        skipped=True,
        msg="Command would have run if not in check mode",
    )




class GaleraStateClassifierTests(unittest.TestCase):
    """Kazdy ksztalt wyniku laduje dokladnie w jednej kategorii."""

    @classmethod
    def setUpClass(cls):
        cls.tasks = yaml.safe_load(PROBE_TASKS.read_text(encoding="utf-8"))
        cls.by_name = {task.get("name"): task for task in cls.tasks}
        cls.set_facts = [
            task["ansible.builtin.set_fact"]
            for task in cls.tasks
            if "ansible.builtin.set_fact" in task
        ]
        cls.facts = {
            key: value for block in cls.set_facts for key, value in block.items()
        }
        cls.env = jinja2.Environment()
        cls.env.filters["intersect"] = lambda left, right: [
            item for item in left if item in right
        ]
        cls.env.filters["difference"] = lambda left, right: [
            item for item in left if item not in right
        ]
        # `search` to test Ansible, nie Jinja — rejestrujemy go z ta sama
        # semantyka (re.search), zeby ocenic wyrazenie z playbooka, nie kopie.
        cls.env.tests["search"] = lambda value, pattern: re.search(pattern, value) is not None

    def classify_states(self, results):
        facts = self.facts
        context = {
            "galera_state_probe": {"results": results},
            "groups": {"galera": NODES},
        }
        resolved = {}
        for key in (
            "galera_state_primary",
            "galera_state_non_primary",
            "galera_state_down_verified",
            "galera_state_unreachable",
            "galera_state_unknown",
        ):
            rendered = self.env.from_string(str(facts[key])).render(
                **context, **resolved
            )
            resolved[key] = yaml.safe_load(rendered)
        return resolved

    def test_probe_never_fails_the_play_on_its_own(self):
        probe = self.by_name["Galera — sonduj wsrep_cluster_status na kazdym wezle"]
        self.assertFalse(probe["failed_when"])
        self.assertTrue(probe["ignore_unreachable"])
        self.assertFalse(probe["changed_when"])
        self.assertEqual(probe["loop"], "{{ groups['galera'] }}")

    def test_unknown_is_derived_after_the_other_states(self):
        """set_fact nie gwarantuje widocznosci kluczy z tego samego wywolania."""
        unknown_block = next(
            index
            for index, block in enumerate(self.set_facts)
            if "galera_state_unknown" in block
        )
        for key in (
            "galera_state_primary",
            "galera_state_non_primary",
            "galera_state_down_verified",
            "galera_state_unreachable",
        ):
            source = next(
                index for index, block in enumerate(self.set_facts) if key in block
            )
            self.assertLess(source, unknown_block, key)

    def test_every_result_shape_lands_in_exactly_one_state(self):
        states = self.classify_states(
            [primary("gnode1"), down_verified("gnode2"), unreachable("gnode3")]
        )
        self.assertEqual(states["galera_state_primary"], ["gnode1"])
        self.assertEqual(states["galera_state_down_verified"], ["gnode2"])
        self.assertEqual(states["galera_state_unreachable"], ["gnode3"])
        self.assertEqual(states["galera_state_unknown"], [])

        buckets = [
            node
            for key, nodes in states.items()
            if key != "galera_state_unknown"
            for node in nodes
        ]
        self.assertEqual(sorted(buckets), sorted(NODES))

    def test_reachable_probe_error_is_unknown_not_silence(self):
        states = self.classify_states(
            [primary("gnode1"), down_verified("gnode2"), probe_error("gnode3")]
        )
        self.assertEqual(states["galera_state_unknown"], ["gnode3"])
        self.assertNotIn("gnode3", states["galera_state_down_verified"])
        self.assertNotIn("gnode3", states["galera_state_unreachable"])

    def test_non_primary_is_distinguished_from_stopped(self):
        states = self.classify_states(
            [non_primary("gnode1"), down_verified("gnode2"), down_verified("gnode3")]
        )
        self.assertEqual(states["galera_state_non_primary"], ["gnode1"])
        self.assertEqual(
            states["galera_state_down_verified"], ["gnode2", "gnode3"]
        )
        self.assertEqual(states["galera_state_unknown"], [])

    def test_clean_cold_cluster_has_no_unknown(self):
        states = self.classify_states([down_verified(node) for node in NODES])
        self.assertEqual(states["galera_state_down_verified"], NODES)
        self.assertEqual(states["galera_state_unknown"], [])

    def test_empty_answer_is_unknown_not_non_primary(self):
        """rc=0 z pustym stdout to brak odpowiedzi, nie dowod braku Primary."""
        states = self.classify_states(
            [empty_answer("gnode1"), primary("gnode2"), non_primary("gnode3")]
        )
        self.assertEqual(states["galera_state_unknown"], ["gnode1"])
        self.assertNotIn("gnode1", states["galera_state_non_primary"])

    def test_check_mode_skip_is_unknown(self):
        """--check zwraca syntetyczne rc=0; zywy Primary nie moze zniknac."""
        states = self.classify_states(
            [skipped_by_check_mode(node) for node in NODES]
        )
        self.assertEqual(states["galera_state_unknown"], NODES)
        self.assertEqual(states["galera_state_non_primary"], [])


class DestructiveTransitionGuardTests(unittest.TestCase):
    """UNKNOWN blokuje bootstrap i cold recovery — bez flagi wyjscia."""

    @classmethod
    def setUpClass(cls):
        cls.bootstrap = yaml.safe_load(BOOTSTRAP.read_text(encoding="utf-8"))[0]
        cls.recover = yaml.safe_load(RECOVER.read_text(encoding="utf-8"))[0]

    def section(self, play, key):
        return play.get(key) or []

    def asserts(self, tasks):
        return [
            " ".join(str(item) for item in task["ansible.builtin.assert"]["that"])
            for task in tasks
            if "ansible.builtin.assert" in task
        ]

    def test_bootstrap_includes_shared_probe_before_guards(self):
        pre = self.section(self.bootstrap, "pre_tasks")
        names = [task.get("name") for task in pre]
        include = next(
            index
            for index, task in enumerate(pre)
            if str(task.get("ansible.builtin.include_tasks", "")).endswith(
                "galera_state_probe.yml"
            )
        )
        guard = next(
            index
            for index, task in enumerate(pre)
            if "galera_state_unknown" in str(task.get("ansible.builtin.assert", ""))
        )
        self.assertLess(include, guard, names)

    def test_bootstrap_refuses_unknown_and_primary(self):
        conditions = " ".join(self.asserts(self.section(self.bootstrap, "pre_tasks")))
        self.assertIn("galera_state_unknown | length == 0", conditions)
        self.assertIn("galera_state_primary | length == 0", conditions)

    def test_unknown_cannot_be_waived_by_confirm_all_down(self):
        unknown_guard = next(
            task
            for task in self.section(self.bootstrap, "pre_tasks")
            if "galera_state_unknown" in str(task.get("ansible.builtin.assert", ""))
        )
        expression = " ".join(unknown_guard["ansible.builtin.assert"]["that"])
        self.assertNotIn("bootstrap_confirm_all_down", expression)
        self.assertNotIn("or", expression)

    def test_recover_refuses_unknown_before_stopping_nodes(self):
        tasks = self.section(self.recover, "tasks")
        conditions = " ".join(self.asserts(tasks))
        self.assertIn("galera_state_unknown | length == 0", conditions)
        self.assertIn("galera_state_primary | length == 0", conditions)
        self.assertIn("galera_state_unreachable | length == 0", conditions)




class DoubleBootstrapProbeTests(unittest.TestCase):
    """Statyczna bramka ISC-65 musi widziec guard w pliku wspolnym i tylko wtedy."""

    @classmethod
    def setUpClass(cls):
        cls.probe = SourceFileLoader(
            "probe_no_double_bootstrap",
            str(REPO / "tests" / "validation" / "probe-no-double-bootstrap.py"),
        ).load_module()
        cls.play = yaml.safe_load(BOOTSTRAP.read_text(encoding="utf-8"))[0]
        cls.base = REPO / "playbooks"

    def without_assert(self, needle):
        play = copy.deepcopy(self.play)
        play["pre_tasks"] = [
            task
            for task in play["pre_tasks"]
            if needle not in str(task.get("ansible.builtin.assert", ""))
        ]
        return play

    def test_probe_accepts_guard_defined_in_included_file(self):
        self.assertTrue(
            self.probe.has_existing_primary_guard(self.play, self.base)
        )

    def test_probe_rejects_missing_unknown_state_guard(self):
        self.assertFalse(
            self.probe.has_existing_primary_guard(
                self.without_assert("galera_state_unknown"), self.base
            ),
            "brak asercji o stanie nieznanym musi byc naruszeniem ISC-65",
        )

    def test_probe_rejects_missing_primary_guard(self):
        self.assertFalse(
            self.probe.has_existing_primary_guard(
                self.without_assert("galera_state_primary"), self.base
            )
        )


if __name__ == "__main__":
    unittest.main()
