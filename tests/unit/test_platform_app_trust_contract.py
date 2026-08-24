"""Frontend CA wspolnego endpointu jest wlasnoscia platformy, nie najemcy."""

import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLATFORM_PROXYSQL = REPO / "playbooks" / "platform_proxysql.yml"
TENANT_APP = REPO / "playbooks" / "app_host.yml"


class PlatformAppTrustOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plays = yaml.safe_load(PLATFORM_PROXYSQL.read_text(encoding="utf-8"))
        cls.app_play = next(play for play in cls.plays if play.get("hosts") == "app")
        cls.tasks = cls.app_play["tasks"]
        cls.by_name = {task.get("name"): task for task in cls.tasks}

    def test_platform_guards_and_installs_frontend_ca_on_app(self):
        conditions = "\n".join(
            str(condition)
            for task in self.app_play.get("pre_tasks", [])
            for condition in (task.get("ansible.builtin.assert") or {}).get("that", [])
        )
        self.assertIn("platform.name is defined", conditions)
        self.assertIn("galera is not defined", conditions)

        copy = self.by_name["APP — rozprowadz CA wspolnego endpointu ProxySQL"]
        args = copy["ansible.builtin.copy"]
        self.assertIn("proxysql.frontend_tls.ca_reference", str(args["src"]))
        self.assertEqual(args["dest"], "/etc/mysql/app/shared/proxysql-ca.pem")
        self.assertEqual(args["mode"], "0644")

    def test_tenant_app_playbook_does_not_own_shared_frontend_ca(self):
        tenant = TENANT_APP.read_text(encoding="utf-8")
        self.assertNotIn("/etc/mysql/app/shared", tenant)
        self.assertNotIn("proxysql.frontend_tls.ca_reference", tenant)


class PlatformPoolMetricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lab = REPO / "tests" / "lab"
        sys.path.insert(0, str(lab))
        cls.probe = SourceFileLoader(
            "probe_platform", str(lab / "probe-platform.py")
        ).load_module()

    def test_zero_tenants_is_a_measured_clean_state(self):
        failures = []
        measured = self.probe.check_pool_metric(
            {"grp1": {"GROUPS": "0"}, "grp2": {"GROUPS": "0"}},
            [],
            120,
            failures,
        )
        self.assertFalse(measured)
        self.assertEqual(failures, [])

    def test_present_tenants_require_fresh_pool_metric(self):
        failures = []
        measured = self.probe.check_pool_metric(
            {"grp1": {"GROUPS": "1"}, "grp2": {"GROUPS": "1"}},
            [],
            120,
            failures,
        )
        self.assertTrue(measured)
        self.assertTrue(failures)


if __name__ == "__main__":
    unittest.main()
