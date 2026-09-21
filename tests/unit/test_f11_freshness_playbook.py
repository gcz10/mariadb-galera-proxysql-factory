#!/usr/bin/env python3
"""F11 publikuje pomiar drillu TYLKO z potwierdzonego stanu — nigdy zmyslone zero.

POWSTALO Z KONTRAKTU: `galera_restore_last_duration_seconds` i
`galera_restore_last_size_bytes` opisuja ostatni UDANY drill odtworzenia.
Trzy pułapki, których ten test broni:

  1. Stan sprzed wprowadzenia pomiarów (albo drill zapisany starszym runnerem)
     nie ma `artifact.duration_seconds`/`size_bytes`. Metryki pomiaru MUSZĄ
     wtedy zniknąć, a nie pokazać zera: zero jest pomiarem ("drill trwał 0 s")
     i wygląda jak natychmiastowe, puste odtworzenie.
  2. Ten sam plik textfile (`galera_restore_drill-<cluster>.prom`) zapisuje
     runner backupu na wybranym donorze. Wpisanie tam zera przez sam play,
     gdy stan nie niesie świeżości, skasowałoby świeższy dowód donora — na
     klastrze, którego `backup.scheduler.host` wygrywa elekcję, czyli na
     tym samym węźle. Dlatego brak potwierdzonego drillu = brak zapisu.
  3. Uszkodzony pomiar (ujemny, tekst, NaN) nie może trafić do collectora:
     zatrułby serię bez błędu składni. Przebieg ma się wtedy wywrócić.
  4. `state.json` jest WSPÓLNY dla backupu i drillu (oba piszą `last_success`).
     Backup biegnie codziennie, drill raz w tygodniu — opublikowanie znacznika
     backupu jako świeżości drillu zamaskowałoby regułę "restore drill stale"
     na zawsze, bo seria nigdy nie byłaby stara.
  5. Klaster bez grupy `restore` (backup wyłączony) nie ma stanu drillu
     i nie może przez to wywrócić playa.

Sonda uruchamia PRAWDZIWY `playbooks/f11_freshness.yml` na inwentarzu z
`connection: local` i stanem zapisanym w katalogu tymczasowym. Werdykt opiera
się na WYEKSPONOWANYM TEKŚCIE plików `.prom`, a nie na treści playbooka.
Jedyne adaptacje lokalne: ścieżki systemowe collectora i katalogu stanu na
katalog tymczasowy oraz właściciel plików z `root` na użytkownika testu
(nieuprzywilejowany przebieg nie może zrobić `chown`).
"""

import grp
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "playbooks" / "f11_freshness.yml"
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")

sys.path.insert(0, str(REPO / "roles" / "galera_backup" / "files"))

from galera_backup.pipeline import drill_metric_path  # noqa: E402

TEXTFILE_DIR = "/var/lib/node_exporter/textfile_collector"
STATE_ROOT = "/opt/galera-backup/clusters"

CLUSTER_NAME = "review-f11"
PMM_CLUSTER_NAME = "review-f11-pmm"
BACKEND = "s3"
DRILL_UNIXTIME = 1787019064

INVENTORY = {
    "all": {
        "vars": {
            "ansible_connection": "local",
            "ansible_become": False,
            "ansible_python_interpreter": sys.executable,
        },
        "children": {
            "galera": {"hosts": {"g1": None, "g2": None}},
            "restore": {"hosts": {"r1": None}},
        },
    }
}

CLUSTER_VARS = {
    "cluster": {"name": CLUSTER_NAME, "environment": "laboratory"},
    "tls": {"mode": "disabled"},
    "backup": {
        "enabled": True,
        "destination": BACKEND,
        "restore_test_schedule": "0 4 * * 0",
    },
    "monitoring": {"pmm": {"cluster_name": PMM_CLUSTER_NAME}},
}

LABELS = (
    f'cluster="{PMM_CLUSTER_NAME}",'
    f'logical_cluster="{CLUSTER_NAME}",backend="{BACKEND}"'
)


def _series(text):
    """Linie metryk (bez komentarzy i pustych) — to, co realnie czyta collector."""
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def _localize(node):
    """Jedyne adaptacje fixture: ścieżki systemowe i właściciel plików."""
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    if isinstance(node, list):
        return [_localize(item) for item in node]
    if isinstance(node, dict):
        localized = {}
        for key, value in node.items():
            if key in ("owner", "group") and value == "root":
                localized[key] = user if key == "owner" else group
            else:
                localized[key] = _localize(value)
        return localized
    if isinstance(node, str):
        return node.replace(TEXTFILE_DIR, "{TEXTFILE}").replace(STATE_ROOT, "{STATE}")
    return node


def _restore_placeholders(node, textfile_dir, state_root):
    if isinstance(node, list):
        return [_restore_placeholders(item, textfile_dir, state_root) for item in node]
    if isinstance(node, dict):
        return {
            key: _restore_placeholders(value, textfile_dir, state_root)
            for key, value in node.items()
        }
    if isinstance(node, str):
        return node.replace("{TEXTFILE}", str(textfile_dir)).replace("{STATE}", str(state_root))
    return node


class F11FreshnessMetricsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="f11-freshness-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.textfile_dir = self.root / "textfile"
        self.state_root = self.root / "state"
        (self.state_root / CLUSTER_NAME).mkdir(parents=True)
        (self.root / "inventory.yml").write_text(
            yaml.safe_dump(INVENTORY, sort_keys=False), encoding="utf-8"
        )
        (self.root / "cluster.yml").write_text(
            yaml.safe_dump(CLUSTER_VARS, sort_keys=False), encoding="utf-8"
        )
        (self.root / "ansible.cfg").write_text(
            "[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n",
            encoding="utf-8",
        )
        self.playbook = self.root / "f11_fixture.yml"
        plays = _restore_placeholders(
            _localize(yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))),
            self.textfile_dir,
            self.state_root,
        )
        self.playbook.write_text(yaml.safe_dump(plays, sort_keys=False), encoding="utf-8")

    # --- stan wejściowy -----------------------------------------------------
    def write_state(self, artifact, unixtime=DRILL_UNIXTIME, command="restore"):
        state = {
            "format_version": 1,
            "cluster": CLUSTER_NAME,
            "last_run": {"command": command, "status": "success", "unixtime": unixtime},
            "last_success": {"command": command, "unixtime": unixtime, "artifact": artifact},
            "last_failure": None,
        }
        (self.state_root / CLUSTER_NAME / "state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )

    # --- przebieg -----------------------------------------------------------
    def run_playbook(self):
        env = dict(os.environ)
        env.update(
            {
                "ANSIBLE_NOCOLOR": "1",
                "ANSIBLE_LOCAL_TEMP": str(self.root / "ansible-tmp"),
                "ANSIBLE_CONFIG": str(self.root / "ansible.cfg"),
                "HOME": str(self.root),
            }
        )
        return subprocess.run(
            [
                ANSIBLE_PLAYBOOK,
                str(self.playbook),
                "-i",
                str(self.root / "inventory.yml"),
                "-e",
                f"@{self.root / 'cluster.yml'}",
            ],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )

    def bridge_file(self):
        return self.textfile_dir / f"galera_restore_drill-{CLUSTER_NAME}.prom"

    def state_metrics(self):
        return (self.textfile_dir / "isa_monitoring_state.prom").read_text(encoding="utf-8")

    # --- przypadki ----------------------------------------------------------
    @unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostępny w PATH")
    def test_measurement_from_successful_state_is_published_once(self):
        self.write_state(
            {
                "backup_name": "galera-review-f11-1",
                "databases_verified": 1,
                "tables_verified": 5,
                "rows_verified": 42,
                "duration_seconds": 2.5,
                "size_bytes": 1048576,
            }
        )
        result = self.run_playbook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        bridge = self.bridge_file()
        self.assertTrue(bridge.exists(), result.stdout)
        lines = _series(bridge.read_text(encoding="utf-8"))
        self.assertIn(
            f"galera_restore_last_success_unixtime{{{LABELS}}} {DRILL_UNIXTIME}", lines
        )
        self.assertIn(f"galera_restore_last_duration_seconds{{{LABELS}}} 2.500", lines)
        self.assertIn(f"galera_restore_last_size_bytes{{{LABELS}}} 1048576", lines)

        # Nazwa pliku jest WSPÓLNA z mostem runnera — inaczej ten sam host
        # serwowałby dwie serie tej samej metryki (odrzucony scrape).
        self.assertEqual(
            bridge.name,
            drill_metric_path(self.textfile_dir, CLUSTER_NAME).name,
        )
        # Każda rodzina dokładnie raz i tylko w pliku mostka.
        self.assertEqual(
            sorted(path.name for path in self.textfile_dir.glob("*.prom")),
            [
                f"galera_restore_drill-{CLUSTER_NAME}.prom",
                "isa_monitoring_config.prom",
                "isa_monitoring_state.prom",
            ],
        )
        state_text = self.state_metrics()
        self.assertNotIn("galera_restore_last", state_text)
        state_lines = _series(state_text)
        self.assertIn(
            f'isa_restore_test_last_success_unixtime{{cluster="{PMM_CLUSTER_NAME}"}} {DRILL_UNIXTIME}',
            state_lines,
        )
        self.assertIn(
            f'isa_tls_cert_expiry_unixtime{{cluster="{PMM_CLUSTER_NAME}"}} 0',
            state_lines,
        )
        # Odczyt stanu drillu należy do hosta restore, nie do galera[0]:
        # delegacja jest widoczna w wyniku zadania jako `[g1 -> r1]`.
        self.assertIn("[g1 -> r1]", result.stdout)

    @unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostępny w PATH")
    def test_legacy_state_keeps_freshness_without_invented_measurement(self):
        # Ksztalty stanu zapisanego PRZED pomiarami: artefakt bez tych pol,
        # artefakt `null` albo brak klucza. Kazdy z nich MUSI dac swiezosc
        # i ZERO linii pomiaru — inaczej seria klamie pomiarem, ktorego nikt
        # nie zmierzyl.
        legacy_artifacts = (
            {"backup_name": "galera-review-f11-1", "rows_verified": 42},
            None,
            # Stan zapisany przez runner SPRZED pomiarow: `artifact` bylo wowczas
            # NAPISEM (StateManager.update_success, `artifact: Optional[str]`).
            # Ten kształt lezy realnie w terenie — czytnik nie moze wolac na nim
            # `.get()` ani traktowac go jako brak drillu.
            "galera-review-f11-1",
        )
        for artifact in legacy_artifacts:
            with self.subTest(artifact=artifact):
                for path in self.textfile_dir.glob("*.prom") if self.textfile_dir.exists() else []:
                    path.unlink()
                self.write_state(artifact)
                result = self.run_playbook()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

                bridge_text = self.bridge_file().read_text(encoding="utf-8")
                lines = _series(bridge_text)
                self.assertIn(
                    f"galera_restore_last_success_unixtime{{{LABELS}}} {DRILL_UNIXTIME}",
                    lines,
                )
                self.assertNotIn("galera_restore_last_duration_seconds", bridge_text)
                self.assertNotIn("galera_restore_last_size_bytes", bridge_text)
                self.assertIn(
                    f'isa_restore_test_last_success_unixtime{{cluster="{PMM_CLUSTER_NAME}"}} {DRILL_UNIXTIME}',
                    _series(self.state_metrics()),
                )

    @unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostępny w PATH")
    def test_real_zeros_are_published_as_measurements(self):
        # Zero JEST pomiarem ("drill trwal 0 s"), a nie brakiem pomiaru — to
        # odwrotna strona reguly z przypadku legacy. Boundary, na ktorym latwo
        # zamienic `is not none` na truthiness i CICHO zgubic realny pomiar.
        self.write_state(
            {
                "backup_name": "galera-review-f11-1",
                "rows_verified": 42,
                "duration_seconds": 0.0,
                "size_bytes": 0,
            }
        )
        result = self.run_playbook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        lines = _series(self.bridge_file().read_text(encoding="utf-8"))
        self.assertIn(
            f"galera_restore_last_duration_seconds{{{LABELS}}} 0.000",
            lines,
            "realny pomiar 0 s zostal pominiety jak brak pomiaru",
        )
        self.assertIn(
            f"galera_restore_last_size_bytes{{{LABELS}}} 0",
            lines,
            "realny pomiar 0 B zostal pominiety jak brak pomiaru",
        )

    @unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostępny w PATH")
    def test_backup_success_is_not_published_as_restore_freshness(self):
        # state.json jest WSPOLNY dla backupu i drillu (oba pisza `last_success`).
        # Backup biegnie codziennie, drill raz w tygodniu — opublikowanie
        # znacznika backupu jako swiezosci drillu zamaskowaloby regule
        # "restore drill stale" na zawsze, bo seria nigdy nie bylaby stara.
        self.write_state(
            {"backup_name": "galera-review-f11-1"}, unixtime=DRILL_UNIXTIME, command="backup"
        )
        result = self.run_playbook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        self.assertIn(
            f'isa_restore_test_last_success_unixtime{{cluster="{PMM_CLUSTER_NAME}"}} 0',
            _series(self.state_metrics()),
            "swiezosc backupu trafila do metryki drillu",
        )
        self.assertFalse(
            self.bridge_file().exists(),
            "stan backupu zostal opublikowany jako pomiar drillu",
        )

    @unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostępny w PATH")
    def test_missing_state_does_not_overwrite_the_donor_bridge(self):
        # Brak stanu na hoście restore (np. host przebudowany) NIE może zamienić
        # świeżego dowodu donora na zero — ten plik ma jednego producenta.
        donor_evidence = (
            "# dowod z backendu (donor)\n"
            "# HELP galera_restore_last_success_unixtime Unix time of the last successful restore drill.\n"
            "# TYPE galera_restore_last_success_unixtime gauge\n"
            f"galera_restore_last_success_unixtime{{{LABELS}}} 1787000000\n"
        )
        self.textfile_dir.mkdir(parents=True, exist_ok=True)
        self.bridge_file().write_text(donor_evidence, encoding="utf-8")

        result = self.run_playbook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self.bridge_file().read_text(encoding="utf-8"),
            donor_evidence,
            "play nadpisał plik mostka bez potwierdzonego drillu",
        )
        self.assertIn(
            f'isa_restore_test_last_success_unixtime{{cluster="{PMM_CLUSTER_NAME}"}} 0',
            _series(self.state_metrics()),
        )

    @unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostępny w PATH")
    def test_cluster_without_restore_group_still_publishes_freshness_metrics(self):
        # Klaster z backup.enabled=false nie ma grupy `restore` — to legalne
        # (patrz komentarz przy `restore_host`). Play musi wtedy opublikować
        # świeżość 0 i NIE utworzyć pliku mostka ani nie wywrócić się.
        inventory = {
            "all": {
                "vars": {
                    "ansible_connection": "local",
                    "ansible_become": False,
                    "ansible_python_interpreter": sys.executable,
                },
                "children": {"galera": {"hosts": {"g1": None, "g2": None}}},
            }
        }
        (self.root / "inventory.yml").write_text(
            yaml.safe_dump(inventory, sort_keys=False), encoding="utf-8"
        )
        result = self.run_playbook()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            f'isa_restore_test_last_success_unixtime{{cluster="{PMM_CLUSTER_NAME}"}} 0',
            _series(self.state_metrics()),
        )
        self.assertFalse(
            self.bridge_file().exists(),
            "mostek drillu powstaje bez potwierdzonego stanu drillu",
        )

    @unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostępny w PATH")
    def test_corrupt_state_fails_closed(self):
        # Uszkodzony JSON to uszkodzony dowód, nie "brak drillu": 0 udawałoby
        # brak sukcesu i zmyślony stan, więc przebieg jest czerwony z podaną
        # ścieżką, a plik mostka nie powstaje.
        (self.state_root / CLUSTER_NAME / "state.json").write_text("{garbage", encoding="utf-8")
        result = self.run_playbook()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("state.json", result.stdout + result.stderr)
        self.assertFalse(self.bridge_file().exists(), "uszkodzony stan zatril plik mostka")

    @unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostępny w PATH")
    def test_invalid_measurement_stops_publication(self):
        cases = [
            ({"duration_seconds": -1.0}, "duration_seconds"),
            ({"duration_seconds": float("nan")}, "duration_seconds"),
            ({"duration_seconds": "2.5"}, "duration_seconds"),
            ({"size_bytes": -1}, "size_bytes"),
            ({"size_bytes": "1048576"}, "size_bytes"),
            ({"size_bytes": 1048576.0}, "size_bytes"),
        ]
        for artifact, field in cases:
            with self.subTest(field=field, artifact=artifact):
                for path in self.textfile_dir.glob("*.prom") if self.textfile_dir.exists() else []:
                    path.unlink()
                self.write_state({"backup_name": "galera-review-f11-1", **artifact})
                result = self.run_playbook()
                self.assertNotEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )
                self.assertIn(field, result.stdout + result.stderr)
                self.assertFalse(
                    self.bridge_file().exists(),
                    "uszkodzony pomiar trafil do collectora",
                )


if __name__ == "__main__":
    unittest.main()
