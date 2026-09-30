#!/usr/bin/env python3
"""Wylaczony restore drill (`restore_test_schedule: disabled`) nie jest wymaganiem.

`disabled` to NIEPUSTY napis (schemat wymaga pola), wiec kazdy konsument, ktory
sprawdzal niepustosc, traktowal produkcje bez drilli jak klaster z drillami:
  * sonda PMM wymagala swiezej metryki drillu, ktorej nikt nie publikuje,
  * regula `restore-drill-stale` powstawala i palila sie wiecznie (brak metryki
    = `or vector(0)` = 1).
Produkcja nie ma drilli — naleza do osobnego srodowiska nieprodukcyjnego — wiec
werdykt ma byc zielony bez nich, a brak/przeterminowanie BACKUPU i metryki TLS
dalej czerwone.

Drugi kontrakt: produkcyjny PMM jest ZEWNETRZNY (dowolna dystrybucja), wiec
wymog dystrybucji Docker dotyczy tylko laboratorium/stagingu; wersja z lockfile
obowiazuje wszedzie.

Dowody: werdykt sondy nad syntetycznymi seriami z PMM oraz PRAWDZIWY
`playbooks/f15_alerts.yml` uruchomiony przeciw atrapie Grafany — uzywany do
sprawdzenia, ze reguly faktycznie zalozone w PMM sa tymi, ktorych oczekuje
sonda, i ze przejscie na wylaczony drill kasuje osierocona regule.
"""

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import yaml

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "tests" / "lab" / "probe-pmm-native.py"
F15 = REPO / "playbooks" / "f15_alerts.yml"
EXAMPLE = REPO / "clusters" / "example-cluster"
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")

NOW = 1_800_000_000.0
PROBE_STARTED = NOW - 120
RESTORE_FRESHNESS = "isa_restore_test_last_success_unixtime"
SCHEDULED = "0 4 * * 0"
DOCKER = "DISTRIBUTION_METHOD_DOCKER"


def load_probe(config=None):
    """Importuje sonde dla deklaracji `config` (domyslnie klaster przykladowy)."""
    with tempfile.TemporaryDirectory() as td:
        config_path = EXAMPLE / "cluster.yml"
        if config is not None:
            config_path = Path(td) / "cluster.yml"
            config_path.write_text(
                yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
            )
        env = {
            "CLUSTER_CONFIG": str(config_path),
            "CLUSTER_INVENTORY": str(EXAMPLE / "inventory.yml"),
        }
        cwd = os.getcwd()
        os.chdir(REPO)
        try:
            with mock.patch.dict(os.environ, env), mock.patch.object(
                sys, "path", [str(PROBE.parent), *sys.path]
            ):
                spec = importlib.util.spec_from_file_location(
                    "probe_pmm_lifecycle_under_test", PROBE
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
        finally:
            os.chdir(cwd)


BASE = load_probe()


def declaration(
    environment="laboratory", schedule=SCHEDULED, backup_enabled=True, tls_mode="full"
):
    config = copy.deepcopy(BASE.CLUSTER_CONFIG)
    config["cluster"]["environment"] = environment
    config["backup"]["enabled"] = backup_enabled
    config["backup"]["restore_test_schedule"] = schedule
    config["tls"]["mode"] = tls_mode
    return config


def healthy_values(*, restore_gate, restore_unixtime=None, tls_full=True, backup=True):
    """Wartosci, ktore PMM zwraca dla zdrowego klastra (metryka -> wartosc)."""
    values = {
        "isa_restore_test_monitoring_enabled": restore_gate,
        "isa_tls_monitoring_enabled": 1 if tls_full else 0,
        "isa_tls_cert_expiry_unixtime": NOW + 90 * 86400 if tls_full else 0,
    }
    if backup:
        values.update(
            {
                "galera_backup_last_success_unixtime": NOW - 3600,
                "galera_backup_last_failure_unixtime": 0,
                "galera_backup_last_run_success": 1,
                "galera_backup_last_size_bytes": 4096,
                "galera_backup_last_duration_seconds": 12,
            }
        )
    if restore_unixtime is not None:
        values[RESTORE_FRESHNESS] = restore_unixtime
    return values


def state_reader(values):
    """Atrapa `state_value` sondy: seria dla kazdej metryki z `values`, reszta pusta."""
    queried = []

    def state_value(name):
        queried.append(name)
        if name not in values:
            return [], None
        series = [
            {
                "metric": {
                    "node_name": BASE.FIRST_GALERA_NODE,
                    "logical_cluster": BASE.CLUSTER_CONFIG["cluster"]["name"],
                    "backend": BASE.CLUSTER_CONFIG["backup"]["destination"],
                },
                "value": [NOW, str(values[name])],
            }
        ]
        return series, float(values[name])

    state_value.queried = queried
    return state_value


def verdict(config, values):
    reader = state_reader(values)
    failures = BASE.lifecycle_metric_failures(config, reader, NOW, PROBE_STARTED)
    return failures, reader.queried


def mentions(failures, metric):
    return any(metric in failure for failure in failures)


class RestorePredicateTests(unittest.TestCase):
    def test_only_a_real_schedule_on_a_backed_up_cluster_enables_drills(self):
        cases = (
            (SCHEDULED, True, True),
            ("disabled", True, False),
            ("", True, False),
            ("  disabled  ", True, False),
            (SCHEDULED, False, False),
        )
        for schedule, backup_enabled, expected in cases:
            with self.subTest(schedule=schedule, backup_enabled=backup_enabled):
                config = declaration(schedule=schedule, backup_enabled=backup_enabled)
                self.assertEqual(BASE.restore_drill_enabled(config), expected)

    def test_declared_gate_value_follows_the_predicate(self):
        for schedule, expected in ((SCHEDULED, 1), ("disabled", 0)):
            with self.subTest(schedule=schedule):
                config = declaration(schedule=schedule)
                gates, _, _ = BASE.lifecycle_expectations(config)
                self.assertEqual(gates["isa_restore_test_monitoring_enabled"], expected)


class ProbeLifecycleVerdictTests(unittest.TestCase):
    def test_scheduled_drill_requires_fresh_restore_evidence(self):
        config = declaration(schedule=SCHEDULED)
        failures, _ = verdict(
            config, healthy_values(restore_gate=1, restore_unixtime=NOW - 86400)
        )
        self.assertEqual(failures, [])
        cases = (
            ("missing", healthy_values(restore_gate=1)),
            ("never ran", healthy_values(restore_gate=1, restore_unixtime=0)),
            (
                "stale",
                healthy_values(restore_gate=1, restore_unixtime=NOW - 9 * 86400),
            ),
        )
        for label, values in cases:
            with self.subTest(case=label):
                failures, _ = verdict(config, values)
                self.assertTrue(mentions(failures, RESTORE_FRESHNESS), failures)

    def test_disabled_drill_needs_no_restore_evidence(self):
        for environment in ("production", "laboratory"):
            with self.subTest(environment=environment):
                config = declaration(environment, schedule="disabled")
                failures, queried = verdict(config, healthy_values(restore_gate=0))
                self.assertEqual(failures, [])
                self.assertNotIn(RESTORE_FRESHNESS, queried)

    def test_restore_freshness_published_as_zero_is_ignored_when_disabled(self):
        # f11 publikuje 0, gdy nie bylo drillu; to nie moze psuc werdyktu.
        config = declaration("production", schedule="disabled")
        failures, _ = verdict(
            config, healthy_values(restore_gate=0, restore_unixtime=0)
        )
        self.assertEqual(failures, [])

    def test_disabled_drill_still_requires_the_matching_zero_gate(self):
        config = declaration("production", schedule="disabled")
        failures, _ = verdict(config, healthy_values(restore_gate=1))
        self.assertTrue(
            mentions(failures, "isa_restore_test_monitoring_enabled"), failures
        )

    def test_backup_evidence_is_still_required_without_drills(self):
        config = declaration("production", schedule="disabled")
        healthy = healthy_values(restore_gate=0)
        missing = dict(healthy)
        del missing["galera_backup_last_success_unixtime"]
        stale_age = (BASE.BACKUP_FRESHNESS_SLA_HOURS + 1) * 3600
        cases = (
            ("missing", missing, "galera_backup_last_success_unixtime"),
            (
                "stale",
                dict(healthy, galera_backup_last_success_unixtime=NOW - stale_age),
                "galera_backup_last_success_unixtime",
            ),
            (
                "failed run",
                dict(healthy, galera_backup_last_run_success=0),
                "galera_backup_last_run_success",
            ),
        )
        for label, values, metric in cases:
            with self.subTest(case=label):
                failures, _ = verdict(config, values)
                self.assertTrue(mentions(failures, metric), failures)

    def test_every_backup_runner_metric_is_required_when_backup_is_enabled(self):
        # Kazda z piecu metryk runnera (ISC-49) z osobna jest wymagana; starego
        # `isa_backup_last_success_unixtime` nikt juz nie publikuje.
        runner_metrics = (
            "galera_backup_last_success_unixtime",
            "galera_backup_last_failure_unixtime",
            "galera_backup_last_run_success",
            "galera_backup_last_size_bytes",
            "galera_backup_last_duration_seconds",
        )
        for schedule in (SCHEDULED, "disabled"):
            config = declaration(schedule=schedule)
            _, _, required = BASE.lifecycle_expectations(config)
            self.assertEqual(sorted(required), sorted(runner_metrics))
            restore_gate = 1 if schedule == SCHEDULED else 0
            restore_unixtime = NOW - 86400 if schedule == SCHEDULED else None
            for metric in runner_metrics:
                with self.subTest(schedule=schedule, missing=metric):
                    values = healthy_values(
                        restore_gate=restore_gate, restore_unixtime=restore_unixtime
                    )
                    del values[metric]
                    failures, _ = verdict(config, values)
                    self.assertTrue(mentions(failures, metric), failures)

    def test_tls_evidence_is_still_required_without_drills(self):
        config = declaration("production", schedule="disabled", tls_mode="full")
        healthy = healthy_values(restore_gate=0)
        absent = dict(healthy)
        del absent["isa_tls_cert_expiry_unixtime"]
        no_gate = dict(healthy)
        del no_gate["isa_tls_monitoring_enabled"]
        cases = (
            ("expiry absent", absent, "TLS cert expiry"),
            (
                "expired",
                dict(healthy, isa_tls_cert_expiry_unixtime=NOW - 86400),
                "TLS cert expiry",
            ),
            ("gate absent", no_gate, "isa_tls_monitoring_enabled"),
        )
        for label, values, metric in cases:
            with self.subTest(case=label):
                failures, _ = verdict(config, values)
                self.assertTrue(mentions(failures, metric), failures)

    def test_cluster_without_backup_needs_neither_backup_nor_restore_evidence(self):
        config = declaration(backup_enabled=False, schedule=SCHEDULED)
        failures, queried = verdict(
            config, healthy_values(restore_gate=0, backup=False)
        )
        self.assertEqual(failures, [])
        self.assertNotIn(RESTORE_FRESHNESS, queried)
        self.assertNotIn("galera_backup_last_success_unixtime", queried)


class ProbeDeclarationWiringTests(unittest.TestCase):
    def test_production_declaration_queries_no_restore_freshness(self):
        probe = load_probe(declaration("production", schedule="disabled"))
        self.assertEqual(probe.ENVIRONMENT, "production")
        self.assertNotIn(RESTORE_FRESHNESS, probe.ALL_STATE_METRICS)
        self.assertEqual(
            probe.EXPECTED_CONFIG_METRICS["isa_restore_test_monitoring_enabled"], 0
        )
        self.assertIn("galera_backup_last_success_unixtime", probe.ALL_STATE_METRICS)
        self.assertIn("isa_tls_cert_expiry_unixtime", probe.ALL_STATE_METRICS)

    def test_scheduled_declaration_queries_restore_freshness(self):
        probe = load_probe(declaration("laboratory", schedule=SCHEDULED))
        self.assertIn(RESTORE_FRESHNESS, probe.ALL_STATE_METRICS)
        self.assertEqual(
            probe.EXPECTED_CONFIG_METRICS["isa_restore_test_monitoring_enabled"], 1
        )


class ProbeAlertExpectationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = BASE.read_alerts_source()

    def suffixes(self, config):
        expected, shared = BASE.expected_alert_rule_uids(config, self.source)
        label = config["monitoring"]["pmm"]["cluster_name"]
        prefix = BASE.alert_uid_prefixes(label)[0] + "-"
        self.assertTrue(all(uid.startswith(prefix) for uid in expected))
        self.assertFalse(expected & shared)
        return {uid[len(prefix):] for uid in expected}

    def test_scheduled_drill_expects_the_stale_rule(self):
        suffixes = self.suffixes(declaration(schedule=SCHEDULED))
        for suffix in (
            "restore-drill-stale",
            "backup-stale",
            "backup-failed",
            "metrics-frozen",
            "tls-cert-expiring",
            "node-loss",
        ):
            self.assertIn(suffix, suffixes)

    def test_disabled_drill_expects_backup_rules_but_no_stale_rule(self):
        suffixes = self.suffixes(declaration("production", schedule="disabled"))
        self.assertNotIn("restore-drill-stale", suffixes)
        for suffix in ("backup-stale", "backup-failed", "metrics-frozen", "node-loss"):
            self.assertIn(suffix, suffixes)

    def test_cluster_without_backup_expects_no_backup_or_restore_rules(self):
        suffixes = self.suffixes(declaration(backup_enabled=False, schedule=SCHEDULED))
        for suffix in (
            "restore-drill-stale",
            "backup-stale",
            "backup-failed",
            "metrics-frozen",
        ):
            self.assertNotIn(suffix, suffixes)
        self.assertIn("node-loss", suffixes)


class PmmRuntimeContractTests(unittest.TestCase):
    LOCKED = BASE.EXPECTED_PMM_VERSION

    def test_production_accepts_the_locked_version_from_any_distribution(self):
        for method in (DOCKER, "DISTRIBUTION_METHOD_AMI", "DISTRIBUTION_METHOD_OVF", None):
            with self.subTest(method=method):
                version = {"version": self.LOCKED}
                if method:
                    version["distribution_method"] = method
                self.assertIsNone(
                    BASE.pmm_runtime_failure(version, self.LOCKED, "production")
                )

    def test_production_fails_on_any_version_mismatch(self):
        for method in (DOCKER, "DISTRIBUTION_METHOD_AMI"):
            with self.subTest(method=method):
                version = {"version": "0.0.0", "distribution_method": method}
                failure = BASE.pmm_runtime_failure(version, self.LOCKED, "production")
                self.assertIsNotNone(failure)
                self.assertIn(self.LOCKED, failure)

    def test_laboratory_and_staging_keep_the_docker_invariant(self):
        for environment in ("laboratory", "staging", ""):
            with self.subTest(environment=environment):
                self.assertIsNone(
                    BASE.pmm_runtime_failure(
                        {"version": self.LOCKED, "distribution_method": DOCKER},
                        self.LOCKED,
                        environment,
                    )
                )
                for version in (
                    {"version": self.LOCKED, "distribution_method": "DISTRIBUTION_METHOD_AMI"},
                    {"version": self.LOCKED},
                    {"version": "0.0.0", "distribution_method": DOCKER},
                ):
                    self.assertIsNotNone(
                        BASE.pmm_runtime_failure(version, self.LOCKED, environment),
                        version,
                    )


class F15EffectiveRulesTests(unittest.TestCase):
    """Wyliczenie `f15_effective_rules` dla kombinacji backup x harmonogram drillu."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="f15-effective-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "ansible.cfg").write_text(
            "[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n",
            encoding="utf-8",
        )
        play = yaml.safe_load(F15.read_text(encoding="utf-8"))[0]
        # Ten sam `vars`/`vars_files` co prawdziwy play; zamiast zadan API jedno
        # zadanie zapisuje wyliczone UID-y.
        render = {
            "name": "Render f15_effective_rules",
            "hosts": play["hosts"],
            "connection": play["connection"],
            "gather_facts": False,
            "vars_files": [str(F15.parent / name) for name in play["vars_files"]],
            "vars": play["vars"],
            "tasks": [
                {
                    "ansible.builtin.copy": {
                        "dest": "{{ rendered_path }}",
                        "content": "{{ f15_effective_rules | map(attribute='uid') | list | to_json }}",
                    }
                }
            ],
        }
        self.playbook = self.root / "render.yml"
        self.playbook.write_text(yaml.safe_dump([render], sort_keys=False), encoding="utf-8")

    def render(self, config):
        cluster_file = self.root / "cluster.yml"
        cluster_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        rendered = self.root / "rendered.json"
        if rendered.exists():
            rendered.unlink()
        env = dict(os.environ)
        env.update(
            {
                "ANSIBLE_NOCOLOR": "1",
                "ANSIBLE_LOCAL_TEMP": str(self.root / "ansible-tmp"),
                "ANSIBLE_CONFIG": str(self.root / "ansible.cfg"),
                "HOME": str(self.root),
            }
        )
        result = subprocess.run(
            [
                ANSIBLE_PLAYBOOK,
                str(self.playbook),
                "-i",
                "localhost,",
                "-c",
                "local",
                "-e",
                f"@{cluster_file}",
                "-e",
                f"rendered_path={rendered}",
                "-e",
                f"ansible_python_interpreter={sys.executable}",
            ],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(rendered.read_text(encoding="utf-8"))

    @unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostępny w PATH")
    def test_rules_follow_backup_and_schedule_combinations(self):
        source = BASE.read_alerts_source()
        cases = (
            ("backup on, scheduled", True, SCHEDULED, True, True),
            ("backup on, disabled", True, "disabled", True, False),
            ("backup off, scheduled", False, SCHEDULED, False, False),
            ("backup off, disabled", False, "disabled", False, False),
        )
        for label, backup_enabled, schedule, backup_rules, restore_rule in cases:
            with self.subTest(case=label):
                config = declaration(backup_enabled=backup_enabled, schedule=schedule)
                cluster_label = config["monitoring"]["pmm"]["cluster_name"]
                prefix = BASE.alert_uid_prefixes(cluster_label)[0] + "-"
                rendered = self.render(config)
                self.assertEqual(len(rendered), len(set(rendered)), "zduplikowana regula")
                self.assertEqual(
                    set(rendered),
                    BASE.expected_alert_rule_uids(config, source)[0],
                    "f15 tworzy inne reguly niz oczekuje sonda",
                )
                self.assertEqual(f"{prefix}restore-drill-stale" in rendered, restore_rule)
                self.assertEqual(f"{prefix}backup-stale" in rendered, backup_rules)
                self.assertEqual(f"{prefix}backup-failed" in rendered, backup_rules)
                self.assertIn(f"{prefix}node-loss", rendered)


class FakeGrafana:
    """Minimalna atrapa API provisioningu Grafany, tyle ile wola f15_alerts.yml."""

    RULES = "/graph/api/v1/provisioning/alert-rules"
    CONTACTS = "/graph/api/v1/provisioning/contact-points"
    POLICIES = "/graph/api/v1/provisioning/policies"

    def __init__(self):
        self.lock = threading.Lock()
        self.rules = {}
        self.folders = []
        self.contacts = {}
        self.policy = {"receiver": "default", "routes": []}
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                status, payload = owner.handle(self.command, self.path, body)
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                if payload is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def handle(self, method, path, body):
        with self.lock:
            self.requests.append((method, path))
            if path == "/graph/api/folders":
                if method == "GET":
                    return 200, self.folders
                self.folders.append(body)
                return 200, body
            if path == "/graph/api/datasources":
                return 200, [{"type": "prometheus", "uid": "fake-prometheus"}]
            if path == self.RULES:
                if method == "GET":
                    return 200, list(self.rules.values())
                self.rules[body["uid"]] = body
                return 201, body
            if path.startswith(self.RULES + "/"):
                uid = path.rsplit("/", 1)[1]
                if method == "DELETE":
                    self.rules.pop(uid, None)
                    return 204, None
                self.rules[uid] = body
                return 200, body
            if path == self.CONTACTS:
                if method == "GET":
                    return 200, list(self.contacts.values())
                self.contacts[body["uid"]] = body
                return 202, body
            if path.startswith(self.CONTACTS + "/"):
                self.contacts[path.rsplit("/", 1)[1]] = body
                return 202, body
            if path == self.POLICIES:
                if method == "GET":
                    return 200, self.policy
                self.policy = body
                return 202, body
        return 404, {"message": f"unhandled {method} {path}"}


class F15RestoreRuleReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="f15-restore-gate-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "ansible.cfg").write_text(
            "[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n",
            encoding="utf-8",
        )
        self.grafana = FakeGrafana()
        self.addCleanup(self.grafana.stop)

    def run_f15(self, config):
        cluster_file = self.root / "cluster.yml"
        cluster_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        env = dict(os.environ)
        env.update(
            {
                "ANSIBLE_NOCOLOR": "1",
                "ANSIBLE_LOCAL_TEMP": str(self.root / "ansible-tmp"),
                "ANSIBLE_CONFIG": str(self.root / "ansible.cfg"),
                "HOME": str(self.root),
                "PMM_SERVER_URL": self.grafana.url,
                "PMM_ADMIN_USER": "admin",
                "PMM_ADMIN_PASSWORD": "fake-admin-secret-0001",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
        )
        return subprocess.run(
            [
                ANSIBLE_PLAYBOOK,
                str(F15),
                "-i",
                "localhost,",
                "-c",
                "local",
                "-e",
                f"@{cluster_file}",
                "-e",
                f"ansible_python_interpreter={sys.executable}",
            ],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )

    def managed(self, label):
        return {
            uid
            for uid, rule in self.grafana.rules.items()
            if (rule.get("labels") or {}).get("managed_by") == "ansible"
            and (rule.get("labels") or {}).get("cluster") == label
        }

    def diagnostics(self, result):
        return f"{result.stdout}\n{result.stderr}\nrequests={self.grafana.requests}"

    @unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostępny w PATH")
    def test_cutover_to_disabled_drills_removes_the_managed_restore_rule(self):
        scheduled = declaration("laboratory", schedule=SCHEDULED)
        production = declaration("production", schedule="disabled")
        label = scheduled["monitoring"]["pmm"]["cluster_name"]
        source = BASE.read_alerts_source()
        stale_uid = BASE.alert_uid_prefixes(label)[0] + "-restore-drill-stale"
        # Cudze reguly o tej samej przyrostkowej nazwie: inny klaster w tym samym
        # PMM oraz regula reczna tego klastra (bez `managed_by=ansible`).
        foreign = {
            "isa-other-restore-drill-stale": {
                "uid": "isa-other-restore-drill-stale",
                "labels": {"managed_by": "ansible", "cluster": "other"},
            },
            "hand-made-restore-drill-stale": {
                "uid": "hand-made-restore-drill-stale",
                "labels": {"cluster": label},
            },
        }
        self.grafana.rules.update(foreign)

        first = self.run_f15(scheduled)
        self.assertEqual(first.returncode, 0, self.diagnostics(first))
        self.assertEqual(
            self.managed(label),
            BASE.expected_alert_rule_uids(scheduled, source)[0],
            "reguly zalozone w PMM rozjechaly sie z oczekiwaniem sondy (drill planowany)",
        )
        self.assertIn(stale_uid, self.managed(label))

        second = self.run_f15(production)
        self.assertEqual(second.returncode, 0, self.diagnostics(second))
        self.assertEqual(
            self.managed(label),
            BASE.expected_alert_rule_uids(production, source)[0],
            "reguly w PMM rozjechaly sie z oczekiwaniem sondy (drill wylaczony)",
        )
        self.assertNotIn(stale_uid, self.grafana.rules)
        self.assertIn(
            ("DELETE", f"{FakeGrafana.RULES}/{stale_uid}"), self.grafana.requests
        )
        self.assertIn(BASE.alert_uid_prefixes(label)[0] + "-backup-stale", self.grafana.rules)
        for uid in foreign:
            self.assertIn(uid, self.grafana.rules, "f15 skasowal regule spoza swojego zakresu")


if __name__ == "__main__":
    unittest.main()
