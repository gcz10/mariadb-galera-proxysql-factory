"""proxysql.endpoint.type: jedyny wdrazany typ to keepalived_vip.

playbooks/f8_keepalived.yml wymaga go wprost, a dla external_load_balancer i dns
nie istnieje zaden wykonawca. Deklaracja klastra i deklaracja platformy musza
wiec odrzucac pozostale typy na bramce walidacji, przed wdrozeniem — a nie
dopiero w F8 na zywych hostach.

Kazdy przypadek zmienia WYLACZNIE `type` w poprawnym fixture (z kontrola, ze
ten sam fixture z keepalived_vip przechodzi), wiec odmowa musi pochodzic od typu
endpointu, a nie od niezwiazanego bledu deklaracji.
"""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft7Validator

from test_cluster_schema_contract import canonical_cluster

REPO = Path(__file__).resolve().parents[2]
CLUSTER_VALIDATOR = REPO / "tests" / "validation" / "validate-cluster-schema.py"
CLUSTER_SCHEMA = REPO / "clusters" / "schema" / "cluster.schema.json"
PLATFORM_VALIDATOR = REPO / "tests" / "validation" / "validate-platform.py"
PLATFORM_SCHEMA = REPO / "platform" / "schema" / "platform.schema.json"
PLATFORM_EXAMPLE = REPO / "platform" / "example"

UNSUPPORTED_TYPES = ("external_load_balancer", "dns")

_spec = importlib.util.spec_from_file_location("validate_platform_under_test", PLATFORM_VALIDATOR)
validate_platform = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_platform)


class ClusterEndpointTypeTests(unittest.TestCase):
    @staticmethod
    def run_cli(config: dict, schema: dict = None) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            declaration = root / "cluster.yml"
            declaration.write_text(yaml.safe_dump(config), encoding="utf-8")
            schema_path = CLUSTER_SCHEMA
            if schema is not None:
                schema_path = root / "schema.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(CLUSTER_VALIDATOR), str(declaration), str(schema_path)],
                cwd=REPO, capture_output=True, text=True, timeout=30,
            )

    @staticmethod
    def with_type(endpoint_type: str, address: str = None) -> dict:
        config = canonical_cluster()
        config["proxysql"]["endpoint"]["type"] = endpoint_type
        if address is not None:
            config["proxysql"]["endpoint"]["address"] = address
        return config

    def test_keepalived_vip_is_accepted(self):
        result = self.run_cli(self.with_type("keepalived_vip"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unsupported_types_are_rejected_on_the_endpoint_type(self):
        for endpoint_type in UNSUPPORTED_TYPES:
            with self.subTest(type=endpoint_type):
                result = self.run_cli(self.with_type(endpoint_type))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("proxysql/endpoint/type", result.stdout)

    def test_hostname_address_does_not_hide_the_unsupported_type(self):
        # Adres-FQDN to naturalna deklaracja dla dns/external_load_balancer;
        # schema musi wskazac takze sam typ, nie tylko wzorzec adresu.
        validator = Draft7Validator(json.loads(CLUSTER_SCHEMA.read_text()))
        for endpoint_type in UNSUPPORTED_TYPES:
            with self.subTest(type=endpoint_type):
                config = self.with_type(endpoint_type, "proxysql.factory.company")
                paths = {tuple(error.absolute_path) for error in validator.iter_errors(config)}
                self.assertIn(("proxysql", "endpoint", "type"), paths)

    def test_semantic_check_rejects_when_schema_does_not_restrict_the_type(self):
        # Walidator przyjmuje schema jako argument; odmowa nie moze zalezec
        # wylacznie od jej wyliczenia. Schema bez `enum` przepuszcza kazdy typ,
        # wiec jedynym odrzucajacym jest kontrola semantyczna.
        schema = json.loads(CLUSTER_SCHEMA.read_text())
        del schema["properties"]["proxysql"]["properties"]["endpoint"]["properties"]["type"]["enum"]

        accepted = self.run_cli(self.with_type("keepalived_vip"), schema)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        for endpoint_type in UNSUPPORTED_TYPES:
            with self.subTest(type=endpoint_type):
                result = self.run_cli(self.with_type(endpoint_type), schema)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("proxysql.endpoint.type", result.stdout)


class PlatformEndpointTypeTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load((PLATFORM_EXAMPLE / "platform.yml").read_text())
        self.inventory = yaml.safe_load((PLATFORM_EXAMPLE / "inventory.yml").read_text())

    def with_type(self, endpoint_type: str) -> dict:
        config = copy.deepcopy(self.config)
        config["proxysql"]["endpoint"]["type"] = endpoint_type
        return config

    def run_cli(self, config: dict) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            declaration, hosts = root / "platform.yml", root / "inventory.yml"
            declaration.write_text(yaml.safe_dump(config), encoding="utf-8")
            hosts.write_text(yaml.safe_dump(self.inventory), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(PLATFORM_VALIDATOR),
                 str(declaration), str(PLATFORM_SCHEMA), str(hosts)],
                cwd=REPO, capture_output=True, text=True, timeout=30,
            )

    def test_keepalived_vip_is_accepted(self):
        result = self.run_cli(self.with_type("keepalived_vip"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unsupported_types_are_rejected_on_the_endpoint_type(self):
        for endpoint_type in UNSUPPORTED_TYPES:
            with self.subTest(type=endpoint_type):
                result = self.run_cli(self.with_type(endpoint_type))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("proxysql/endpoint/type", result.stdout)

    def test_semantic_errors_reject_the_type_without_the_schema(self):
        groups = validate_platform.inventory_groups(self.inventory)
        self.assertEqual(validate_platform.semantic_errors(self.with_type("keepalived_vip"), groups), [])
        for endpoint_type in UNSUPPORTED_TYPES:
            with self.subTest(type=endpoint_type):
                errors = validate_platform.semantic_errors(self.with_type(endpoint_type), groups)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("proxysql.endpoint.type", errors[0])


if __name__ == "__main__":
    unittest.main()
