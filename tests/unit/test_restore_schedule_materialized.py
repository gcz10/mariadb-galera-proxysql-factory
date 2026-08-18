#!/usr/bin/env python3
"""Kazde pole harmonogramu w schemacie MUSI miec skutek w kodzie.

POWSTALO PO REALNEJ AWARII KONTRAKTU (n13, 2026-08-18).
`backup.restore_test_schedule` bylo polem-widmem:
  * WYMAGANE w clusters/schema/cluster.schema.json,
  * cytowane w tresci alertu ISC-47 "Restore drill stale (no successful drill
    in 8 days)" w playbooks/f15_alerts.yml,
  * raportowane metryka `isa_restore_test_monitoring_enabled=1`,
  * i NIEOBECNE w jakimkolwiek schedulerze.

Na zywym n13 potwierdzono: `/etc/cron.d/galera-backup-*` istnial (bo
`full_backup_schedule` ma szablon `cron.j2`), a restore drill nie mial ani wpisu
w cronie, ani timera systemd. Drill uruchamial sie tylko recznie, wiec alert
krytyczny zapalilby sie po 8 dniach w kazdym realnym wdrozeniu.

Ten test jest falsyfikowalny: usuniecie szablonu `restore-cron.j2` albo zadania
instalujacego go w roli natychmiast go wywala.
"""
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCHEMA = REPO / "clusters" / "schema" / "cluster.schema.json"
ROLE_TASKS = REPO / "roles" / "galera_backup" / "tasks" / "main.yml"
RESTORE_CRON_TPL = REPO / "roles" / "galera_backup" / "templates" / "restore-cron.j2"
BACKUP_CRON_TPL = REPO / "roles" / "galera_backup" / "templates" / "cron.j2"
RESTORE_PLAYBOOK = REPO / "playbooks" / "f10_restore.yml"
ALERTS = REPO / "playbooks" / "f15_alerts.yml"

# Pola harmonogramu ze schematu -> szablon, ktory nadaje im skutek.
SCHEDULE_FIELDS = {
    "full_backup_schedule": BACKUP_CRON_TPL,
    "restore_test_schedule": RESTORE_CRON_TPL,
}


class TestRestoreScheduleMaterialized(unittest.TestCase):
    def test_schedule_fields_have_a_scheduler_template(self):
        """Pole harmonogramu bez szablonu to obietnica bez pokrycia."""
        for field, template in SCHEDULE_FIELDS.items():
            with self.subTest(field=field):
                self.assertIn(
                    field,
                    SCHEMA.read_text(encoding="utf-8"),
                    f"{field} zniknelo ze schematu — zaktualizuj ten test razem z kontraktem",
                )
                self.assertTrue(
                    template.exists(),
                    f"{field} nie ma szablonu schedulera ({template.name}) — pole-widmo",
                )
                self.assertIn(
                    "{{ backup." + field + " }}",
                    template.read_text(encoding="utf-8"),
                    f"{template.name} nie wstawia realnie wartosci backup.{field}",
                )

    def test_role_installs_restore_cron(self):
        """Sam szablon nie wystarczy — rola musi go gdzies wdrazac."""
        tasks = ROLE_TASKS.read_text(encoding="utf-8")
        self.assertIn(
            "src: restore-cron.j2",
            tasks,
            "rola galera_backup nie wdraza restore-cron.j2",
        )
        self.assertIn(
            "/etc/cron.d/galera-restore-{{ cluster.name }}",
            tasks,
            "brak docelowej sciezki cron.d dla restore drill",
        )
        # Harmonogram wylaczony ('disabled'/pusty) nie moze tworzyc wpisu.
        self.assertIn(
            "['', 'disabled']",
            tasks,
            "brak bramki na wylaczony harmonogram restore",
        )

    def test_alert_metric_is_refreshed_by_the_drill(self):
        """Alert ISC-47 nie moze mierzyc 'kiedy ostatnio puszczono Ansible'."""
        alerts = ALERTS.read_text(encoding="utf-8")
        self.assertIn(
            "isa_restore_test_last_success_unixtime",
            alerts,
            "alert ISC-47 nie odwoluje sie juz do metryki swiezosci drillu",
        )
        drill = RESTORE_PLAYBOOK.read_text(encoding="utf-8")
        self.assertRegex(
            drill,
            r"import_playbook:\s*f11_freshness\.yml",
            "f10_restore.yml nie odswieza metryki swiezosci po drillu — "
            "sukces drillu nie dotrze do alertu ISC-47",
        )

    def test_restore_cron_runs_the_runner_with_confirm(self):
        """Drill kasuje datadir — bez --confirm cron cicho by nie robil nic."""
        tpl = RESTORE_CRON_TPL.read_text(encoding="utf-8")
        self.assertRegex(
            tpl,
            r"/opt/galera-backup/galera-backup\s+restore\s+\{\{ cluster\.name \}\}\s+--confirm",
            "wpis cron nie wywoluje runnera restore z --confirm",
        )
        self.assertIn(
            "systemd-cat",
            tpl,
            "brak trwalego zrzutu logow crona (systemd-cat) — diagnostyka po nocy przepada",
        )
        self.assertRegex(
            tpl,
            r"CRON_TZ=\{\{ backup\.scheduler\.timezone",
            "wpis cron nie ustawia strefy czasowej ze schematu",
        )


if __name__ == "__main__":
    unittest.main()
