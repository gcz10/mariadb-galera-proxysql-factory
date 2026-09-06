#!/usr/bin/env python3
"""Weryfikacja certyfikatu PMM ma wynikac z DEKLARACJI, nie z zaszytego `false`.

`monitoring.pmm.validate_certs` jest polem WYMAGANYM przez oba schematy
(klaster i platforma), ale trzy playbooki wysylajace haslo admina PMM mialy
`validate_certs: false` wpisane na sztywno, a `platform_adopt.yml` domyslal sie
`false` przy braku pola. Klaster deklarujacy `true` i tak dostawal polaczenie
bez weryfikacji — deklaracja byla ozdoba.

Test sprawdza dwie rzeczy na PRAWDZIWYCH plikach:
  * zadne zadanie `uri` w repozytorium nie ma literalnego `false`,
  * wyrazenie kazdego playbooka renderuje sie na `true`, gdy deklaracja mowi
    `true`, na `false` gdy mowi `false`, i na `true` gdy pola brakuje
    (fail-closed).
"""

import unittest
from pathlib import Path

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "playbooks"

# playbook -> nazwa zmiennej niosacej decyzje
DECISION_VARS = {
    "cluster_deregister.yml": "pmm_validate_certs",
    "infra_services.yml": "pmm_validate_certs",
    "platform_adopt.yml": "pmm_validate",
    "f15_alerts.yml": "pmm_validate_certs",
    "f11_proxysql_metrics.yml": "pmm_validate_certs_effective",
    "f13_remove_node.yml": "pmm_validate_certs",
}

# `bool` jest filtrem Ansible, nie Jinjy — semantyka jak w ansible.plugins.filter.
ENV = jinja2.Environment()
ENV.filters["bool"] = lambda value: str(value).strip().lower() in ("true", "yes", "on", "1")


def iter_tasks(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from iter_tasks(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_tasks(item)


def play_var(playbook: Path, name: str) -> str:
    for play in yaml.safe_load(playbook.read_text(encoding="utf-8")) or []:
        if isinstance(play, dict) and name in (play.get("vars") or {}):
            return play["vars"][name]
    raise AssertionError(f"{playbook.name}: brak zmiennej {name} w vars play'a")


def render(expression: str, declared) -> str:
    pmm = {"server_url": "https://192.0.2.70", "cluster_name": "probe"}
    if declared is not None:
        pmm["validate_certs"] = declared
    return ENV.from_string(expression).render(monitoring={"pmm": pmm}).strip()


class PmmTlsValidationContractTests(unittest.TestCase):
    def test_no_uri_task_disables_verification_unconditionally(self):
        offenders = []
        for playbook in sorted(PLAYBOOKS.rglob("*.yml")):
            document = yaml.safe_load(playbook.read_text(encoding="utf-8"))
            for task in iter_tasks(document):
                uri = task.get("ansible.builtin.uri") or task.get("uri")
                if isinstance(uri, dict) and uri.get("validate_certs") is False:
                    offenders.append(f"{playbook.name}: {task.get('name')}")
        self.assertEqual(offenders, [], "zaszyte validate_certs: false")

    def test_declaration_decides_in_every_pmm_playbook(self):
        for filename, var in DECISION_VARS.items():
            with self.subTest(playbook=filename):
                expression = play_var(PLAYBOOKS / filename, var)
                self.assertEqual(render(expression, True), "True")
                self.assertEqual(render(expression, False), "False")
                # Brak pola (schema go wymaga) nie moze znaczyc "nie weryfikuj".
                self.assertEqual(render(expression, None), "True")


if __name__ == "__main__":
    unittest.main()
