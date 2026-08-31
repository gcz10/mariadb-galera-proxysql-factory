"""Straznik writera w backupie czyta ProxySQL kontem read-only.

DLACZEGO. `admin-admin_credentials` to pula READ-WRITE calego ProxySQL, a jedna
para ProxySQL obsluguje CALA flote. Zapisywanie tego hasla w
`/etc/galera-backup/secrets.env` na wezle Galery oznaczalo, ze kompromitacja
JEDNEGO wezla bazy daje pelne prawa zapisu do wspolnego proxy wszystkich
najemcow — po to, zeby odczytac jedna informacje: kto jest teraz writerem.

Zmierzone na zywym ProxySQL 3.0 (green, 2026-08-24), konto z
`admin-stats_credentials`:

    SELECT srv_host FROM stats_mysql_connection_pool WHERE hostgroup=890  -> rc=0, 192.168.1.30
    SELECT hostname FROM runtime_mysql_servers      WHERE hostgroup_id=890 -> ERROR 1045: no such table
    UPDATE global_variables ...                                            -> ERROR 1045: attempt to write a readonly database

Czyli tozsamosc writera da sie ustalic bez jakichkolwiek praw zapisu, a proba
eskalacji tym samym poswiadczeniem jest odrzucana przez samo ProxySQL.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SECRETS_TEMPLATE = REPO / "roles" / "galera_backup" / "templates" / "secrets.env.j2"
PIPELINE = REPO / "roles" / "galera_backup" / "files" / "galera_backup" / "pipeline.py"
GUARDS = REPO / "roles" / "galera_backup" / "files" / "galera_backup" / "guards.py"
CONFIG = REPO / "roles" / "galera_backup" / "files" / "galera_backup" / "config.py"
PLATFORM_PROXYSQL = REPO / "playbooks" / "platform_proxysql.yml"
BACKUP_PLAY = REPO / "playbooks" / "f10_backup.yml"


class SchedulerHoldsNoWriteCredentialTests(unittest.TestCase):
    def test_secrets_template_never_ships_rw_admin_to_galera_node(self):
        body = SECRETS_TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn(
            "GALERA_BACKUP_PROXYSQL_ADMIN_PASSWORD",
            body,
            "haslo puli read-write ProxySQL nie moze ladowac w secrets.env na wezle Galery",
        )
        self.assertIn(
            "GALERA_BACKUP_PROXYSQL_STATS_PASSWORD",
            body,
            "straznik writera dostaje wylacznie poswiadczenie read-only",
        )

    def test_backup_play_requires_stats_secret_not_admin_secret(self):
        body = BACKUP_PLAY.read_text(encoding="utf-8")
        self.assertNotIn(
            "'PROXYSQL_ADMIN_PASSWORD'",
            body,
            "f10_backup nie moze pobierac hasla admina RW",
        )
        self.assertIn("PROXYSQL_STATS_PASSWORD", body)

    def test_config_requires_read_only_credentials(self):
        body = CONFIG.read_text(encoding="utf-8")
        self.assertIn("GALERA_BACKUP_PROXYSQL_STATS_USER", body)
        self.assertIn("GALERA_BACKUP_PROXYSQL_STATS_PASSWORD", body)
        self.assertNotIn("GALERA_BACKUP_PROXYSQL_ADMIN_PASSWORD", body)


class WriterGuardUsesStatisticsSchemaTests(unittest.TestCase):
    """Schemat konfiguracyjny jest niedostepny dla konta stats — zmierzone."""

    def setUp(self):
        # Straznik writera mieszka teraz w guards.py (wydzielony z pipeline.py,
        # pipeline jest facade z re-eksportem). Cialo jest 1:1 identyczne.
        self.body = GUARDS.read_text(encoding="utf-8")
        guard = re.search(
            r"def assert_scheduler_is_not_writer\(.*?\n(?=\ndef |\nclass )",
            self.body,
            re.S,
        )
        self.assertIsNotNone(guard, "nie znaleziono straznika writera")
        # Komentarz tlumaczacy, DLACZEGO nie uzywamy schematu konfiguracyjnego,
        # sam musi moc go wymienic. Kontrakt dotyczy kodu wykonywanego.
        self.guard = "\n".join(
            line for line in guard.group(0).splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_guard_reads_statistics_table(self):
        self.assertIn(
            "stats_mysql_connection_pool",
            self.guard,
            "tozsamosc writera pochodzi z tabeli statystycznej, czytelnej dla konta read-only",
        )

    def test_guard_does_not_touch_configuration_schema(self):
        self.assertNotIn(
            "runtime_mysql_servers",
            self.guard,
            "konto z admin-stats_credentials nie widzi runtime_mysql_servers "
            "(zmierzone: ERROR 1045 no such table) — zapytanie o nia zawsze padnie",
        )

    def test_guard_filters_on_statistics_column_names(self):
        # stats_mysql_connection_pool ma `hostgroup`/`srv_host`, a nie
        # `hostgroup_id`/`hostname` znane z runtime_mysql_servers.
        self.assertIn("srv_host", self.guard)
        self.assertRegex(self.guard, r"hostgroup=\{?")
        self.assertNotIn("hostgroup_id", self.guard)


class PlatformRegistersReadOnlyAccountTests(unittest.TestCase):
    def test_platform_owns_stats_credentials(self):
        body = PLATFORM_PROXYSQL.read_text(encoding="utf-8")
        self.assertIn(
            "admin-stats_credentials",
            body,
            "konto read-only dla strażnika writera rejestruje platforma, nie najemca",
        )
        self.assertIn("proxysql_stats_user", body)

    def test_tenant_backup_play_does_not_register_credentials(self):
        body = BACKUP_PLAY.read_text(encoding="utf-8")
        self.assertNotIn(
            "UPDATE global_variables",
            body,
            "najemca konsumuje poswiadczenie, nie zapisuje go w globalnym ProxySQL",
        )


if __name__ == "__main__":
    unittest.main()
