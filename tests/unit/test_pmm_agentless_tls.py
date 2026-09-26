"""PMM agentless DB transport follows the tenant TLS contract on both agent types.

Real incident this file prevents (x17, 2026-09-26): `tls: true` was applied with
the tenant CA, the connection was encrypted and verified, but PMM 3.9.1 does NOT
echo `tls_ca` in GET /v1/inventory/agents. A convergence predicate comparing the
read-back `tls_ca` would therefore rewrite the agents on every single run.
The fingerprint label is the only stable, observable identity.
"""
import hashlib
import unittest
from pathlib import Path

import jinja2
from jinja2.nativetypes import NativeEnvironment
import yaml


ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = yaml.safe_load((ROOT / "roles/pmm/defaults/main.yml").read_text())
TASKS = {
    task["name"]: task
    for task in yaml.safe_load((ROOT / "roles/pmm/tasks/agentless_register.yml").read_text())
}
ENV = NativeEnvironment(undefined=jinja2.StrictUndefined)
ENV.filters["bool"] = bool
ENV.filters["hash"] = lambda value, name: hashlib.new(name, value.encode()).hexdigest()

CA_FIXTURE = "-----BEGIN CERTIFICATE-----\ntenant-ca\n-----END CERTIFICATE-----"
CA_SHA = hashlib.sha256(CA_FIXTURE.encode()).hexdigest()

RECONCILE = ("Uzgodnij dane dostępowe istniejących mysqld_exporter",
             "Uzgodnij dane dostępowe istniejących QAN Performance Schema agents")
CREATE = ("Utwórz PMM-managed mysqld_exporter agents",
          "Utwórz QAN Performance Schema agents")


def resolve_defaults(tls, lookup=None):
    """Rozwiazuje zmienne w kolejnosci Ansible: leniwie, z lookup tylko gdy uzyty."""
    context = {} if tls is None else {"tls": tls}
    context["lookup"] = lookup or (lambda plugin, path: CA_FIXTURE)
    context["playbook_dir"] = "/repo/playbooks"
    context["pmm_mysql_tls_ca_ref"] = ENV.from_string(
        DEFAULTS["pmm_mysql_tls_ca_ref"]).render(**context)
    context["pmm_mysql_tls_enabled"] = ENV.from_string(
        DEFAULTS["pmm_mysql_tls_enabled"]).render(**{"tls": tls} if tls is not None else {})
    context["pmm_mysql_tls_ca"] = ENV.from_string(
        DEFAULTS["pmm_mysql_tls_ca"]).render(**context)
    context["pmm_mysql_tls_ca_sha256"] = ENV.from_string(
        DEFAULTS["pmm_mysql_tls_ca_sha256"]).render(**context)
    return context


def render(expression, **context):
    """Zadania trzymaja albo goly warunek, albo szablon — obslugujemy oba."""
    source = expression if "{{" in expression else "{{ " + expression + " }}"
    return ENV.from_string(source).render(**context)


class DefaultsTests(unittest.TestCase):
    def test_disabled_tenant_never_loads_ca_and_labels_none(self):
        def forbidden(*args):
            raise AssertionError("disabled mode must not load the CA file")

        for tls in (None, {"mode": "disabled"}, {"mode": "disabled", "require_secure_transport": False}):
            with self.subTest(tls=tls):
                resolved = resolve_defaults(tls, lookup=forbidden)
                self.assertFalse(resolved["pmm_mysql_tls_enabled"])
                self.assertEqual(resolved["pmm_mysql_tls_ca"], "")
                self.assertEqual(resolved["pmm_mysql_tls_ca_sha256"], "none")

    def test_full_tenant_loads_ca_and_fingerprints_content(self):
        calls = []

        def ca_lookup(plugin, path):
            calls.append((plugin, path))
            return CA_FIXTURE

        resolved = resolve_defaults({"mode": "full", "ca_reference": "pki/tenant/ca.pem"},
                                   lookup=ca_lookup)
        self.assertTrue(resolved["pmm_mysql_tls_enabled"])
        self.assertEqual(resolved["pmm_mysql_tls_ca"], CA_FIXTURE)
        self.assertEqual(resolved["pmm_mysql_tls_ca_sha256"], CA_SHA)
        self.assertEqual(calls, [("ansible.builtin.file", "/repo/playbooks/../pki/tenant/ca.pem")])

    def test_absolute_ca_reference_is_used_verbatim(self):
        seen = []
        resolve_defaults({"mode": "full", "ca_reference": "/etc/isa/ca.pem"},
                         lookup=lambda plugin, path: seen.append(path) or CA_FIXTURE)
        self.assertEqual(seen, ["/etc/isa/ca.pem"])


class ReconcileContractTests(unittest.TestCase):
    def setUp(self):
        self.base = {"pmm_mysql_service_ids": {"db-mysql": "tenant-id"},
                     "pmm_credentials_revision_effective": "1", "pmm_mysql_tls_enabled": True,
                     "pmm_mysql_tls_ca": CA_FIXTURE, "pmm_mysql_tls_ca_sha256": CA_SHA}
        self.item = {"service_id": "tenant-id", "tls": True, "tls_skip_verify": False,
                     "custom_labels": {"credentials_revision": "1", "tls_ca_sha256": CA_SHA}}

    def test_drift_predicate_uses_fingerprint_not_read_back_ca(self):
        for name in RECONCILE:
            with self.subTest(agent=name):
                task = TASKS[name]
                self.assertTrue(task["no_log"])
                scope, drift = task["when"]
                self.assertNotIn("item.tls_ca", drift,
                                 "GET nie odsyla tls_ca — porownanie z nim nigdy nie zbiega")
                self.assertIn("tls_ca_sha256", drift)
                self.assertTrue(render(scope, item=self.item, **self.base))
                self.assertFalse(render(drift, item=self.item, **self.base))

    def test_every_observable_transport_drift_triggers_a_rewrite(self):
        drift = TASKS[RECONCILE[0]]["when"][1]
        for update in ({"tls": False},
                       {"tls_skip_verify": True},
                       {"custom_labels": {"credentials_revision": "1", "tls_ca_sha256": "none"}},
                       {"custom_labels": {"credentials_revision": "1", "tls_ca_sha256": "stale"}},
                       {"custom_labels": {"credentials_revision": "0", "tls_ca_sha256": CA_SHA}}):
            with self.subTest(update=update):
                self.assertTrue(render(drift, item={**self.item, **update}, **self.base))
        self.assertTrue(render(drift, item=self.item,
                               **{**self.base, "pmm_mysql_tls_ca_sha256": "rotated"}))
        # Ten sam klaster po wylaczeniu TLS: stan "tls=false + odcisk none" jest
        # zbiezny, wiec kondycja dryfu musi milczec — inaczej wylaczony najemca
        # przepisywalby agentow w kazdym przebiegu.
        self.assertFalse(render(drift, item={**self.item, "tls": False,
                                             "custom_labels": {"credentials_revision": "1",
                                                               "tls_ca_sha256": "none"}},
                                **{**self.base, "pmm_mysql_tls_enabled": False,
                                   "pmm_mysql_tls_ca": "", "pmm_mysql_tls_ca_sha256": "none"}))

    def test_agents_outside_our_service_ids_are_never_touched(self):
        scope = TASKS[RECONCILE[0]]["when"][0]
        self.assertFalse(render(scope, item={**self.item, "service_id": "foreign-id"}, **self.base))

    def test_all_four_tasks_send_verified_transport_and_label_the_fingerprint(self):
        for name in RECONCILE + CREATE:
            with self.subTest(task=name):
                body = TASKS[name]["ansible.builtin.uri"]["body"]
                agent = next(iter(body.values()))
                labels = agent["custom_labels"]
                labels = labels.get("values", labels)
                self.assertEqual(labels["tls_ca_sha256"],
                                 "{{ pmm_mysql_tls_ca_sha256 }}")
                for enabled, ca in ((True, CA_FIXTURE), (False, "")):
                    rendered = {
                        key: render(value, pmm_mysql_tls_enabled=enabled, pmm_mysql_tls_ca=ca)
                        if isinstance(value, str) and "{{" in value else value
                        for key, value in agent.items()
                        if key in ("tls", "tls_skip_verify", "tls_ca")}
                    self.assertEqual(rendered, {"tls": enabled, "tls_skip_verify": False,
                                                "tls_ca": ca})
                self.assertNotIn("tls_cert", agent)
                self.assertNotIn("tls_key", agent)


if __name__ == "__main__":
    unittest.main()
