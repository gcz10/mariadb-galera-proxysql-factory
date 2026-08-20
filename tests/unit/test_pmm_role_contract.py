#!/usr/bin/env python3
"""Kontrakt roli pmm oraz wrapperow f11_pmm_client.yml / f11_pmm_agent.yml.

POWSTAL PRZY REFAKTORZE F11: logika dwoch playbookow PMM zyje w jednej roli
roles/pmm z parametrem pmm_mode (agent|agentless), a stare playbooki sa
cienkimi wrapperami. Ten test pilnuje kontraktu refaktoru:

1. PUBLICZNE API: oba playbooki zostaja wywolywalne bez zmiany Makefile/CI —
   te same plays (nazwy, hosts, serial, become) co przed refaktorem, kazdy
   play deleguje do roli pmm z poprawna para (pmm_mode, pmm_stage).
2. GRAF SCEN: dispatcher roli include'uje wylacznie istniejace pliki taskow,
   a kazdy plik sceny jest poprawna lista zadan (CI nie wchodzi w dynamiczne
   include_tasks, wiec to musi pilowac test).
3. ROZDZIELENIE TRYBOW: instalacja RPM/pmm-agent tylko w agent_install.yml,
   rejestracja generic/external (agentless) tylko w agentless_register.yml.
4. DEDUPLIKACJA AUTH: zadne zadanie uri w roli nie nosi danych uwierzytelniajacych
   bezposrednio — auth wylacznie przez module_defaults na wywolaniu roli.
5. SEMANTIKA: monitoring.agent_groups, credentials_revision, QAN wg
   monitoring.qan_source, owner/consumer ProxySQL oraz push/pull zostaly
   zachowane w odpowiednich trybach.
6. SEKRET SETUP: zadanie "Zarejestruj wezel" ma sekret w task-level
   environment z no_log (lustrzane wymaganie do test_secret_scope).
"""

import os
import unittest

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIENT_PB = os.path.join(REPO, "playbooks", "f11_pmm_client.yml")
AGENT_PB = os.path.join(REPO, "playbooks", "f11_pmm_agent.yml")
ROLE_TASKS = os.path.join(REPO, "roles", "pmm", "tasks")
ROLE_DEFAULTS = os.path.join(REPO, "roles", "pmm", "defaults", "main.yml")
# Zdjecie stanu sprzed refaktoru: (nazwa play, hosts, scena) w kolejnosci wykonywania.
# To jest publiczne API, na ktorym bazuja Makefile i sonda probe-pmm-native.py.
CLIENT_PLAYS = [
    ("F11 - Sprawdź PMM API przed zmianami na hostach", "localhost", "preflight"),
    ("F11 - Przygotuj konto monitorujące MariaDB", "galera", "monitor_account"),
    ("F11 - Zarejestruj natywne obiekty PMM", "localhost", "register"),
    ("F11 - Usuń zastąpiony monitoring standalone", "galera", "legacy_cleanup"),
]
AGENT_PLAYS = [
    ("F11 — zainstaluj i zarejestruj pmm-agent", "all", "install"),
    ("F11 — zarejestruj uslugi bazodanowe pod lokalnym agentem", "localhost", "register"),
]
# Stage, ktore uzywaja uri — ich wywolania roli wymagaja module_defaults.
URI_STAGES = {"preflight", "register"}

EXPECTED_STAGE_FILES = {
    "agent_install.yml",
    "agent_register.yml",
    "agentless_preflight.yml",
    "agentless_register.yml",
    "agentless_legacy.yml",
    "monitor_account.yml",
}

AGENT_ONLY_MARKERS = ["percona-release", "pmm_client.release_rpm", "'pmm-agent', 'setup'"]
AGENTLESS_ONLY_MARKERS = ["external_exporter", "generic:"]


def load_yaml(path):
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)

def read_text(path):
    """Czyta plik kontraktu bez pozostawiania otwartego deskryptora."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()
def role_invocations(play):
    """Lista wywolan roli pmm w playu (sekcja roles)."""
    out = []
    for entry in play.get("roles") or []:
        if isinstance(entry, dict) and entry.get("role") == "pmm":
            out.append(entry)
    return out


def walk_tasks(items):
    """Rekursywne przejscie listy zadan (block/rescue/always)."""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if "block" in item:
            for section in ("block", "rescue", "always"):
                yield from walk_tasks(item.get(section))
            continue
        yield item


class WrapperContractTests(unittest.TestCase):
    def test_client_wrapper_plays_delegate_to_role(self):
        plays = load_yaml(CLIENT_PB)
        self.assertEqual(len(plays), len(CLIENT_PLAYS))
        for play, (name, hosts, stage) in zip(plays, CLIENT_PLAYS):
            with self.subTest(play=name):
                self.assertEqual(play.get("name"), name)
                self.assertEqual(play.get("hosts"), hosts)
                invocations = role_invocations(play)
                self.assertEqual(len(invocations), 1, "play musi delegowac dokladnie do jednej roli pmm")
                varz = invocations[0].get("vars", {})
                self.assertEqual(varz.get("pmm_mode"), "agentless")
                self.assertEqual(varz.get("pmm_stage"), stage)
                if stage in URI_STAGES:
                    md = invocations[0].get("module_defaults", {}).get("ansible.builtin.uri", {})
                    for key in ("url_username", "url_password", "force_basic_auth", "validate_certs"):
                        self.assertIn(key, md, f"module_defaults uri musi zawierac {key}")

    def test_agent_wrapper_plays_delegate_to_role(self):
        plays = load_yaml(AGENT_PB)
        self.assertEqual(len(plays), len(AGENT_PLAYS))
        for play, (name, hosts, stage) in zip(plays, AGENT_PLAYS):
            with self.subTest(play=name):
                self.assertEqual(play.get("name"), name)
                self.assertEqual(play.get("hosts"), hosts)
                invocations = role_invocations(play)
                self.assertEqual(len(invocations), 1, "play musi delegowac dokladnie do jednej roli pmm")
                varz = invocations[0].get("vars", {})
                self.assertEqual(varz.get("pmm_mode"), "agent")
                self.assertEqual(varz.get("pmm_stage"), stage)
                if stage in URI_STAGES:
                    md = invocations[0].get("module_defaults", {}).get("ansible.builtin.uri", {})
                    for key in ("url_username", "url_password", "force_basic_auth", "validate_certs"):
                        self.assertIn(key, md, f"module_defaults uri musi zawierac {key}")

    def test_agent_install_play_keeps_serial_and_environment(self):
        """serial:1 i play-level environment to warunki brzegowe instalacji agenta."""
        plays = load_yaml(AGENT_PB)
        first = plays[0]
        self.assertEqual(first.get("serial"), 1, "instalacja agenta MUSI byc rolling (serial:1)")
        env = first.get("environment", {})
        for key in (
            "PMM_AGENT_SERVER_USERNAME",
            "PMM_AGENT_SERVER_INSECURE_TLS",
            "PMM_AGENT_CONFIG_FILE",
            "PMM_AGENT_SETUP_CUSTOM_LABELS",
        ):
            self.assertIn(key, env, f"environment playu instalacji musi zachowac {key}")
        self.assertNotIn(
            "PMM_AGENT_SERVER_PASSWORD",
            env,
            "sekret NIE moze wrocic na poziom play (ISC-43)",
        )

    def test_wrappers_contain_no_inline_tasks(self):
        """Wrapper jest cienki; logika zadan zyje w roli."""
        for path in (CLIENT_PB, AGENT_PB):
            for play in load_yaml(path):
                for section in ("tasks", "pre_tasks", "post_tasks"):
                    self.assertFalse(
                        play.get(section),
                        f"{os.path.basename(path)}: {section} playu '{play.get('name')}' musi byc puste",
                    )


class RoleStructureTests(unittest.TestCase):
    def test_dispatcher_includes_exactly_expected_stage_files(self):
        main = load_yaml(os.path.join(ROLE_TASKS, "main.yml"))
        included = {
            task["ansible.builtin.include_tasks"]
            for task in walk_tasks(main)
            if "ansible.builtin.include_tasks" in task
        }
        self.assertEqual(included, EXPECTED_STAGE_FILES)

    def test_dispatcher_fails_closed_on_unknown_mode_or_stage(self):
        main = load_yaml(os.path.join(ROLE_TASKS, "main.yml"))
        guards = [
            task for task in walk_tasks(main)
            if isinstance(task.get("ansible.builtin.fail"), dict)
        ]
        self.assertTrue(guards, "dispatcher musi odrzucac nieznane pary tryb/scena (fail-closed)")
    def test_stage_files_are_task_lists(self):
        """CI nie wchodzi w dynamiczne include_tasks — poprawnosc plikow scen pilnuje test."""
        for filename in sorted(EXPECTED_STAGE_FILES):
            with self.subTest(stage_file=filename):
                data = load_yaml(os.path.join(ROLE_TASKS, filename))
                self.assertIsInstance(data, list)
                self.assertTrue(data)
                for task in data:
                    self.assertIsInstance(task, dict)
                    self.assertIn("name", task, "kazde zadanie sceny musi miec name")

    def test_agent_only_logic_confined_to_agent_files(self):
        files = {name: read_text(os.path.join(ROLE_TASKS, name))
                 for name in EXPECTED_STAGE_FILES}
        for marker in AGENT_ONLY_MARKERS:
            holders = [name for name, body in files.items() if marker in body]
            self.assertEqual(
                holders,
                ["agent_install.yml"],
                f"marker '{marker}' agenta wyciekl poza agent_install.yml",
            )
        for marker in AGENTLESS_ONLY_MARKERS:
            holders = [name for name, body in files.items() if marker in body]
            self.assertEqual(
                holders,
                ["agentless_register.yml"],
                f"marker '{marker}' agentless wyciekl poza agentless_register.yml",
            )

    def test_monitor_account_stage_is_shared(self):
        """Konto pmm_monitor tworzy JEDNA wspolna sciezka dla obu trybow."""
        install = read_text(os.path.join(ROLE_TASKS, "agent_install.yml"))
        self.assertIn("monitor_account.yml", install, "agent_install.yml musi wspoltworzyc konto przez monitor_account.yml")
        shared = load_yaml(os.path.join(ROLE_TASKS, "monitor_account.yml"))
        users = [
            task for task in shared
            if isinstance(task.get("ansible.mysql.mysql_user"), dict)
        ]
        self.assertEqual(len(users), 1, "monitor_account.yml tworzy dokladnie jedno konto")

    def test_uri_tasks_carry_no_inline_auth(self):
        """Auth uri zyje w module_defaults wywolan roli, nie w zadaniach."""
        for filename in sorted(EXPECTED_STAGE_FILES):
            for task in walk_tasks(load_yaml(os.path.join(ROLE_TASKS, filename))):
                if "ansible.builtin.uri" not in task:
                    continue
                with self.subTest(stage_file=filename, task=task.get("name")):
                    uri_args = task["ansible.builtin.uri"]
                    for key in ("url_username", "url_password", "force_basic_auth", "validate_certs"):
                        self.assertNotIn(
                            key, uri_args,
                            f"zadanie uri nie moze nosic {key} — auth idzie z module_defaults",
                        )


class SemanticsPreservedTests(unittest.TestCase):
    def test_agent_groups_still_gate_both_modes(self):
        defaults = read_text(ROLE_DEFAULTS)
        self.assertIn("monitoring.agent_groups", defaults, "defaults musi liczyc hosty agentowe z monitoring.agent_groups")
        install = read_text(os.path.join(ROLE_TASKS, "agent_install.yml"))
        self.assertIn("pmm_agent_target", install, "agent_install.yml dalej pomija hosty spoza monitoring.agent_groups")

    def test_credentials_revision_labels_in_both_modes(self):
        for filename in ("agentless_register.yml", "agent_register.yml"):
            body = read_text(os.path.join(ROLE_TASKS, filename))
            self.assertIn(
                "credentials_revision", body,
                f"{filename} musi etykietowac eksportery rewizja hasel (kontrola rotacji)",
            )

    def test_qan_source_still_dynamic_in_agent_mode(self):
        agent = read_text(os.path.join(ROLE_TASKS, "agent_register.yml"))
        self.assertIn("monitoring.qan_source", agent, "agent_register.yml musi tworzyc QAN wg monitoring.qan_source")

    def test_proxysql_owner_consumer_split_preserved(self):
        defaults = read_text(ROLE_DEFAULTS)
        self.assertIn(
            "proxysql.role | default('owner')",
            defaults,
            "defaults musi zachowac wykluczenie wezlow ProxySQL konsumenta",
        )

    def test_push_metrics_only_in_agent_mode(self):
        agent = read_text(os.path.join(ROLE_TASKS, "agent_register.yml"))
        agentless = read_text(os.path.join(ROLE_TASKS, "agentless_register.yml"))
        self.assertIn("push_metrics", agent, "tryb agenta wymusza push (pull = cichy up=0 za firewallem)")
        self.assertNotIn("push_metrics", agentless, "sciezka agentless nie ustawia push_metrics")


class SecretRegistrationTaskTests(unittest.TestCase):
    def test_setup_task_keeps_secret_in_task_env_with_no_log(self):
        tasks = walk_tasks(load_yaml(os.path.join(ROLE_TASKS, "agent_install.yml")))
        setup = [
            task for task in tasks
            if "Zarejestruj wezel" in task.get("name", "") or "pmm-agent setup" in task.get("name", "")
        ]
        self.assertTrue(setup, "zadanie rejestracji wezla musi istniec w agent_install.yml")
        for task in setup:
            self.assertIn(
                "PMM_AGENT_SERVER_PASSWORD",
                task.get("environment", {}),
                "sekret rejestracji musi byc w task-level environment",
            )
            self.assertTrue(task.get("no_log"), "zadanie z sekretem musi miec no_log: true")


if __name__ == "__main__":
    unittest.main()
