"""Kontrakt polityki bramki budowy (tests/validation/gate-build.sh).

Srodowisko (laboratory/staging/production) jest jawnym argumentem preflight
i steps. Testy wykonuja skrypt i sprawdzaja to, co widzi operator: kod wyjscia,
komunikat odmowy i liste celow make, ktore faktycznie wystartowaly (podstawiony
`make` tylko je zapisuje). Odmowa musi zapasc PRZED pierwszym celem make.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "validation" / "gate-build.sh"

LAB_LIKE = ("laboratory", "staging")

LAB_FULL_PLAN = [
    "lab-seed-smoke",
    "cluster-app-host",
    "cluster-backup-configure",
    "cluster-backup",
    "cluster-restore-drill CONFIRM=yes",
    "cluster-monitoring-refresh",
    "cluster-alerts",
]
PRODUCTION_FULL_PLAN = [
    "cluster-app-host",
    "cluster-backup-configure",
    "cluster-backup",
    "cluster-monitoring-refresh",
    "cluster-alerts",
]


def run_preflight(
    environment: str,
    backup_enabled: str = "true",
    build_skip: str = "",
    existing_data: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(GATE), "preflight", environment, backup_enabled, build_skip, existing_data],
        capture_output=True,
        text=True,
        timeout=30,
    )


class GateBuildCase(unittest.TestCase):
    """Podstawiony `make` zapisuje wywolane cele; MAKE_FAIL_ON wymusza porazke celu."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.td_path = Path(self.td.name)
        self.log_file = self.td_path / "make.log"
        self.fake_make = self.td_path / "fake_make"
        self.fake_make.write_text(
            "#!/usr/bin/env bash\n"
            'echo "$*" >> "$MAKE_LOG"\n'
            '[ "$*" != "${MAKE_FAIL_ON:-}" ]\n',
            encoding="utf-8",
        )
        self.fake_make.chmod(0o755)

    def run_steps(self, environment: str, build_skip: str = "", fail_on: str = ""):
        env = dict(os.environ)
        env["MAKE"] = str(self.fake_make)
        env["MAKE_LOG"] = str(self.log_file)
        env["MAKE_FAIL_ON"] = fail_on
        proc = subprocess.run(
            [str(GATE), "steps", environment, build_skip],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        return proc, self.invoked()

    def invoked(self) -> list[str]:
        if not self.log_file.is_file():
            return []
        return [line.strip() for line in self.log_file.read_text(encoding="utf-8").splitlines() if line.strip()]


class GateBuildCliTest(unittest.TestCase):
    def test_script_exists_and_parses(self):
        self.assertTrue(GATE.is_file(), f"brak {GATE}")
        subprocess.run(["bash", "-n", str(GATE)], check=True, timeout=30)

    def test_unknown_subcommand_refuses(self):
        proc = subprocess.run([str(GATE), "nonsense"], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)

    def test_pre_environment_argument_shapes_are_usage_errors(self):
        # Sygnatury sprzed jawnego srodowiska nie maja przejsciowego trybu.
        legacy_preflight = subprocess.run(
            [str(GATE), "preflight", "true", "seed", ""], capture_output=True, text=True, timeout=30
        )
        legacy_steps = subprocess.run([str(GATE), "steps", "seed"], capture_output=True, text=True, timeout=30)
        self.assertEqual(legacy_preflight.returncode, 2, legacy_preflight.stderr)
        self.assertEqual(legacy_steps.returncode, 2, legacy_steps.stderr)

    def test_preflight_refuses_unknown_or_empty_environment(self):
        for environment in ("", "prod", "Production", "example"):
            with self.subTest(environment=environment):
                proc = run_preflight(environment)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn("nieznane srodowisko", proc.stderr)


class GateBuildEnvironmentStepsTest(GateBuildCase):
    def test_steps_refuse_unknown_or_empty_environment_before_any_target(self):
        for environment in ("", "prod", "Production"):
            with self.subTest(environment=environment):
                proc, invoked = self.run_steps(environment)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(invoked, [])


class GateBuildLabLikePreflightTest(unittest.TestCase):
    def test_seed_backup_skipped_together_is_legal(self):
        for environment in LAB_LIKE:
            with self.subTest(environment=environment):
                proc = run_preflight(environment, build_skip="seed backup")
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_seed_skipped_without_backup_refuses_without_existing_data(self):
        for environment in LAB_LIKE:
            with self.subTest(environment=environment):
                proc = run_preflight(environment, build_skip="seed")
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("EXISTING_DATA=yes", proc.stderr)

    def test_seed_skipped_with_declared_existing_data_is_legal(self):
        for environment in LAB_LIKE:
            with self.subTest(environment=environment):
                proc = run_preflight(environment, build_skip="seed", existing_data="yes")
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_backup_disabled_disarms_the_coupling(self):
        # Na klastrze bez backupu drill nie istnieje, wiec EXISTING_DATA jest
        # pytaniem o dane dla przebiegu, ktory sie nie odbędzie.
        for environment in LAB_LIKE:
            with self.subTest(environment=environment):
                proc = run_preflight(environment, backup_enabled="false", build_skip="seed")
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_no_skips_is_legal(self):
        for environment in LAB_LIKE:
            with self.subTest(environment=environment):
                proc = run_preflight(environment)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_disabled_declarations_and_required_step_skips_stay_legal_outside_production(self):
        # To produkcja, nie lab/staging, odbiera prawo do wylaczenia tych krokow.
        for environment in LAB_LIKE:
            with self.subTest(environment=environment):
                proc = run_preflight(
                    environment,
                    backup_enabled="false",
                    build_skip="app-host alerts",
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)


class GateBuildLabLikeStepsTest(GateBuildCase):
    def test_default_execution_order_runs_app_host_before_backup_and_drill_before_refresh(self):
        for environment in LAB_LIKE:
            with self.subTest(environment=environment):
                self.log_file.unlink(missing_ok=True)
                proc, invoked = self.run_steps(environment)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(invoked, LAB_FULL_PLAN)

    def test_build_skip_app_host_excludes_app_host_only(self):
        proc, invoked = self.run_steps("laboratory", "app-host")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(invoked, [step for step in LAB_FULL_PLAN if step != "cluster-app-host"])

    def test_build_skip_backup_excludes_all_backup_targets(self):
        proc, invoked = self.run_steps("staging", "backup")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(invoked, ["lab-seed-smoke", "cluster-app-host", "cluster-alerts"])

    def test_build_skip_all_skips_everything(self):
        proc, invoked = self.run_steps("laboratory", "seed app-host backup alerts")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(invoked, [])

    def test_failed_backup_stops_before_drill_refresh_and_alerts(self):
        proc, invoked = self.run_steps("laboratory", fail_on="cluster-backup")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(
            invoked,
            ["lab-seed-smoke", "cluster-app-host", "cluster-backup-configure", "cluster-backup"],
        )


class GateBuildProductionPreflightTest(unittest.TestCase):
    def test_full_declaration_without_skips_is_legal(self):
        proc = run_preflight("production")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_required_steps_cannot_be_skipped(self):
        for skip in ("app-host", "backup", "alerts", "seed backup", "alerts app-host"):
            with self.subTest(build_skip=skip):
                proc = run_preflight("production", build_skip=skip)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                for token in skip.split():
                    if token != "seed":
                        self.assertIn(f"BUILD_SKIP={token}", proc.stderr)

    def test_every_violation_is_reported_before_refusing(self):
        proc = run_preflight("production", build_skip="backup alerts")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("BUILD_SKIP=backup", proc.stderr)
        self.assertIn("BUILD_SKIP=alerts", proc.stderr)

    def test_unknown_skip_token_is_refused_not_ignored(self):
        proc = run_preflight("production", build_skip="backups")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("'backups'", proc.stderr)

    def test_skip_tokens_split_on_any_whitespace(self):
        # Tabulator/nowa linia nie moga byc obejsciem odmowy.
        for separator in ("\t", "\n", "  "):
            with self.subTest(separator=repr(separator)):
                proc = run_preflight("production", build_skip=f"seed{separator}alerts")
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn("BUILD_SKIP=alerts", proc.stderr)

    def test_skipping_seed_alone_is_legal_because_production_never_seeds(self):
        # W lab/staging to samo BUILD_SKIP=seed wymaga EXISTING_DATA=yes; produkcja
        # nie sieje, wiec sprzezenie seed->backup jej nie dotyczy.
        proc = run_preflight("production", build_skip="seed", existing_data="")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_disabled_backup_declaration_is_refused(self):
        for backup in ("false", ""):
            with self.subTest(backup=backup):
                proc = run_preflight("production", backup_enabled=backup)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn("backup.enabled=true", proc.stderr)

    def test_disabled_backup_and_unsafe_skip_are_both_reported(self):
        proc = run_preflight("production", backup_enabled="false", build_skip="alerts")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("backup.enabled=true", proc.stderr)
        self.assertIn("BUILD_SKIP=alerts", proc.stderr)


class GateBuildProductionStepsTest(GateBuildCase):
    def test_runs_required_steps_without_seed_or_restore_drill(self):
        proc, invoked = self.run_steps("production")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(invoked, PRODUCTION_FULL_PLAN)

    def test_skipping_seed_changes_nothing(self):
        proc, invoked = self.run_steps("production", "seed")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(invoked, PRODUCTION_FULL_PLAN)

    def test_unsafe_skip_is_refused_before_the_first_target(self):
        for skip in ("app-host", "backup", "alerts", "seed backup", "backups", "seed\talerts"):
            with self.subTest(build_skip=skip):
                self.log_file.unlink(missing_ok=True)
                proc, invoked = self.run_steps("production", skip)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(invoked, [], "odmowa musi zapasc przed jakimkolwiek celem make")

    def test_failed_backup_stops_before_refresh_and_alerts(self):
        proc, invoked = self.run_steps("production", fail_on="cluster-backup")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(invoked, ["cluster-app-host", "cluster-backup-configure", "cluster-backup"])


if __name__ == "__main__":
    unittest.main()
