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

import yaml

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


def _alert_rules(alerts_path, uid):
    """Reguly alertow (dict z uid+expr) o dokladnym `uid`.

    Szukanie rekursywne po calej strukturze sprawia, ze przeniesienie listy
    `f15_rules` w inne miejsce playbooka nie oslabia testu, a yaml.safe_load
    gubi komentarze — nazwa metryki wpisana tylko w komentarzu nie zaspokoi
    asercji.
    """
    rules = []

    def _walk(node):
        if isinstance(node, dict):
            if "uid" in node and "expr" in node:
                rules.append(node)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(yaml.safe_load(alerts_path.read_text(encoding="utf-8")))
    return [r for r in rules if r.get("uid") == uid]


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
        tasks = yaml.safe_load(ROLE_TASKS.read_text(encoding="utf-8"))
        cron_jobs = [
            t for t in tasks if isinstance(t, dict)
            and t.get("ansible.builtin.template", {}).get("src") == "restore-cron.j2"
        ]
        self.assertTrue(
            cron_jobs,
            "rola galera_backup nie wdraza restore-cron.j2",
        )
        # Jeden obiekt zadania musi laczyc src, dest i bramke — trzy trafienia
        # w roznych zadaniach nie dowodza, ze wpis cron restore powstaje.
        job = cron_jobs[0]
        template = job["ansible.builtin.template"]
        self.assertEqual(
            template["src"],
            "restore-cron.j2",
            "zadanie crona restore ma nieprawidlowy szablon",
        )
        self.assertEqual(
            template["dest"],
            "/etc/cron.d/galera-restore-{{ cluster.name }}",
            "brak docelowej sciezki cron.d dla restore drill",
        )
        when = str(job.get("when", ""))
        # Harmonogram wylaczony ('disabled'/pusty) nie moze tworzyc wpisu.
        self.assertIn(
            "restore_test_schedule",
            when,
            "bramka zadania nie czyta backup.restore_test_schedule",
        )
        self.assertIn(
            "not in ['', 'disabled']",
            when,
            "brak bramki na wylaczony harmonogram restore",
        )

    def test_alert_metric_is_refreshed_by_the_drill(self):
        """Alert ISC-47 nie moze mierzyc 'kiedy ostatnio puszczono Ansible'."""
        rules = _alert_rules(
            ALERTS, "{{ f15_uid_prefix }}-restore-drill-stale"
        )
        self.assertEqual(
            len(rules),
            1,
            "regula ISC-47 (dokladny uid restore-drill-stale) zniknela lub "
            "jest zduplikowana w f15_alerts.yml",
        )
        self.assertIn(
            "isa_restore_test_last_success_unixtime",
            rules[0]["expr"],
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
