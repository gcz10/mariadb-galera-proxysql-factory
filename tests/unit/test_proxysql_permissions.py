"""Unit tests verifying ProxySQL file permissions in playbooks (ISC-45, CWE-732).

Ensures that:
- /var/lib/proxysql is restricted to 0700 (proxysql:proxysql).
- /etc/proxysql.cnf is 0640 (root:proxysql) so the daemon user can read it on start.
- /var/lib/proxysql/proxysql.db is 0600 (proxysql:proxysql).
- /etc/proxysql/admin-check.cnf is 0600 (root:root).
"""

import unittest
import yaml


class ProxySQLFilePermissionsTests(unittest.TestCase):
    """Tests file permissions in playbooks configuring the ProxySQL pair.

    Instance-level files belong to the platform layer (platform_proxysql.yml);
    f7_proxysql.yml is the tenant registration playbook. The invariants are
    about the shared instance, so every task is looked up across both files —
    whichever of them owns the task must keep the exact permission contract.
    """

    PLAYBOOKS = (
        "playbooks/platform_proxysql.yml",
        "playbooks/f7_proxysql.yml",
    )

    def setUp(self):
        self.plays = []
        for path in self.PLAYBOOKS:
            with open(path, encoding="utf-8") as f:
                self.plays.extend(yaml.safe_load(f))

    def get_file_task(self, target_path: str):
        for play in self.plays:
            for task in play.get("tasks", []):
                file_mod = task.get("ansible.builtin.file") or task.get("file") or {}
                copy_mod = task.get("ansible.builtin.copy") or task.get("copy") or {}
                dest = file_mod.get("path") or file_mod.get("dest") or copy_mod.get("dest")
                if dest == target_path:
                    return {**file_mod, **copy_mod}
        return None

    def test_var_lib_proxysql_permissions(self):
        task = self.get_file_task("/var/lib/proxysql")
        self.assertIsNotNone(task, "Task securing /var/lib/proxysql must exist")
        self.assertEqual(task.get("owner"), "proxysql")
        self.assertEqual(task.get("group"), "proxysql")
        self.assertEqual(task.get("mode"), "0700")

    def test_etc_proxysql_cnf_permissions(self):
        task = self.get_file_task("/etc/proxysql.cnf")
        self.assertIsNotNone(task, "Task securing /etc/proxysql.cnf must exist")
        self.assertEqual(task.get("owner"), "root")
        self.assertEqual(task.get("group"), "proxysql")
        self.assertEqual(task.get("mode"), "0640")

    def test_proxysql_db_permissions(self):
        task = self.get_file_task("/var/lib/proxysql/proxysql.db")
        self.assertIsNotNone(task, "Task securing /var/lib/proxysql/proxysql.db must exist")
        self.assertEqual(task.get("owner"), "proxysql")
        self.assertEqual(task.get("group"), "proxysql")
        self.assertEqual(task.get("mode"), "0600")

    def test_admin_check_cnf_permissions(self):
        task = self.get_file_task("/etc/proxysql/admin-check.cnf")
        self.assertIsNotNone(task, "Task deploying /etc/proxysql/admin-check.cnf must exist")
        self.assertEqual(task.get("owner"), "root")
        self.assertEqual(task.get("group"), "root")
        self.assertEqual(task.get("mode"), "0600")


if __name__ == "__main__":
    unittest.main()
