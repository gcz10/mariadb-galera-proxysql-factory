"""Off-host means every resolved address is outside every database source."""

import os
import shutil
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "validation"))
from production_backup_host import off_host_errors


class ProductionBackupLocationTests(unittest.TestCase):
    def errors(self, endpoint, destination="s3", resolve=False):
        return off_host_errors(destination, endpoint, {"gnode1", "gnode2"},
                               {"198.51.100.1", "198.51.100.2"}, resolve=resolve)

    def test_dns_alias_with_one_local_answer_is_rejected(self):
        answers = [(2, 1, 6, "", ("192.0.2.80", 0)),
                   (2, 1, 6, "", ("198.51.100.2", 0))]
        with patch("production_backup_host.socket.getaddrinfo", return_value=answers):
            self.assertTrue(self.errors("storage.company:443", resolve=True))

    def test_failed_dns_never_proves_off_host_storage(self):
        with patch("production_backup_host.socket.getaddrinfo", side_effect=OSError):
            errors = self.errors("storage.company:443", resolve=True)
        self.assertIn("unverified", errors[0])

    def test_remote_s3_and_smb_endpoints_are_accepted(self):
        self.assertEqual(self.errors("192.0.2.80:443"), [])
        self.assertEqual(self.errors("//192.0.2.80/backups", "smb"), [])

    def test_loopback_and_mapped_database_addresses_are_rejected(self):
        for endpoint in ("127.0.0.1:443", "[::1]:443", "[::ffff:198.51.100.1]:443"):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(self.errors(endpoint))

    def test_transport_or_embedded_credentials_cannot_disguise_location(self):
        for endpoint in ("http://192.0.2.80", "https://secret:password@192.0.2.80"):
            with self.subTest(endpoint=endpoint):
                errors = self.errors(endpoint)
                self.assertTrue(errors)
                self.assertNotIn("password", " ".join(errors))


@unittest.skipIf(shutil.which("ansible-playbook") is None, "ansible-playbook unavailable")
class ProductionBackupPreflightTests(unittest.TestCase):
    def test_dns_named_inventory_without_ansible_host_still_measures_all_sources(self):
        repo = Path(__file__).resolve().parents[2]
        preflight = yaml.safe_load((repo / "playbooks/f2_preflight.yml").read_text())
        source_play = next(play for play in preflight if play.get("hosts") == "galera:restore")
        task = source_play["tasks"][0]
        reader = repo / "tests/validation/production_backup_host.py"
        task["ansible.builtin.command"]["argv"][-1] = "{{ lookup('ansible.builtin.file', '" + str(reader) + "') }}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory, playbook = root / "inventory.yml", root / "preflight.yml"
            inventory.write_text(yaml.safe_dump({"all": {
                "vars": {"ansible_connection": "local", "ansible_python_interpreter": sys.executable},
                "children": {"galera": {"hosts": {
                    "db1": {"galera_node_address": "198.51.100.1"},
                    "db2": {"galera_node_address": "198.51.100.2"},
                }}},
            }}))
            playbook.write_text(yaml.safe_dump([{
                "name": "Measure source identity without SSH or mounting", "hosts": "db1",
                "gather_facts": False, "become": False,
                "vars": {"cluster": {"environment": "production"}, "ansible_facts": {
                    "hostname": "db1", "fqdn": "db1.company",
                    "all_ipv4_addresses": ["198.51.100.1"], "all_ipv6_addresses": [],
                }}, "tasks": [task],
            }]))
            for endpoint, expected in (("192.0.2.80:443", 0), ("198.51.100.2:443", 2)):
                with self.subTest(endpoint=endpoint):
                    result = subprocess.run([
                        shutil.which("ansible-playbook"), str(playbook), "-i", str(inventory),
                        "-e", yaml.safe_dump({"backup": {
                            "destination": "s3", "s3": {"endpoint": endpoint},
                        }}, default_flow_style=True),
                    ], cwd=repo, env=dict(os.environ, ANSIBLE_NOCOLOR="1"),
                       capture_output=True, text=True, timeout=90)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    self.assertNotIn("undefined", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
