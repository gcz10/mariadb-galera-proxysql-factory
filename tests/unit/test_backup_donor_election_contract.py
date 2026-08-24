"""Donor backupu jest WYBIERANY przy starcie, a nie przypiety na stale.

DEFEKT, KTORY TO ZAMYKA. `backup.scheduler.host` wskazywal jeden wezel, a runner
odmawial pracy, gdy ten wezel byl aktualnym writerem (`E_WRITER`, slusznie —
backup z writera lamie ISC-39). Po failoverze WLASNIE NA host schedulera kazdy
kolejny backup padal trwale: bezpiecznie, ale klaster zostawal bez kopii az do
recznej interwencji.

SKAD SIE BIERZE ZBIOR KANDYDATOW. Nie trzeba nowego zrodla prawdy ani uprawnien
SUPER — ProxySQL juz utrzymuje dokladnie ten zbior. Dokumentacja
`mysql_galera_hostgroups`:
* `backup_writer_hostgroup` — "If the cluster has multiple nodes with read_only=0
  and their count exceeds max_writers, additional nodes are placed in this
  hostgroup" (czyli zdrowe wezly zapisywalne, ktore NIE sa aktywnym writerem),
* `offline_hostgroup` — "Unhealthy nodes are moved to this hostgroup until they
  become healthy again",
* `active` — ProxySQL sam przenosi serwery miedzy hostgrupami.
(https://proxysql.com/documentation/main-runtime/mysql-tables)

Czyli ONLINE w backup hostgroup == zdrowy nie-writer. Odczyt idzie tym samym
kontem read-only, ktore wprowadzil P1-3.

KONTRAKT
1. Kandydaci pochodza z BACKUP hostgroup, nie z writer hostgroup.
2. Skonfigurowany `backup.scheduler.host` jest PREFERENCJA: wygrywa, gdy jest
   zdrowy; nie blokuje backupu, gdy zostal writerem.
3. Wybor jest deterministyczny — kazdy wezel liczy ten sam wynik z tych samych
   danych, niezaleznie od kolejnosci wierszy zwroconych przez ProxySQL.
4. Wezly spoza `galera_nodes` (cudzy najemca na wspoldzielonym ProxySQL) nie sa
   kandydatami.
5. Pusty zbior kandydatow to blad fail-closed, nie cichy backup z przypadkowego
   wezla.
"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[2]
PIPELINE_PATH = REPO / "roles" / "galera_backup" / "files" / "galera_backup" / "pipeline.py"
TEMPLATE = REPO / "roles" / "galera_backup" / "templates" / "config.json.j2"

import tests.unit.test_galera_backup_core as core  # noqa: E402  (wspolny loader modulu)

pipeline = core.pipeline


def _cfg(scheduler="192.168.1.51", nodes=None):
    return MagicMock(
        proxysql={
            "admin_host": "192.168.1.44",
            "admin_port": 6032,
            "writer_hostgroup": 10,
            "backup_hostgroup": 20,
        },
        scheduler_system_address=scheduler,
        scheduler_system_hostname="gnode1",
        galera_nodes=nodes or ["192.168.1.51", "192.168.1.52", "192.168.1.53"],
    )


SECRETS = {
    "GALERA_BACKUP_PROXYSQL_STATS_USER": "isa_stats",
    "GALERA_BACKUP_PROXYSQL_STATS_PASSWORD": "stats-secret",
}


class DonorElectionTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            hasattr(pipeline, "elect_backup_donor"),
            "runner musi umiec wybrac donora, a nie tylko sprawdzic przypiety host",
        )

    def _runner(self, rows):
        runner = MagicMock()
        runner.run.return_value = (0, "".join(f"{r}\n" for r in rows), "")
        return runner

    def test_candidates_come_from_backup_hostgroup(self):
        runner = self._runner(["192.168.1.51", "192.168.1.52"])
        pipeline.elect_backup_donor(_cfg(), SECRETS, runner)
        sql = " ".join(runner.run.call_args.args[0])
        self.assertIn("stats_mysql_connection_pool", sql)
        self.assertIn("hostgroup=20", sql.replace(" ", ""))
        self.assertIn("ONLINE", sql)

    def test_configured_host_is_preferred_when_healthy(self):
        runner = self._runner(["192.168.1.52", "192.168.1.51"])
        self.assertEqual(
            pipeline.elect_backup_donor(_cfg(), SECRETS, runner),
            "192.168.1.51",
            "zdrowy skonfigurowany scheduler musi wygrac niezaleznie od kolejnosci wierszy",
        )

    def test_failover_onto_scheduler_elects_another_healthy_node(self):
        # Scheduler zostal writerem, wiec ProxySQL nie trzyma go w backup hostgroup.
        runner = self._runner(["192.168.1.53", "192.168.1.52"])
        self.assertEqual(
            pipeline.elect_backup_donor(_cfg(), SECRETS, runner),
            "192.168.1.52",
            "po failoverze na scheduler backup ma isc z innego zdrowego wezla, nie padac",
        )

    def test_election_ignores_foreign_tenant_nodes(self):
        runner = self._runner(["10.9.9.9", "192.168.1.52"])
        self.assertEqual(
            pipeline.elect_backup_donor(_cfg(), SECRETS, runner),
            "192.168.1.52",
            "wezel spoza galera_nodes nalezy do innego najemcy wspolnego ProxySQL",
        )

    def test_no_healthy_candidate_fails_closed(self):
        runner = self._runner([])
        with self.assertRaises(pipeline.BackupError) as ctx:
            pipeline.elect_backup_donor(_cfg(), SECRETS, runner)
        self.assertEqual(ctx.exception.code, "E_PROXYSQL")

    def test_proxysql_error_fails_closed(self):
        runner = MagicMock()
        runner.run.return_value = (1, "", "connection refused")
        with self.assertRaises(pipeline.BackupError) as ctx:
            pipeline.elect_backup_donor(_cfg(), SECRETS, runner)
        self.assertEqual(ctx.exception.code, "E_PROXYSQL")

    def test_password_never_reaches_argv(self):
        runner = self._runner(["192.168.1.51"])
        pipeline.elect_backup_donor(_cfg(), SECRETS, runner)
        self.assertNotIn("stats-secret", runner.run.call_args.args[0])
        self.assertEqual(runner.run.call_args.kwargs["env"]["MYSQL_PWD"], "stats-secret")


class GuardJudgesExecutorNotPreferenceTests(unittest.TestCase):
    """Regresja zlapana na zywym green-r9 (2026-08-24).

    Gdy preferowany `backup.scheduler.host` zostal writerem, elekcja poprawnie
    wskazywala inny wezel — ale straznik writera nadal trzymal w zbiorze
    tozsamosci `scheduler_system_address`. Wybrany donor porownywal wiec adres
    CUDZEGO hosta z writerem, trafial i przerywal backup przez `E_WRITER`.
    Efekt: brak kopii dokladnie w scenariuszu, dla ktorego powstala elekcja.
    """

    def test_elected_donor_is_not_blamed_for_preferences_role(self):
        cfg = MagicMock(
            proxysql={"admin_host": "192.168.1.44", "admin_port": 6032, "writer_hostgroup": 10},
            # cluster.yml wskazuje .53, ale ten wezel jest wlasnie writerem.
            scheduler_system_address="192.168.1.53",
            scheduler_system_hostname="gnode3",
            # Backup wykonuje wybrany donor .51.
            node_system_address="192.168.1.51",
            galera_nodes=["192.168.1.51", "192.168.1.52", "192.168.1.53"],
        )
        runner = MagicMock()
        runner.run.return_value = (0, "192.168.1.53\n", "")

        pipeline.assert_scheduler_is_not_writer(
            cfg, SECRETS, runner, current_hostname="gnode1"
        )

    def test_executing_node_that_is_the_writer_is_still_rejected(self):
        cfg = MagicMock(
            proxysql={"admin_host": "192.168.1.44", "admin_port": 6032, "writer_hostgroup": 10},
            scheduler_system_address="192.168.1.53",
            scheduler_system_hostname="gnode3",
            node_system_address="192.168.1.51",
            galera_nodes=["192.168.1.51", "192.168.1.52", "192.168.1.53"],
        )
        runner = MagicMock()
        runner.run.return_value = (0, "192.168.1.51\n", "")

        with self.assertRaises(pipeline.BackupError) as ctx:
            pipeline.assert_scheduler_is_not_writer(
                cfg, SECRETS, runner, current_hostname="gnode1"
            )
        self.assertEqual(ctx.exception.code, "E_WRITER")


class RunnerConfigCarriesElectionInputsTests(unittest.TestCase):
    def test_template_renders_own_address_and_backup_hostgroup(self):
        body = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn(
            "node_system_address", body,
            "wezel musi znac WLASNY adres, zeby stwierdzic, czy to on zostal wybrany",
        )
        self.assertIn(
            "backup_hostgroup", body,
            "elekcja czyta backup hostgroup; bez niego runner nie wie, gdzie patrzec",
        )


if __name__ == "__main__":
    unittest.main()
