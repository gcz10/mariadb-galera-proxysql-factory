#!/usr/bin/env python3
"""Deklaracja klastra rzadzi receptura — dla monitoringu I dla backupu.

POWSTAL Z DWOCH POMYLEK, NIE JEDNEJ.

Najpierw wylaczalny byl backup, a monitoring nie: schemat wymagal bloku `pmm`
bezwarunkowo, `cluster-build` wolal `cluster-monitoring` zawsze, a sonda PMM
siedziala w bramie bez warunku. Klaster deweloperski albo obserwowany cudzym
systemem nie mial jak tego zadeklarowac.

Przy naprawie tamtego napisalem tutaj, ze `backup.enabled: false` jest "juz
konfiguracja pierwszej klasy". To bylo NIEPRAWDA i kosztowalo pelna budowe:
honorowaly go SONDY, nie RECEPTY. Najemca `nova-r9` z wylaczonym backupem
przeszedl deploy, bootstrap, join, ProxySQL, monitoring i hardening, po czym
padl na `cluster-backup-configure`, ktory zazadal poswiadczen S3 magazynu
swiadomie nieistniejacego. Jedynym obejsciem bylo `BUILD_SKIP=backup`, czyli
powtorzenie w wywolaniu tego, co juz stalo w konfiguracji.

Dlatego kontrakt jest teraz WSPOLNY i sparametryzowany przelacznikiem: kazdy
nowy przelacznik ma trafic do `SWITCHES`, zamiast dorabiac sobie osobny plik i
osobna (rozjezdzajaca sie) semantyke.
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
GATE_SCRIPT = REPO / "tests" / "validation" / "gate-build.sh"

# przelacznik -> (zmienna make, cele, sonda, marker skipu w sondzie)
SWITCHES = {
    "monitoring": (
        "monitoring_enabled",
        ("cluster-monitoring", "cluster-monitoring-refresh", "cluster-alerts"),
        REPO / "tests" / "lab" / "probe-pmm-native.py",
        "SKIP: monitoring wylaczony",
    ),
    "backup": (
        "backup_enabled",
        ("cluster-backup-configure", "cluster-backup", "cluster-restore-drill"),
        REPO / "tests" / "lab" / "probe-backup.py",
        "SKIP: backup wylaczony",
    ),
}


class DeclarationGatesContractTests(unittest.TestCase):
    def test_schema_accepts_every_switch(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        for block in SWITCHES:
            with self.subTest(block=block):
                self.assertEqual(
                    schema["properties"][block]["properties"]["enabled"]["type"],
                    "boolean",
                )

    def test_schema_still_demands_pmm_address(self):
        """Wylaczyc monitoring wolno; sklamac o jego adresie — nie.

        Gdyby `pmm` przestal byc wymagany, klaster z `enabled: true` mogl by
        przejsc walidacje bez wskazania, GDZIE ten PMM jest, i pasc dopiero na
        maszynie.
        """
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertIn("pmm", schema["properties"]["monitoring"]["required"])

    def test_targets_are_gated_in_the_same_shell_as_their_work(self):
        """Bramka i playbooki MUSZA byc w jednej powloce.

        Kazda linia recepty make to osobna powloka, wiec `exit 0` w linii
        strazniczej nie pomija kolejnych. Pierwsza wersja bramki monitoringu
        wygladala poprawnie i mimo to uruchamiala wszystkie playbooki —
        dlatego sprawdzamy WLASNOSC (kontynuacja linii), nie brzmienie warunku.
        """
        text = MAKEFILE.read_text(encoding="utf-8")
        for block, (var, targets, _, _) in SWITCHES.items():
            for target in targets:
                with self.subTest(target=target):
                    body = re.search(rf"^{target}:.*?(?=\n\S|\Z)", text, re.S | re.M)
                    self.assertIsNotNone(body, f"brak celu {target}")
                    recipe = body.group(0)
                    self.assertIn(
                        f"$({var})", recipe, f"{target} nie sprawdza deklaracji {block}"
                    )
                    gate_line = next(
                        line for line in recipe.splitlines() if f"$({var})" in line
                    )
                    self.assertTrue(
                        gate_line.rstrip().endswith("\\"),
                        f"{target}: straznik konczy linie, wiec `exit 0` nie pominie "
                        "kolejnych powlok — dolacz korpus przez kontynuacje linii",
                    )
                    self.assertIn("exit 0", gate_line)

    def test_gates_read_the_declaration_from_the_cluster_file(self):
        """Wartosc ma pochodzic z cluster.yml, nie z pamieci operatora."""
        text = MAKEFILE.read_text(encoding="utf-8")
        for var in (v for v, _, _, _ in SWITCHES.values()):
            with self.subTest(var=var):
                self.assertRegex(text, rf"{var}\s*=\s*\$\(shell[^\n]*cluster\.yml")

    def test_seed_coupling_guard_respects_disabled_backup(self):
        """Na klastrze bez kopii nie pytamy o dane dla drillu, ktory nie nastapi.

        Straznik sprzezenia seed->backup zadal EXISTING_DATA=yes takze wtedy,
        gdy backup byl wylaczony deklaracja — czyli wymuszal odpowiedz na
        pytanie o przebieg, ktorego nie bedzie.

        Od F4 straznik zyje w tests/validation/gate-build.sh (preflight);
        Makefile musi mu tylko za plombowac deklaracje i przelaczniki.
        """
        recipe = (GATE_SCRIPT.read_text(encoding="utf-8"))
        # Szukamy WARUNKU, nie slowa: EXISTING_DATA (w komunikacie bledu) nie
        # mierzy kolejnosci — warunek porownania tak.
        condition = '[ "$existing_data" != "yes" ]'
        self.assertIn(condition, recipe)
        self.assertLess(
            recipe.index('[ "$backup_enabled" = "true" ]'),
            recipe.index(condition),
            "warunek EXISTING_DATA musi lezec WEWNATRZ bramki backup_enabled",
        )
        makefile = MAKEFILE.read_text(encoding="utf-8")
        build = re.search(r"^cluster-build:.*?(?=\n\S|\Z)", makefile, re.S | re.M)
        self.assertIsNotNone(build, "brak celu cluster-build")
        self.assertIn(
            'gate-build.sh preflight "$(backup_enabled)" "$(BUILD_SKIP)" "$(EXISTING_DATA)"',
            build.group(0),
            "cluster-build musi przekazac deklaracje backupu i przelaczniki do bramki",
        )

    def test_probes_skip_instead_of_failing(self):
        """Sonda ma powiedziec, ze nie mierzy — nie udawac zielonej ani czerwonej."""
        for block, (_, _, probe, marker) in SWITCHES.items():
            with self.subTest(block=block):
                self.assertIn(marker, probe.read_text(encoding="utf-8"))

    def test_monitoring_probe_runs_normally_when_declared(self):
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
            [sys.executable, str(REPO / "tests" / "lab" / "probe-pmm-native.py")],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertNotIn("SKIP: monitoring wylaczony", result.stdout)


if __name__ == "__main__":
    unittest.main()
