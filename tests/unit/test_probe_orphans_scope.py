#!/usr/bin/env python3
"""probe-orphans mierzy WYLACZNIE zasoby, ktore fabryka deklaruje.

Trzy regresje pilnujace przenosnosci sondy sierot:
  * S3 zewnetrzny (endpoint nie wskazuje hosta infra TEGO inwentarza) nie ma
    konta serwisowego zakladanego przez fabryke, wiec sonda nie wolno zadac
    poswiadczen MINIO_ROOT ani uruchamiac `mc` na grupie infra. Predykat jest
    lustrzany wobec `galera_backup_managed_minio` z playbooks/cluster_deregister.yml
    (ten sam, ktory decyduje o tym, CZY derejestracja w ogole odwola konto) —
    test renderuje wyrazenie WPROST Z PLAYBOOKA i wymaga zgodnego werdyktu.
  * mc_image czytamy z lockfile wskazanego przez definicje (`versions.lock_file`),
    nie z sztywnej sciezki.
  * Brak zadeklarowanego monitoring.pmm.server_url to odmowa PRZED pomiarem,
    nie powod, zeby mierzyc PMM z adresu wpisane w kodzie.
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "lab"))

PLAYBOOK = REPO / "playbooks" / "cluster_deregister.yml"
SPEC = importlib.util.spec_from_file_location("probe_orphans_under_test", REPO / "tests" / "lab" / "probe-orphans.py")
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)

INFRA_ADDRESS = "10.0.9.1"


class _Inventory:
    """FakeContext: inwentarz w pamieci, sekrety pod kontrola testu."""

    def __init__(self, config, inventory, secrets):
        self.config = config
        self.inventory = inventory
        self.cluster_name = "probe"
        self._secrets = secrets

    def group_hosts(self, group):
        hosts = self.inventory.get("all", {}).get("children", {}).get(group, {}).get("hosts", {})
        return list(hosts.keys())

    def host_address(self, name, group):
        entry = self.inventory.get("all", {}).get("children", {}).get(group, {}).get("hosts", {}).get(name)
        return (entry or {}).get("ansible_host")

    def env_secret(self, name):
        return self._secrets.get(name, "")


def config(destination="s3", endpoint="10.0.9.9:9000", enabled=True,
           server_url="https://pmm.invalid", lock_file="versions/versions.lock.yml"):
    if destination == "s3":
        backup = {"enabled": enabled, "destination": "s3",
                  "s3": {"endpoint": endpoint, "bucket": "probe-galera-backups"}}
    else:
        backup = {"enabled": True, "destination": "smb",
                  "smb": {"source": "//nas/x", "mount_point": "/m", "options": []}}
    return {
        "cluster": {"name": "probe", "environment": "laboratory"},
        "versions": {"lock_file": lock_file},
        "proxysql": {"hostgroup_base": 10, "app_user": "app_user_probe"},
        "backup": backup,
        "monitoring": {"pmm": {"server_url": server_url, "cluster_name": "probe",
                               "validate_certs": True}},
    }


class OrphanPredicateMirrorTests(unittest.TestCase):
    """Predykat sondy musi wydawac TEN SAM werdykt co derejestracja."""

    @classmethod
    def setUpClass(cls):
        cls.doc = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
        preflight = cls.doc[0]
        predicate_task = next(
            task for task in preflight["tasks"]
            if "ansible.builtin.set_fact" in task
            and "galera_backup_managed_minio" in task["ansible.builtin.set_fact"]
        )
        cls.expression = str(predicate_task["ansible.builtin.set_fact"]["galera_backup_managed_minio"])
        # Srodowisko musi odwzorowywac SEMANTYKE ANSIBLE, nie gola Jinje.
        # Zmierzono na ansible 2.15: na hoscie BEZ `ansible_host` playbook pada
        # z AnsibleUndefinedVariable (preflight wywala sie glosno), wiec sonda
        # w tym stanie ma prawo raportowac NIEzarzadzane zamiast zgadywac.
        # StrictUndefined podnioslby tu wyjatek i test pinowalby artefakt
        # srodowiska testowego, nie kontrakt playbooka.
        cls.env = jinja2.Environment(undefined=jinja2.Undefined)
        cls.env.filters["regex_replace"] = lambda value, pattern, replacement: re.sub(pattern, replacement, str(value))

    def playbook_verdict(self, destination, endpoint, hostvars_by_host):
        """Renderuj wyrazenie derejestracji na TO SAMYM inwentarzu co sonda."""
        context = {
            "backup": {"destination": destination, "s3": {"endpoint": endpoint}},
            "groups": {"infra": list(hostvars_by_host)},
            "hostvars": hostvars_by_host,
        }
        rendered = self.env.from_string(self.expression).render(context).strip()
        return rendered == "True"

    def verdict_pair(self, *, destination, endpoint, with_infra, with_ansible_host=True):
        inventory = {"all": {"children": {
            "proxysql": {"hosts": {"p1": {"ansible_host": "10.0.0.21"}}},
        }}}
        if with_infra:
            host_entry = {"ansible_host": INFRA_ADDRESS} if with_ansible_host else {
                "infra_node_address": INFRA_ADDRESS}
            inventory["all"]["children"]["infra"] = {"hosts": {"i1": host_entry}}
        ctx = _Inventory(config=config(), inventory=inventory, secrets={"PMM_ADMIN_PASSWORD": "x"})
        backup_cfg = {"destination": destination, "s3": {"endpoint": endpoint}}
        children = (ctx.inventory["all"]["children"].get("infra", {}).get("hosts", {})
                    if with_infra else {})
        hostvars = {name: entry for name, entry in children.items()}
        probe_address = str((children.get("i1") or {}).get("ansible_host") or "")
        managed = PROBE._managed_minio_address(backup_cfg, probe_address)
        return managed, self.playbook_verdict(destination, endpoint, hostvars)

    def test_external_s3_agrees_with_deregister_predicate(self):
        managed, expected = self.verdict_pair(
            destination="s3", endpoint="10.0.9.9:9000", with_infra=True)
        self.assertFalse(managed)
        self.assertFalse(expected)

    def test_managed_endpoint_with_port_agrees(self):
        managed, expected = self.verdict_pair(
            destination="s3", endpoint=f"{INFRA_ADDRESS}:9000", with_infra=True)
        self.assertTrue(managed)
        self.assertTrue(expected)

    def test_managed_endpoint_without_port_agrees(self):
        managed, expected = self.verdict_pair(
            destination="s3", endpoint=INFRA_ADDRESS, with_infra=True)
        self.assertTrue(managed)
        self.assertTrue(expected)

    def test_missing_infra_group_agrees(self):
        managed, expected = self.verdict_pair(
            destination="s3", endpoint=f"{INFRA_ADDRESS}:9000", with_infra=False)
        self.assertFalse(managed)
        self.assertFalse(expected)

    def test_infra_host_without_ansible_host_agrees_with_playbook(self):
        """Playbook czyta WYLACZNIE `hostvars[infra[0]].ansible_host`; bez tego
        pola pada z AnsibleUndefinedVariable (zmierzono na ansible 2.15), wiec
        derejestracja nie uznaje takiego klastra za zarzadzany. Sonda nie moze
        utworzyc laskawszej konwencji (fallback na `infra_node_address`):
        wowczas wymagalaby poswiadczen MinIO tam, gdzie preflight derejestracji
        wywala sie glosno zanim cokolwiek zostanie odwolane."""
        managed, expected = self.verdict_pair(
            destination="s3", endpoint=f"{INFRA_ADDRESS}:9000",
            with_infra=True, with_ansible_host=False)
        self.assertFalse(managed)
        self.assertFalse(expected)


class OrphansMainScopeTests(unittest.TestCase):
    """main() nie wolno dotykac lokalnego MinIO dla S3 zewnetrznego."""

    def run_main(self, *, destination, endpoint, secrets, inventory, enabled=True):
        module_cfg = config(destination=destination, endpoint=endpoint, enabled=enabled)
        ctx = _Inventory(config=module_cfg, inventory=inventory, secrets=secrets)
        ansible_targets = []

        def fake_ansible(ctx, pattern, script, timeout=120):
            from _probe_common import AnsibleResult
            result = AnsibleResult()
            result.bodies = {name: "" for name in (ctx.group_hosts("proxysql") + ctx.group_hosts("infra"))}
            ansible_targets.append(pattern)
            return result

        api_paths = []

        def fake_api(path, server_url, auth_header, undetermined, unavailable, pmm_config):
            api_paths.append(path)
            shapes = {
                "/v1/inventory/nodes": {},
                "/v1/inventory/services": {},
                "/graph/api/v1/provisioning/alert-rules": [],
                "/graph/api/v1/provisioning/contact-points": [],
                "/graph/api/folders": [],
                "/graph/api/v1/provisioning/policies": {"routes": []},
            }
            return shapes.get(path, {})

        with mock.patch.object(PROBE, "ProbeContext", lambda: ctx), \
                mock.patch.object(PROBE, "run_ansible", fake_ansible), \
                mock.patch.object(PROBE, "api_get", fake_api), \
                mock.patch("builtins.print") as printed:
            code = PROBE.main()
        output = "\n".join(" ".join(str(arg) for arg in call.args) for call in printed.call_args_list)
        return code, output, ansible_targets, api_paths

    INVENTORY_EXTERNAL = {"all": {"children": {
        "proxysql": {"hosts": {"p1": {"ansible_host": "10.0.0.21"}}},
        "infra": {"hosts": {"i1": {"ansible_host": "10.0.9.1"}}},
    }}}

    def test_external_s3_skips_minio_without_touching_local_storage(self):
        code, output, targets, paths = self.run_main(
            destination="s3", endpoint="10.0.9.9:9000", secrets={"PMM_ADMIN_PASSWORD": "x"},
            inventory=self.INVENTORY_EXTERNAL)
        self.assertEqual(code, 0, output)
        self.assertIn("SKIP: magazyn S3 jest zewnetrzny", output)
        self.assertNotIn("MINIO_ROOT_USER", output)
        self.assertNotIn("MinIO: brak MINIO_ROOT", output)
        self.assertNotIn("infra", targets, "lokalny MinIO zostal dotkniety mimo zewnetrznego S3")
        # Podsumowanie nie moze twierdzic, ze MinIO zostalo zmierzone.
        self.assertIn("w PMM, Grafanie i ProxySQL", output)

    def test_managed_minio_still_fails_closed_without_root_credentials(self):
        inventory = {"all": {"children": {
            "proxysql": {"hosts": {"p1": {"ansible_host": "10.0.0.21"}}},
            "infra": {"hosts": {"i1": {"ansible_host": "10.0.9.9"}}},
        }}}
        code, output, targets, paths = self.run_main(
            destination="s3", endpoint="10.0.9.9:9000", secrets={"PMM_ADMIN_PASSWORD": "x"},
            inventory=inventory)
        self.assertEqual(code, 2, output)
        self.assertIn("MinIO: brak MINIO_ROOT_USER/MINIO_ROOT_PASSWORD", output)

    def test_disabled_backup_with_managed_endpoint_still_skips(self):
        inventory = {"all": {"children": {
            "proxysql": {"hosts": {"p1": {"ansible_host": "10.0.0.21"}}},
            "infra": {"hosts": {"i1": {"ansible_host": "10.0.9.9"}}},
        }}}
        code, output, targets, paths = self.run_main(
            destination="s3", endpoint="10.0.9.9:9000", secrets={"PMM_ADMIN_PASSWORD": "x"},
            inventory=inventory, enabled=False)
        self.assertEqual(code, 0, output)
        self.assertIn("SKIP: backup wylaczony", output)

    def test_disabled_backup_on_external_s3_reports_external_skip(self):
        code, output, targets, paths = self.run_main(
            destination="s3", endpoint="10.0.9.9:9000", secrets={"PMM_ADMIN_PASSWORD": "x"},
            inventory=self.INVENTORY_EXTERNAL, enabled=False)
        self.assertEqual(code, 0, output)
        self.assertIn("SKIP: magazyn S3 jest zewnetrzny", output)


class OrphansDeclarationTests(unittest.TestCase):
    """Deklaracja zamiast zalozenia: adres PMM i lockfile pochodza z configu."""

    def run_main(self, config_extra, secrets):
        module_cfg = config()
        module_cfg.update(config_extra)
        ctx = _Inventory(config=module_cfg, inventory=self.INVENTORY_EXTERNAL, secrets=secrets)
        api_hits = []
        with mock.patch.object(PROBE, "ProbeContext", lambda: ctx), \
                mock.patch.object(PROBE, "api_get",
                                  lambda path, url, header, undetermined, unavailable, pmm_config:
                                  api_hits.append((path, url)) or {}), \
                mock.patch("builtins.print") as printed:
            code = PROBE.main()
        output = "\n".join(" ".join(str(arg) for arg in call.args)
                           for call in printed.call_args_list)
        return code, output, api_hits

    INVENTORY_EXTERNAL = {"all": {"children": {
        "proxysql": {"hosts": {"p1": {"ansible_host": "10.0.0.21"}}},
    }}}

    def test_missing_pmm_declaration_fails_before_any_request(self):
        code, output, hits = self.run_main(
            {"monitoring": {"pmm": {"cluster_name": "probe", "validate_certs": True}}},
            {"PMM_ADMIN_PASSWORD": "x"})
        self.assertEqual(code, 1, output)
        self.assertIn("monitoring.pmm.server_url", output)
        self.assertEqual(hits, [], "sonda wyslala zapytanie bez zadeklarowanego adresu")

    def test_mc_image_comes_from_the_declared_lockfile(self):
        undetermined: list[str] = []
        with mock.patch.object(PROBE, "REPO_ROOT", REPO):
            image = PROBE._load_mc_image(
                _Inventory(config=config(lock_file="versions/versions-el10-123.lock.yml"),
                           inventory={}, secrets={}),
                undetermined)
        self.assertEqual(undetermined, [])
        self.assertTrue(image.endswith("sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727"))

    def test_missing_declared_lockfile_reports_that_path(self):
        undetermined: list[str] = []
        with mock.patch.object(PROBE, "REPO_ROOT", REPO):
            image = PROBE._load_mc_image(
                _Inventory(config=config(lock_file="versions/brak.lock.yml"),
                           inventory={}, secrets={}),
                undetermined)
        self.assertEqual(image, "")
        self.assertEqual(len(undetermined), 1)
        self.assertIn("versions/brak.lock.yml", undetermined[0])


if __name__ == "__main__":
    unittest.main()
