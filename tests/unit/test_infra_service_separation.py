#!/usr/bin/env python3
"""Monitoring, magazyn kopii i relay maja rozne cykle zycia.

POWSTAL PO REALNEJ STRACIE (2026-08-24). PMM, MinIO i Maildev byly jednym,
nierozdzielnym zestawem na hoscie `infra`. Przebudowa monitoringu oznaczala
zniszczenie hosta, a wraz z nim magazynu kopii CALEJ floty — mimo ze magazyn
mial zostac nietkniety i moze rownie dobrze stac poza laboratorium.

Kontrakt: sklad uslug jest deklaracja platformy (`platform.infra.services`),
a szablon compose renderuje WYLACZNIE to, co zadeklarowano — razem z wolumenami
i sekretami. Brak deklaracji zachowuje sie jak dawniej (pelny zestaw).
Deklaracja jest tez kontraktem wykonania: zadeklarowane uslugi nie moga
"przejsc" bez hosta `infra`, a produkcja (zewnetrzny PMM/S3/SMTP) nie hostuje
zadnych uslug laboratoryjnych i zawsze deklaruje jawnie `infra.services: []`.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import jinja2
import yaml
from jinja2 import Template

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "roles" / "infra_services" / "templates" / "compose.yml.j2"
PLAYBOOK = REPO / "playbooks" / "infra_services.yml"
SCHEMA = REPO / "platform" / "schema" / "platform.schema.json"

LOCK = {
    "pmm": {"image": "percona/pmm-server:3.9.1", "image_digest": "sha256:" + "a" * 64},
    "minio": {"image": "minio/minio:RELEASE", "image_digest": "sha256:" + "b" * 64},
    "maildev": {"image": "maildev/maildev:2.2.1"},
}


def render(services):
    tmpl = Template(COMPOSE.read_text(encoding="utf-8"))
    # `to_json` dostarcza Ansible, nie Jinja — bez niego szablon nie renderuje sie
    # poza playbookiem, a kontrakt ma sprawdzac dokladnie ten plik, ktory idzie na host.
    tmpl.environment.filters["to_json"] = json.dumps
    return yaml.safe_load(
        tmpl.render(
            infra_services=services,
            lock=LOCK,
            platform={"name": "t"},
            ansible_host="10.0.0.1",
            pmm_admin_password="haslo-admina",
            minio_root_user="root-user",
            minio_root_password="haslo-minio",
        )
    )


class InfraServiceSeparationTests(unittest.TestCase):
    def test_full_set_renders_all_three(self):
        out = render(["pmm", "minio", "maildev"])
        self.assertEqual(sorted(out["services"]), ["maildev", "minio", "pmm-server"])
        self.assertEqual(sorted(out["volumes"]), ["minio-data", "pmm-data"])

    def test_monitoring_only_leaves_no_storage_behind(self):
        """Najwazniejszy przypadek: PMM bez MinIO."""
        out = render(["pmm", "maildev"])
        self.assertNotIn("minio", out["services"])
        self.assertNotIn("minio-data", out["volumes"], "wolumen magazynu nie moze powstac")
        rendered = json.dumps(out)
        self.assertNotIn("MINIO_ROOT", rendered, "sekret magazynu nie moze trafic do compose")

    def test_storage_only_leaves_no_monitoring_behind(self):
        out = render(["minio"])
        self.assertEqual(list(out["services"]), ["minio"])
        self.assertNotIn("pmm-data", out["volumes"])

    def test_pmm_without_relay_disables_smtp(self):
        """Bez relaya alert nie ma dokad wyjsc — konfiguracja musi to mowic wprost."""
        out = render(["pmm"])
        env = out["services"]["pmm-server"]["environment"]
        self.assertEqual(env["GF_SMTP_ENABLED"], "false")
        self.assertNotIn("depends_on", out["services"]["pmm-server"])

    def test_schema_constrains_the_service_names(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        services = schema["properties"]["platform"]["properties"]["infra"]["properties"]["services"]
        self.assertEqual(sorted(services["items"]["enum"]), ["maildev", "minio", "pmm"])
        self.assertTrue(services["uniqueItems"])


UNDECLARED = object()
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")


def platform_definition(environment, services=UNDECLARED):
    platform = {"name": "t", "environment": environment, "rocky_linux_major": 10}
    if services is not UNDECLARED:
        platform["infra"] = {"services": services}
    return platform


def host_pattern(expression, environment, services=UNDECLARED):
    """Renderuje wzorzec hostow tak, jak robi to Ansible (lancuchowy Undefined)."""
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    return env.from_string(expression).render(platform=platform_definition(environment, services))


class InfraHostSelectionTests(unittest.TestCase):
    """Play'e mutujace host infra adresuja go WYLACZNIE dla zadeklarowanych uslug."""

    @classmethod
    def setUpClass(cls):
        cls.plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
        firewall = next(
            play for play in cls.plays if play.get("ansible.builtin.import_playbook") == "firewall.yml"
        )
        service = next(
            play for play in cls.plays
            if play.get("name") == "Infra — uslugi warstwy wspolnej zadeklarowane przez platforme"
        )
        cls.expressions = {
            "firewall": firewall["vars"]["firewall_target_hosts"],
            "services": service["hosts"],
        }

    def test_hosted_services_in_lab_target_the_infra_host(self):
        for name, expression in self.expressions.items():
            for environment in ("laboratory", "staging"):
                for services in (["pmm", "minio", "maildev"], ["minio"], UNDECLARED):
                    with self.subTest(play=name, environment=environment, services=services):
                        self.assertEqual(host_pattern(expression, environment, services), "infra")

    def test_no_service_declared_touches_no_host(self):
        """`all:!all` = pusty zbior hostow: play jest pomijany, nic sie nie laczy."""
        for name, expression in self.expressions.items():
            for environment in ("laboratory", "staging", "production"):
                with self.subTest(play=name, environment=environment, services=[]):
                    self.assertEqual(host_pattern(expression, environment, []), "all:!all")

    def test_production_never_targets_the_infra_host(self):
        """Nawet zadeklarowane (bledne) uslugi nie wybieraja hosta w produkcji."""
        for name, expression in self.expressions.items():
            for services in ([], ["pmm"], ["pmm", "minio", "maildev"], UNDECLARED):
                with self.subTest(play=name, services=services):
                    self.assertEqual(host_pattern(expression, "production", services), "all:!all")



@unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostepny w PATH")
class InfraDeclarationGateBehaviorTests(unittest.TestCase):
    """Bramka kontrolera uruchamiana naprawde — bez laczenia sie z jakimkolwiek hostem."""

    @classmethod
    def setUpClass(cls):
        plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
        cls.workdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.workdir.cleanup)
        cls.gate_path = Path(cls.workdir.name, "gate.yml")
        cls.gate_path.write_text(yaml.safe_dump([plays[0]], allow_unicode=True), encoding="utf-8")

    def run_gate(self, platform, infra_hosts):
        children = {"proxysql": {"hosts": {"p1": {"ansible_host": "192.0.2.2"}}}}
        if infra_hosts:
            children["infra"] = {
                "hosts": {f"i{n}": {"ansible_host": f"192.0.2.{10 + n}"} for n in range(infra_hosts)}
            }
        inventory = Path(self.workdir.name, "inventory.yml")
        inventory.write_text(yaml.safe_dump({"all": {"children": children}}), encoding="utf-8")
        variables = Path(self.workdir.name, "platform.json")
        variables.write_text(json.dumps({"platform": platform}), encoding="utf-8")
        return subprocess.run(
            [ANSIBLE_PLAYBOOK, "-i", str(inventory), "-e", f"@{variables}", str(self.gate_path)],
            cwd=REPO,
            capture_output=True,
            encoding="utf-8",
            env=dict(os.environ, ANSIBLE_NOCOLOR="1"),
            timeout=120,
        )

    def test_production_rejects_hosted_services_even_without_an_infra_host(self):
        for infra_hosts in (0, 1):
            for services in (["pmm"], ["pmm", "minio", "maildev"], UNDECLARED):
                with self.subTest(infra_hosts=infra_hosts, services=services):
                    proc = self.run_gate(platform_definition("production", services), infra_hosts)
                    self.assertNotEqual(proc.returncode, 0, proc.stdout)
                    self.assertIn("platform.infra.services: []", proc.stdout)

    def test_production_empty_declaration_is_an_explicit_measured_noop(self):
        for infra_hosts in (0, 1):
            with self.subTest(infra_hosts=infra_hosts):
                proc = self.run_gate(platform_definition("production", []), infra_hosts)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("INFRA NO-OP", proc.stdout)
                self.assertIn(f"hostów infra w inventory {infra_hosts}", proc.stdout)

    def test_declared_services_without_exactly_one_infra_host_do_not_pass(self):
        """Play `hosts: infra` bez hosta konczy sie kodem 0 — to nie jest wdrozenie."""
        for services in (["pmm", "minio", "maildev"], UNDECLARED):
            with self.subTest(services=services):
                proc = self.run_gate(platform_definition("laboratory", services), 0)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("wymagany 1", proc.stdout)
                self.assertNotIn("INFRA NO-OP", proc.stdout)

    def test_lab_with_one_infra_host_passes_the_gate_unchanged(self):
        proc = self.run_gate(platform_definition("laboratory", ["pmm", "minio", "maildev"]), 1)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("INFRA NO-OP", proc.stdout)

    def test_lab_empty_declaration_is_a_noop_too(self):
        proc = self.run_gate(platform_definition("laboratory", []), 1)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("INFRA NO-OP", proc.stdout)


if __name__ == "__main__":
    unittest.main()
