#!/usr/bin/env python3
"""Rejestracja w PMM jest DEKLARACJA klastra, nie zalozeniem fabryki.

POWSTAL Z TEJ SAMEJ ASYMETRII CO BACKUP. `backup.enabled: false` bylo
konfiguracja pierwszej klasy, a monitoring — nie: schemat wymagal bloku `pmm`
bezwarunkowo, `cluster-build` wolal `cluster-monitoring` zawsze, a sonda PMM
siedziala w bramie po budowie bez zadnego warunku. Klaster deweloperski albo
obserwowany cudzym systemem (Zabbix, Datadog, wlasny Prometheus) nie mial jak
tego zadeklarowac: budowa zadala PMM_ADMIN_PASSWORD i probowala rejestrowac
wezly w serwerze, ktorego moglo nie byc, a brama oblewala klaster za brak
rejestracji, ktorej nikt nie obiecal.

Kontrakt: `monitoring.enabled: false` wylacza rejestracje i sondy, a pominiecie
pola zachowuje dotychczasowe zachowanie.
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCHEMA = REPO / "clusters" / "schema" / "cluster.schema.json"
MAKEFILE = REPO / "Makefile"
PROBE = REPO / "tests" / "lab" / "probe-pmm-native.py"


class MonitoringOptionalContractTests(unittest.TestCase):
    def test_schema_accepts_the_switch_and_still_demands_pmm_address(self):
        """Wylaczyc monitoring wolno; sklamac o jego adresie — nie.

        Gdyby `pmm` przestal byc wymagany, klaster z `enabled: true` mogl by
        przejsc walidacje bez wskazania, GDZIE ten PMM jest, i paść dopiero
        na maszynie.
        """
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        monitoring = schema["properties"]["monitoring"]
        self.assertEqual(monitoring["properties"]["enabled"]["type"], "boolean")
        self.assertIn("pmm", monitoring["required"])

    def test_targets_are_gated_in_the_same_shell_as_their_work(self):
        """Bramka i playbooki MUSZA byc w jednej powloce.

        Kazda linia recepty make to osobna powloka, wiec `exit 0` w linii
        strazniczej nie pomija kolejnych. Pierwsza wersja tej zmiany wygladala
        poprawnie i mimo to uruchamiala wszystkie playbooki przy
        `monitoring.enabled: false` — dlatego sprawdzamy WLASNOSC (kontynuacja
        linii), a nie brzmienie warunku.
        """
        text = MAKEFILE.read_text(encoding="utf-8")
        for target in ("cluster-monitoring", "cluster-monitoring-refresh", "cluster-alerts"):
            with self.subTest(target=target):
                body = re.search(rf"^{target}:.*?(?=\n\S|\Z)", text, re.S | re.M)
                self.assertIsNotNone(body, f"brak celu {target}")
                recipe = body.group(0)
                self.assertIn(
                    "$(monitoring_enabled)",
                    recipe,
                    f"{target} nie sprawdza deklaracji monitoringu",
                )
                gate_line = next(
                    line for line in recipe.splitlines() if "$(monitoring_enabled)" in line
                )
                self.assertTrue(
                    gate_line.rstrip().endswith("\\"),
                    f"{target}: straznik konczy linie, wiec `exit 0` nie pominie "
                    "kolejnych powlok — dolacz korpus przez kontynuacje linii",
                )
                self.assertIn("exit 0", gate_line)

    def test_gate_reads_the_declaration_from_the_cluster_file(self):
        """Wartosc ma pochodzic z cluster.yml, nie z pamieci operatora."""
        text = MAKEFILE.read_text(encoding="utf-8")
        self.assertRegex(text, r"monitoring_enabled\s*=\s*\$\(shell[^\n]*cluster\.yml")

    def test_probe_skips_instead_of_failing(self):
        """Sonda ma powiedziec, ze nie mierzy — nie udawac zielonej ani czerwonej."""
        source = PROBE.read_text(encoding="utf-8")
        self.assertIn("MONITORING_ENABLED = bool(", source)
        self.assertIn("if not MONITORING_ENABLED:", source)
        self.assertRegex(source, r'print\(\s*\n?\s*"SKIP: monitoring wylaczony')

    def test_probe_runs_normally_when_monitoring_is_declared(self):
        """Falsyfikowalnosc: przy wlaczonym monitoringu sonda NIE moze skipowac.

        Bez PMM_ADMIN_PASSWORD konczy sie kodem 2 ("required"), a nie 0 —
        czyli przechodzi obok skipu i wchodzi we wlasciwa sciezke.
        """
        env = {
            "PATH": "/usr/bin:/bin",
            "CLUSTER_CONFIG": str(REPO / "clusters" / "example-cluster" / "cluster.yml"),
            "CLUSTER_INVENTORY": str(REPO / "clusters" / "example-cluster" / "inventory.yml"),
        }
        result = subprocess.run(
            [sys.executable, str(PROBE)], cwd=REPO, capture_output=True, text=True, env=env
        )
        self.assertNotIn("SKIP: monitoring wylaczony", result.stdout)


if __name__ == "__main__":
    unittest.main()
