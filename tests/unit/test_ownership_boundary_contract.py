"""Kontrakt granicy wlasnosci: najemca nie mutuje hostow warstwy wspolnej.

Najemca deklaruje wspolne hosty w swoim inventory (fcp1/fcp2/fcinfra/fcapp),
bo musi sie do nich laczyc — rejestruje hostgroupy w ProxySQL i rozdaje CA.
Deklaracja nie jest jednak wlasnoscia: polityka firewalld tych hostow nalezy
do warstwy wspolnej. Bez bramki `make cluster-deploy` przepisywal public.xml
na fcp1/fcp2 CIDR-ami biezacego najemcy i przeladowywal firewalld.
"""

import re
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[2]
FIREWALL_PLAYBOOK = REPO / "playbooks" / "firewall.yml"
MAKEFILE = REPO / "Makefile"

SHARED_GROUPS = ["proxysql", "infra", "app"]
TENANT_GROUPS = ["galera", "restore"]


def makefile_recipes():
    """Zwroc {target: [logiczne linie recepty]} — kontynuacje sklejone."""
    recipes = {}
    current = None
    pending = ""
    for raw in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("\t"):
            if current is None:
                continue
            pending += raw.strip()
            if pending.endswith("\\"):
                pending = pending[:-1] + " "
                continue
            recipes[current].append(pending)
            pending = ""
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+):(?!=)", raw)
        current = match.group(1) if match else None
        if current:
            recipes.setdefault(current, [])
    return recipes


class TenantFirewallScopeTests(unittest.TestCase):
    """Cele najemcy moga dotykac wylacznie wlasnych wezlow."""

    @classmethod
    def setUpClass(cls):
        cls.recipes = makefile_recipes()

    def firewall_invocations(self, target):
        return [
            line
            for line in self.recipes.get(target, [])
            if "playbooks/firewall.yml" in line
        ]

    def test_tenant_targets_scope_firewall_to_own_nodes(self):
        for target in ("cluster-deploy", "cluster-firewall"):
            invocations = self.firewall_invocations(target)
            self.assertTrue(invocations, f"{target} nie uruchamia firewall.yml")
            for line in invocations:
                self.assertIn(
                    "firewall_target_hosts=galera:restore",
                    line,
                    f"{target} musi ograniczyc firewall do wezlow najemcy",
                )

    def test_no_tenant_target_writes_host_policy_on_shared_nodes(self):
        """Najemca moze uzywac wspolnych hostow (rejestracja w ProxySQL, CA),
        ale nie moze przepisywac ich polityki hosta."""
        for target, lines in self.recipes.items():
            if not target.startswith("cluster-"):
                continue
            for line in lines:
                for playbook in re.findall(r"playbooks/[A-Za-z0-9_./-]+\.yml", line):
                    path = REPO / playbook
                    if not path.exists():
                        continue
                    body = path.read_text(encoding="utf-8")
                    if "firewalld/zones/public.xml" not in body:
                        continue
                    self.assertIn(
                        "firewall_target_hosts=",
                        line,
                        f"{target} zapisuje polityke hosta bez jawnego zakresu",
                    )
                    scope = re.search(r"firewall_target_hosts=(\S+)", line).group(1)
                    self.assertFalse(
                        set(scope.split(":")) & set(SHARED_GROUPS),
                        f"{target} celuje w warstwe wspolna: {scope}",
                    )


class FirewallOwnershipGuardTests(unittest.TestCase):
    """Bramka w playbooku dziala nawet przy recznym uruchomieniu."""

    @classmethod
    def setUpClass(cls):
        cls.plays = yaml.safe_load(FIREWALL_PLAYBOOK.read_text(encoding="utf-8"))
        cls.play = cls.plays[0]
        cls.guard = next(
            task
            for task in cls.play["pre_tasks"]
            if "warstwy wspolnej" in task.get("name", "")
        )
        cls.env = jinja2.Environment()
        cls.env.filters["intersect"] = lambda left, right: [
            item for item in left if item in right
        ]

    def evaluate(self, groups, config):
        expression = self.guard["ansible.builtin.assert"]["that"][0]
        rendered = self.env.from_string("{{ " + expression + " }}").render(
            group_names=groups,
            firewall_shared_groups=self.play["vars"]["firewall_shared_groups"],
            **config,
        )
        return rendered.strip().lower() == "true"

    def test_shared_host_requires_platform_definition(self):
        tenant = {"platform": {"rocky_linux_major": 9}, "galera": {"nodes_expected": 3}}
        shared = {"platform": {"name": "shared", "rocky_linux_major": 10}}

        self.assertFalse(
            self.evaluate(["proxysql"], tenant),
            "najemca nie moze przepisac firewalla wspolnego ProxySQL",
        )
        self.assertFalse(self.evaluate(["infra"], tenant))
        self.assertFalse(self.evaluate(["app"], tenant))
        self.assertTrue(self.evaluate(["proxysql"], shared))
        self.assertTrue(self.evaluate(["galera"], tenant))
        self.assertTrue(self.evaluate(["restore"], tenant))

    def test_guard_precedes_any_mutation(self):
        names = [task.get("name") for task in self.play["pre_tasks"]]
        self.assertEqual(names.index(self.guard["name"]), 0)
        actions = {
            key
            for task in self.play["pre_tasks"]
            for key in task
            if key.startswith("ansible.")
        }
        self.assertEqual(actions, {"ansible.builtin.assert"})


class PlatformFirewallOwnerTests(unittest.TestCase):
    """Warstwa wspolna ma wlasny cel firewalla i wywoluje go w buildzie."""

    @classmethod
    def setUpClass(cls):
        cls.recipes = makefile_recipes()
        cls.text = MAKEFILE.read_text(encoding="utf-8")

    def test_platform_firewall_target_covers_shared_groups(self):
        lines = self.recipes.get("platform-firewall", [])
        self.assertTrue(lines, "brak celu platform-firewall")
        joined = " ".join(lines)
        self.assertIn("playbooks/firewall.yml", joined)
        self.assertIn("$(PLATFORM_OPTS)", joined)
        self.assertIn("firewall_target_hosts=proxysql:infra:app", joined)

    def test_platform_firewall_is_phony_and_wired_into_build(self):
        phony = re.search(r"\.PHONY:([\s\S]*?)\n\n", self.text).group(1)
        self.assertIn("platform-firewall", phony.replace("\\", " "))
        build = " ".join(self.recipes.get("platform-build", []))
        self.assertIn("platform-firewall", build)


class FirewallProbeScopeTests(unittest.TestCase):
    """Sonda weryfikuje polityke tylko tych hostow, ktorych config jest wlascicielem."""

    @classmethod
    def setUpClass(cls):
        cls.probe = SourceFileLoader(
            "probe_firewall", str(REPO / "tests" / "lab" / "probe-firewall.py")
        ).load_module()

    def test_owned_groups_follow_config_ownership(self):
        tenant = {"cluster": {"name": "n17"}, "platform": {"rocky_linux_major": 9}}
        shared = {"platform": {"name": "shared", "rocky_linux_major": 10}}
        self.assertEqual(self.probe.owned_groups(tenant), tuple(TENANT_GROUPS))
        self.assertEqual(self.probe.owned_groups(shared), tuple(SHARED_GROUPS))


if __name__ == "__main__":
    unittest.main()
