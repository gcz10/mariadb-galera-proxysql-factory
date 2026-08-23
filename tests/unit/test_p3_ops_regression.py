"""Regression tests for P3 Day-2 Ops improvements (ISC-65, ISC-23, graceful drain).

Verifies:
1. bootstrap.yml: strict anchored matching for wsrep_cluster_status=Primary
   (ensures non-Primary is never misclassified as live Primary).
2. f13_remove_node.yml: strict sequential ordering of:
   - OFFLINE_SOFT drain -> wait ConnUsed=0 -> DELETE FROM mysql_servers.
"""

import re
import unittest
import yaml


class BootstrapClassificationTests(unittest.TestCase):
    """Tests regex classification in bootstrap.yml (ISC-65)."""

    def test_anchored_regex_behavior(self):
        pattern = r"(?m)^wsrep_cluster_status\tPrimary$"

        primary_output = "wsrep_cluster_status\tPrimary\n"
        non_primary_output = "wsrep_cluster_status\tnon-Primary\n"
        arbitrary_output = "some_other_var\tPrimary\n"

        self.assertIsNotNone(re.search(pattern, primary_output))
        self.assertIsNone(re.search(pattern, non_primary_output))
        self.assertIsNone(re.search(pattern, arbitrary_output))

    def test_bootstrap_uses_anchored_regex_from_shared_classifier(self):
        with open("playbooks/bootstrap.yml", encoding="utf-8") as f:
            bootstrap = f.read()
        with open("playbooks/tasks/galera_state_probe.yml", encoding="utf-8") as f:
            classifier = f.read()

        # Klasyfikator zostal wyniesiony do pliku wspolnego (bootstrap +
        # cold recovery). Kotwiczony wzorzec musi byc w nim, a bootstrap musi
        # go faktycznie wlaczac — inaczej test pilnowalby martwego pliku.
        self.assertIn(
            "tasks/galera_state_probe.yml",
            bootstrap,
            "bootstrap.yml must include the shared Galera state classifier",
        )
        self.assertIn(
            "(?m)^wsrep_cluster_status\\tPrimary$",
            classifier,
            "classifier must use anchored regex for Primary detection",
        )


class RemoveNodeDrainSequenceTests(unittest.TestCase):
    """Tests graceful drain sequence in f13_remove_node.yml."""

    def setUp(self):
        with open("playbooks/f13_remove_node.yml", encoding="utf-8") as f:
            self.plays = yaml.safe_load(f)

    def test_drain_tasks_sequence_and_invariants(self):
        proxysql_play = None
        for play in self.plays:
            if play.get("hosts") == "proxysql":
                proxysql_play = play
                break

        self.assertIsNotNone(proxysql_play, "ProxySQL play in f13_remove_node.yml must exist")
        tasks = proxysql_play.get("tasks", [])

        offline_soft_idx = None
        conn_used_idx = None
        delete_idx = None

        for idx, task in enumerate(tasks):
            text = str(task)
            if "OFFLINE_SOFT" in text:
                offline_soft_idx = idx
            if "stats_mysql_connection_pool" in text and "ConnUsed" in text:
                conn_used_idx = idx
                until_expr = task.get("until", "")
                self.assertIn("== 0", str(until_expr), "ConnUsed task must wait until 0")
            if "DELETE FROM mysql_servers" in text:
                delete_idx = idx

        self.assertIsNotNone(offline_soft_idx, "Task setting OFFLINE_SOFT must exist")
        self.assertIsNotNone(conn_used_idx, "Task waiting for ConnUsed==0 must exist")
        self.assertIsNotNone(delete_idx, "Task deleting from mysql_servers must exist")

        self.assertLess(
            offline_soft_idx,
            conn_used_idx,
            "OFFLINE_SOFT must occur BEFORE waiting for active connections to drain",
        )
        self.assertLess(
            conn_used_idx,
            delete_idx,
            "Active connections must be drained BEFORE deleting backend from ProxySQL",
        )


if __name__ == "__main__":
    unittest.main()
