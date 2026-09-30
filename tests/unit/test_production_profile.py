"""Production declarations cannot soften the required transport policy."""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from test_cluster_schema_contract import canonical_cluster

REPO = Path(__file__).resolve().parents[2]
VALIDATOR = REPO / "tests" / "validation" / "validate-cluster-schema.py"
SCHEMA = REPO / "clusters" / "schema" / "cluster.schema.json"


def production_cluster():
    config = canonical_cluster()
    config["cluster"]["environment"] = "production"
    config["tls"] = {
        "mode": "full",
        "ca_reference": "pki/production/ca.pem",
        "certificate_reference": "pki/production/server-cert.pem",
        "private_key_reference": "pki/production/server-key.pem",
    }
    config["backup"]["s3"].update(endpoint="192.0.2.80:443", secure=True)
    config["backup"]["restore_test_schedule"] = "disabled"
    config["monitoring"]["pmm"].update(
        server_url="https://192.0.2.81",
        validate_certs=True,
        ca_reference="pki/production/pmm-ca.pem",
    )
    config["monitoring"]["alerts"] = {"email": "ops@factory.company"}
    config["mariadb_tuning"]["innodb_flush_log_at_trx_commit"] = 1
    return config


class ProductionClusterPolicyTests(unittest.TestCase):
    def validate(self, config, extra_groups=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "cluster.yml"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            inventory = {"all": {"children": {"galera": {"hosts": {
                f"gnode{number}": {"ansible_host": f"198.51.100.{number}",
                                   "galera_node_address": f"198.51.100.{number}"}
                for number in range(1, 4)
            }}}}}
            inventory["all"]["children"].update(extra_groups or {})
            (root / "inventory.yml").write_text(yaml.safe_dump(inventory), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(VALIDATOR), str(path), str(SCHEMA)],
                cwd=REPO, capture_output=True, text=True, timeout=30,
            )

    def test_production_rejects_disabled_database_tls(self):
        config = production_cluster()
        config["tls"] = {"mode": "disabled"}
        result = self.validate(config)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tls", result.stdout + result.stderr)

    def test_production_rejects_obsolete_durability_exception_field(self):
        config = production_cluster()
        config["mariadb_tuning"]["durability_risk_accepted"] = True
        result = self.validate(config)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("durability_risk_accepted", result.stdout + result.stderr)

    def test_safe_production_configuration_is_accepted(self):
        result = self.validate(production_cluster())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_preserves_safe_omitted_and_string_durability(self):
        for value in (None, "1"):
            with self.subTest(value=value):
                config = production_cluster()
                if value is None:
                    del config["mariadb_tuning"]["innodb_flush_log_at_trx_commit"]
                else:
                    config["mariadb_tuning"]["innodb_flush_log_at_trx_commit"] = value
                result = self.validate(config)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_rejects_each_unsafe_durability_mode(self):
        for value in (0, 2, "0", "2"):
            with self.subTest(value=value):
                config = production_cluster()
                config["mariadb_tuning"]["innodb_flush_log_at_trx_commit"] = value
                result = self.validate(config)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("innodb_flush_log_at_trx_commit", result.stdout + result.stderr)

    def test_laboratory_can_still_choose_reduced_durability(self):
        config = production_cluster()
        config["cluster"]["environment"] = "laboratory"
        config["versions"]["policy"] = "candidate"
        config["mariadb_tuning"]["innodb_flush_log_at_trx_commit"] = 0
        result = self.validate(config)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_rejects_loopback_pmm_server_identity(self):
        config = production_cluster()
        config["monitoring"]["pmm"]["server_url"] = "https://127.0.0.1"
        result = self.validate(config)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PMM", result.stdout + result.stderr)

    def test_production_rejects_each_transport_and_operational_bypass(self):
        cases = [
            (("versions", "policy"), "candidate"),
            (("tls", "require_secure_transport"), False),
            (("monitoring", "enabled"), False),
            (("monitoring", "pmm", "validate_certs"), False),
            (("monitoring", "pmm", "ca_reference"), ""),
            (("backup", "enabled"), False),
            (("backup", "s3", "secure"), False),
            (("backup", "encryption_enabled"), False),
            (("backup", "destination"), "filesystem"),
            (("backup", "restore_test_schedule"), "0 4 * * 0"),
            (("monitoring", "alerts", "email"), "ops@example.invalid"),
            (("monitoring", "alerts", "email"), "ops@localhost"),
            (("monitoring", "alerts", "email"), "ops@example.com"),
            (("monitoring", "alerts", "email"), "ops@team.example.org"),
        ]
        for path, value in cases:
            with self.subTest(path=path):
                config = production_cluster()
                node = config
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
                result = self.validate(config)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_rejects_backup_on_any_database_source_identity(self):
        for endpoint in ("198.51.100.2:443", "gnode3:443", "[::ffff:198.51.100.1]:443"):
            with self.subTest(endpoint=endpoint):
                config = production_cluster()
                config["backup"]["s3"]["endpoint"] = endpoint
                result = self.validate(config)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("production backup", result.stdout + result.stderr)

    def test_production_smb_requires_sealed_remote_storage(self):
        config = production_cluster()
        config["backup"]["destination"] = "smb"
        del config["backup"]["s3"]
        config["backup"]["smb"] = {
            "source": "//192.0.2.80/backups", "mount_point": "/mnt/backups",
            "options": ["vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
        }
        result = self.validate(config)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config["backup"]["smb"]["options"].remove("seal")
        result = self.validate(config)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        config["backup"]["smb"]["options"].append("seal")
        config["backup"]["smb"]["source"] = "//198.51.100.3/backups"
        result = self.validate(config)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_rejects_managed_infra_host(self):
        result = self.validate(production_cluster(), {"infra": {"hosts": {"old-lab": {"ansible_host": "192.0.2.80"}}}})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("infra", result.stdout + result.stderr)


class EffectiveProductionPolicyTests(unittest.TestCase):
    def test_extra_variables_cannot_downgrade_a_production_declaration(self):
        config = production_cluster()
        config["cluster"]["environment"] = "laboratory"
        result = subprocess.run(
            [sys.executable, str(REPO / "tests/validation/production_profile.py"), "cluster"],
            input=json.dumps({"configuration": config, "inventory": {}, "declared_environment": "production"}),
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be downgraded", result.stdout)


class ProductionPlatformPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load((REPO / "platform/example/platform.yml").read_text())
        self.config["platform"].update(environment="production", infra={"services": []})
        self.config["monitoring"]["pmm"]["ca_reference"] = "pki/production/pmm-ca.pem"
        self.config["monitoring"]["alerts"]["email"] = "ops@factory.company"
        self.inventory = yaml.safe_load((REPO / "platform/example/inventory.yml").read_text())
        del self.inventory["all"]["children"]["infra"]
        self.inventory["all"]["children"]["app"] = {"hosts": {"app1": {
            "ansible_host": "10.0.1.31", "app_node_address": "10.0.1.31",
        }}}

    def validate(self, config=None, inventory=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            declaration = root / "platform.yml"
            hosts = root / "inventory.yml"
            declaration.write_text(yaml.safe_dump(config if config is not None else self.config))
            hosts.write_text(yaml.safe_dump(inventory if inventory is not None else self.inventory))
            return subprocess.run(
                [sys.executable, str(REPO / "tests/validation/validate-platform.py"),
                 str(declaration), str(REPO / "platform/schema/platform.schema.json"), str(hosts)],
                cwd=REPO, capture_output=True, text=True, timeout=30,
            )

    def test_external_production_platform_is_accepted(self):
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_refuses_every_managed_lab_service(self):
        for services in (None, ["pmm"], ["minio"], ["maildev"]):
            with self.subTest(services=services):
                config = copy.deepcopy(self.config)
                if services is None:
                    del config["platform"]["infra"]
                else:
                    config["platform"]["infra"]["services"] = services
                result = self.validate(config)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_refuses_managed_infra_inventory_and_missing_client(self):
        for group, addition in (("infra", True), ("app", False)):
            with self.subTest(group=group):
                inventory = copy.deepcopy(self.inventory)
                if addition:
                    inventory["all"]["children"][group] = {"hosts": {"old-lab": {"ansible_host": "10.0.1.51"}}}
                else:
                    del inventory["all"]["children"][group]
                result = self.validate(inventory=inventory)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(group, result.stdout + result.stderr)


@unittest.skipIf(shutil.which("ansible-playbook") is None, "ansible-playbook unavailable")
class ProductionControllerPreflightTests(unittest.TestCase):
    def run_preflight(self, override=None, limit="localhost"):
        fixture = ProductionPlatformPolicyTests()
        fixture.setUp()
        fixture.inventory["all"]["hosts"] = {"localhost": {
            "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
        }}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            declaration, inventory = root / "platform.yml", root / "inventory.yml"
            declaration.write_text(yaml.safe_dump(fixture.config))
            inventory.write_text(yaml.safe_dump(fixture.inventory))
            command = [
                shutil.which("ansible-playbook"), "playbooks/platform_preflight.yml",
                "-i", str(inventory), "-e", "@" + str(declaration), "--limit", limit,
            ]
            if override is not None:
                command += ["-e", json.dumps(override)]
            return subprocess.run(command, cwd=REPO, env=dict(os.environ, ANSIBLE_NOCOLOR="1"),
                                  capture_output=True, text=True, timeout=90)

    def test_inventory_become_does_not_escalate_on_controller(self):
        result = self.run_preflight()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("changed=0", result.stdout)

    def test_extra_variables_cannot_disable_pmm_certificate_verification(self):
        fixture = ProductionPlatformPolicyTests()
        fixture.setUp()
        monitoring = copy.deepcopy(fixture.config["monitoring"])
        monitoring["pmm"]["validate_certs"] = False
        result = self.run_preflight({"monitoring": monitoring})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("validate_certs", result.stdout)

    def test_extra_variables_cannot_downgrade_platform_environment(self):
        fixture = ProductionPlatformPolicyTests()
        fixture.setUp()
        platform = dict(fixture.config["platform"], environment="laboratory")
        result = self.run_preflight({"platform": platform})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cannot be downgraded", result.stdout)

    def test_limit_cannot_skip_the_effective_production_policy(self):
        fixture = ProductionPlatformPolicyTests()
        fixture.setUp()
        platform = dict(fixture.config["platform"], environment="laboratory")
        result = self.run_preflight({"platform": platform}, limit="app1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cannot be downgraded", result.stdout)
        self.assertNotIn("Gathering Facts", result.stdout)


if __name__ == "__main__":
    unittest.main()
