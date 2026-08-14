"""Regression test for play-level vs task-level secret environment scoping (ISC-43).

Verifies that sensitive credentials (like PMM_AGENT_SERVER_PASSWORD) are scoped
strictly to the tasks that need them (with no_log: true) and never placed in
play-level environment blocks where unrelated tasks would inherit them.
"""

import unittest
import yaml


class SecretScopeTests(unittest.TestCase):
    def test_pmm_agent_secret_not_in_play_environment(self):
        with open("playbooks/f11_pmm_agent.yml", encoding="utf-8") as f:
            plays = yaml.safe_load(f)

        for play in plays:
            play_env = play.get("environment", {})
            self.assertNotIn(
                "PMM_AGENT_SERVER_PASSWORD",
                play_env,
                "PMM_AGENT_SERVER_PASSWORD must NOT be in play-level environment",
            )

    def test_pmm_agent_setup_task_has_secret_and_no_log(self):
        with open("playbooks/f11_pmm_agent.yml", encoding="utf-8") as f:
            plays = yaml.safe_load(f)

        found_setup_task = False
        for play in plays:
            for task in play.get("tasks", []):
                name = task.get("name", "")
                if "Zarejestruj wezel" in name or "pmm-agent setup" in name:
                    found_setup_task = True
                    task_env = task.get("environment", {})
                    self.assertIn(
                        "PMM_AGENT_SERVER_PASSWORD",
                        task_env,
                        "Task must define PMM_AGENT_SERVER_PASSWORD in task-level environment",
                    )
                    self.assertTrue(
                        task.get("no_log"),
                        "Task with secret environment must have no_log: true",
                    )

        self.assertTrue(found_setup_task, "pmm-agent setup task must be found")


if __name__ == "__main__":
    unittest.main()
