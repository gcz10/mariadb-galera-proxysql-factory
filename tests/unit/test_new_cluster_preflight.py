#!/usr/bin/env python3
"""Bramka nowego klastra lapie niedokonczony szablon PRZED converge.

Powstalo z czystego przebiegu sigma-r9. `cluster-validate` mowil PASS przy pustych
CIDR-ach, brakujacym PKI, `.invalid` w PMM i niepelnym known_hosts. Kazdy blad
wychodzil dopiero kilka minut pozniej w osobnym playbooku.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SCHEMA_VALIDATOR = REPO / "tests" / "validation" / "validate-cluster-schema.py"
INVENTORY_VALIDATOR = REPO / "tests" / "validation" / "validate-inventory.py"
SCHEMA = REPO / "clusters" / "schema" / "cluster.schema.json"
EXAMPLE = REPO / "clusters" / "example-cluster"


class NewClusterPreflightTests(unittest.TestCase):
    @staticmethod
    def _run(*args):
        return subprocess.run(
            [sys.executable, *map(str, args)],
            cwd=REPO,
            capture_output=True,
            text=True,
        )

    def test_copied_template_is_rejected_until_required_fields_are_replaced(self):
        config = yaml.safe_load((EXAMPLE / "cluster.yml").read_text(encoding="utf-8"))
        config["cluster"]["name"] = "copied-but-unfinished"

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cluster.yml"
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            result = self._run(SCHEMA_VALIDATOR, path, SCHEMA)

        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("tls.ca_reference jest puste", output)
        self.assertIn("network.administration_cidrs jest puste", output)
        self.assertIn("monitoring.pmm.server_url", output)
        self.assertIn("backup.smb.source", output)
        self.assertIn("monitoring.alerts.email", output)
        self.assertIn("galera.cluster_name='example_galera'", output)

    def test_verbatim_copy_does_not_inherit_the_template_exemption(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cluster.yml"
            path.write_text(
                (EXAMPLE / "cluster.yml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            result = self._run(SCHEMA_VALIDATOR, path, SCHEMA)

        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("tls.ca_reference jest puste", output)
        self.assertIn("galera.cluster_name='example_galera'", output)

    def test_shipped_template_itself_remains_valid(self):
        result = self._run(SCHEMA_VALIDATOR, EXAMPLE / "cluster.yml", SCHEMA)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_cluster_without_known_hosts_is_rejected_with_command(self):
        config = yaml.safe_load((EXAMPLE / "cluster.yml").read_text(encoding="utf-8"))
        config["cluster"]["name"] = "missing-trust"
        inventory = yaml.safe_load((EXAMPLE / "inventory.yml").read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "missing-trust"
            root.mkdir()
            config_path = root / "cluster.yml"
            inventory_path = root / "inventory.yml"
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            inventory_path.write_text(yaml.safe_dump(inventory), encoding="utf-8")
            result = self._run(
                INVENTORY_VALIDATOR,
                inventory_path,
                config_path,
                "--require-known-hosts",
            )

        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("brak", output)
        self.assertIn("make cluster-trust-hosts CLUSTER=missing-trust", output)

    def test_repository_validation_does_not_require_ignored_known_hosts(self):
        """CI waliduje definicje z czystego checkoutu, bez lokalnych kluczy."""
        config = yaml.safe_load((EXAMPLE / "cluster.yml").read_text(encoding="utf-8"))
        config["cluster"]["name"] = "repository-only"
        inventory = yaml.safe_load((EXAMPLE / "inventory.yml").read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repository-only"
            root.mkdir()
            config_path = root / "cluster.yml"
            inventory_path = root / "inventory.yml"
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            inventory_path.write_text(yaml.safe_dump(inventory), encoding="utf-8")
            result = self._run(INVENTORY_VALIDATOR, inventory_path, config_path)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_known_hosts_must_cover_every_ssh_endpoint_including_app(self):
        config = yaml.safe_load((EXAMPLE / "cluster.yml").read_text(encoding="utf-8"))
        config["cluster"]["name"] = "partial-trust"
        inventory = yaml.safe_load((EXAMPLE / "inventory.yml").read_text(encoding="utf-8"))
        addresses = sorted(
            {
                host["ansible_host"]
                for group in inventory["all"]["children"].values()
                for host in (group.get("hosts") or {}).values()
                if host and host.get("ansible_host")
            }
        )
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m"

        for missing in addresses:
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "partial-trust"
                root.mkdir()
                config_path = root / "cluster.yml"
                inventory_path = root / "inventory.yml"
                config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
                inventory_path.write_text(yaml.safe_dump(inventory), encoding="utf-8")
                (root / "known_hosts").write_text(
                    "".join(
                        f"{address} {key}\n"
                        for address in addresses
                        if address != missing
                    ),
                    encoding="utf-8",
                )
                result = self._run(
                    INVENTORY_VALIDATOR,
                    inventory_path,
                    config_path,
                    "--require-known-hosts",
                )

            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn(missing, output)
            self.assertIn("known_hosts nie zna hostow", output)

    def test_hashed_known_hosts_covers_every_ssh_endpoint(self):
        config = yaml.safe_load((EXAMPLE / "cluster.yml").read_text(encoding="utf-8"))
        config["cluster"]["name"] = "hashed-trust"
        inventory = yaml.safe_load((EXAMPLE / "inventory.yml").read_text(encoding="utf-8"))
        addresses = sorted(
            {
                host["ansible_host"]
                for group in inventory["all"]["children"].values()
                for host in (group.get("hosts") or {}).values()
                if host and host.get("ansible_host")
            }
        )
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "hashed-trust"
            root.mkdir()
            config_path = root / "cluster.yml"
            inventory_path = root / "inventory.yml"
            known_hosts = root / "known_hosts"
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            inventory_path.write_text(yaml.safe_dump(inventory), encoding="utf-8")
            known_hosts.write_text(
                "".join(f"{address} {key}\n" for address in addresses),
                encoding="utf-8",
            )
            subprocess.run(
                ["ssh-keygen", "-H", "-f", str(known_hosts)],
                check=True,
                capture_output=True,
            )
            result = self._run(
                INVENTORY_VALIDATOR,
                inventory_path,
                config_path,
                "--require-known-hosts",
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_trust_hosts_reports_dead_hosts_with_a_bounded_probe(self):
        inventory = {
            "all": {
                "vars": {
                    "ansible_user": "nobody",
                    "ansible_ssh_private_key_file": "/dev/null",
                },
                "children": {
                    "galera": {
                        "hosts": {
                            "dead": {"ansible_host": "192.0.2.1"},
                        }
                    }
                },
            }
        }
        with tempfile.TemporaryDirectory(
            prefix="trust-test-", dir=REPO / "clusters"
        ) as td:
            cluster = Path(td).name
            (Path(td) / "inventory.yml").write_text(
                yaml.safe_dump(inventory), encoding="utf-8"
            )
            started = time.monotonic()
            result = subprocess.run(
                [
                    "make", "cluster-trust-hosts", f"CLUSTER={cluster}",
                    "TRUST_HOST_PROBES=1", "TRUST_KEYSCAN_TIMEOUT=1",
                ],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=15,
            )
            elapsed = time.monotonic() - started

        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("NIEOSIAGALNE: 192.0.2.1", output)
        self.assertLess(elapsed, 10, output)

    def test_trust_hosts_uses_inventory_identity_and_fails_auth_immediately(self):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m"
        with tempfile.TemporaryDirectory(
            prefix="trust-auth-", dir=REPO / "clusters"
        ) as td:
            root = Path(td)
            cluster = root.name
            private_key = root / "operator-key"
            inventory = {
                "all": {
                    "vars": {
                        "ansible_user": "ansible",
                        "ansible_ssh_private_key_file": str(private_key),
                    },
                    "children": {
                        "galera": {
                            "hosts": {
                                "auth": {"ansible_host": "192.0.2.1"},
                            }
                        }
                    },
                }
            }
            (root / "inventory.yml").write_text(
                yaml.safe_dump(inventory), encoding="utf-8"
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "ssh-keyscan").write_text(
                "#!/bin/sh\nfor last do :; done\n"
                f"printf '%s {key}\\n' \"$last\"\n",
                encoding="utf-8",
            )
            args_log = root / "ssh-args"
            (fake_bin / "ssh").write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$SSH_ARGS_LOG\"\n"
                "echo 'Permission denied (publickey).' >&2\nexit 255\n",
                encoding="utf-8",
            )
            for executable in ("ssh-keyscan", "ssh"):
                (fake_bin / executable).chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["SSH_ARGS_LOG"] = str(args_log)
            started = time.monotonic()
            result = subprocess.run(
                ["make", "cluster-trust-hosts", f"CLUSTER={cluster}"],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
            )
            elapsed = time.monotonic() - started
            ssh_args = args_log.read_text(encoding="utf-8")

        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn(
            f"odrzuca ansible z kluczem {private_key}",
            output,
        )
        self.assertIn(f"-i {private_key}", ssh_args)
        self.assertIn("ansible@192.0.2.1", ssh_args)
        self.assertLess(elapsed, 10, output)

    def test_trust_hosts_rejects_empty_inventory(self):
        with tempfile.TemporaryDirectory(
            prefix="trust-empty-", dir=REPO / "clusters"
        ) as td:
            cluster = Path(td).name
            (Path(td) / "inventory.yml").write_text(
                yaml.safe_dump({"all": {"children": {}}}),
                encoding="utf-8",
            )
            result = subprocess.run(
                ["make", "cluster-trust-hosts", f"CLUSTER={cluster}"],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=15,
            )

        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("ansible-inventory nie zwrocilo zadnego ansible_host", output)


if __name__ == "__main__":
    unittest.main()
