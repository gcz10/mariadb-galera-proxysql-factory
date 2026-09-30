#!/usr/bin/env python3
"""Produkcja nie adoptuje po cichu zasobow laboratorium.

`infra.services: []` (jedyna deklaracja dozwolona w produkcji) sprawia, ze
infra_services.yml i platform_pmm_backup.yml niczego nie robia. Host, ktory byl
laboratoryjnym hostem infra i zostal przeklasyfikowany, zachowalby wiec dzialajace
kontenery PMM/MinIO/Maildev i timer kopii PMM. Preflight ma to WYKRYC i odmowic —
bez usuwania czegokolwiek i bez blokowania cudzego Dockera.
"""
import re
import unittest
from pathlib import Path

import jinja2
import yaml
from jinja2.nativetypes import NativeEnvironment

REPO = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO / "playbooks" / "platform_preflight.yml"
PLAY_NAME_PREFIX = "Platform Preflight - produkcja odmawia"


def load_play():
    plays = yaml.safe_load(PREFLIGHT.read_text(encoding="utf-8"))
    return next(play for play in plays if play.get("name", "").startswith(PLAY_NAME_PREFIX))


class ProductionArtifactPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.play = load_play()
        cls.tasks = {task["name"]: task for task in cls.play["tasks"]}
        cls.names = [task["name"] for task in cls.play["tasks"]]
        env = NativeEnvironment(undefined=jinja2.StrictUndefined)
        # Filtr i test dostarcza Ansible, nie Jinja.
        env.filters["regex_replace"] = lambda value, pattern, replacement="": re.sub(pattern, replacement, value)
        env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
        cls.env = env
        cls.facts = cls.tasks["Ustal artefakty laboratorium wykryte na hoscie"]["ansible.builtin.set_fact"]

    def evaluate(self, *, paths=(), containers=(), volumes=(), networks=(), docker=True, failing=()):
        """Wynik obu faktow dla stanu hosta opisanego rejestrami zadan."""
        variables = dict(self.play["vars"])
        variables["preflight_lab_stat"] = {
            "results": [
                {"item": path, "stat": {"exists": path in paths}}
                for path in variables["preflight_lab_paths"]
            ]
        }
        variables["preflight_docker_cli"] = {"rc": 0 if docker else 1}

        def register(name, lines):
            if not docker:
                return {"skipped": True, "changed": False}
            return {"rc": 1 if name in failing else 0, "stdout_lines": list(lines)}

        variables["preflight_lab_containers"] = register("containers", containers)
        variables["preflight_lab_volumes"] = register("volumes", volumes)
        variables["preflight_lab_networks"] = register("networks", networks)
        return {
            key: self.env.from_string(self.facts[key]).render(**variables)
            for key in ("preflight_lab_findings", "preflight_docker_unmeasured")
        }

    def test_only_production_scans_and_it_scans_every_owned_host_group(self):
        env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
        for environment, expected in (
            ("production", "proxysql:app:infra"),
            ("laboratory", "all:!all"),
            ("staging", "all:!all"),
        ):
            with self.subTest(environment=environment):
                rendered = env.from_string(self.play["hosts"]).render(
                    platform={"environment": environment}
                )
                self.assertEqual(rendered, expected)

    def test_clean_host_with_unrelated_docker_is_not_blocked(self):
        """Cudzy Docker (rowniez PMM operatora pod innymi nazwami) nie jest artefaktem isa."""
        result = self.evaluate(
            volumes=["pgdata", "pmm-data-backup-20260823-133353", "isa-other-data", "isa-pmm-data-x"],
            networks=["bridge", "host", "isa-infra-x"],
        )
        self.assertEqual(result["preflight_lab_findings"], [])
        self.assertFalse(result["preflight_docker_unmeasured"])

    def test_known_lab_artifacts_are_all_reported(self):
        result = self.evaluate(
            paths=["/etc/isa-infra", "/etc/systemd/system/isa-pmm-backup.timer"],
            containers=["pmm-server", "minio", "maildev"],
            volumes=["isa-pmm-data", "isa-minio-data", "isa-pmm-data-backup-1790000000-3.9.1", "pgdata"],
            networks=["bridge", "isa-infra"],
        )
        self.assertCountEqual(
            result["preflight_lab_findings"],
            [
                "sciezka /etc/isa-infra",
                "sciezka /etc/systemd/system/isa-pmm-backup.timer",
                "kontener pmm-server",
                "kontener minio",
                "kontener maildev",
                "wolumen isa-pmm-data",
                "wolumen isa-minio-data",
                "wolumen isa-pmm-data-backup-1790000000-3.9.1",
                "siec isa-infra",
            ],
        )

    def test_missing_docker_cli_is_measured_from_paths_alone(self):
        result = self.evaluate(paths=["/etc/isa-pmm-backup"], docker=False)
        self.assertEqual(result["preflight_lab_findings"], ["sciezka /etc/isa-pmm-backup"])
        self.assertFalse(result["preflight_docker_unmeasured"])

    def test_docker_that_cannot_be_read_is_unmeasured_not_clean(self):
        """Milczacy Docker nie dowodzi braku zasobow isa-infra."""
        for failing in (("containers",), ("volumes",), ("networks",)):
            with self.subTest(failing=failing):
                result = self.evaluate(failing=failing)
                self.assertTrue(result["preflight_docker_unmeasured"])



if __name__ == "__main__":
    unittest.main()
