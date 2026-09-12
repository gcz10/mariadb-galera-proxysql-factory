#!/usr/bin/env python3
"""Zakres kasowania mysql_servers_ssl_params przy derejestracji: DOSLOWNY katalog CA.

POWSTAL PO AUDYCIE 2026-09-12. Zadanie "Wyczysc wpisy klastra w tabelach
ProxySQL" w `cluster_deregister.yml` kasowalo wiersze CA predykatem
`ssl_ca LIKE '%/<cluster.name>/%'`. ProxySQL admin to SQLite, gdzie `_` jest
wildcardem pojedynczego znaku, a `%` dowolnego ciagu
(https://www.sqlite.org/lang_expr.html). Nazwa najemcy jest dowolnym tekstem,
wiec `tenant_a` pasowala takze do sciezki `/etc/mysql/tls/tenant-a/ca.pem`:
derejestracja jednego klastra kasowala parametry TLS backendu sasiada, ktory
dzialal dalej na wspolnej parze ProxySQL i po cichu tracil zaufanie do CA.

Predykat jest teraz porownaniem DOSLOWNEGO prefiksu katalogu
(`substr(ssl_ca, 1, length('<dir>/')) = '<dir>/'`), gdzie `_`, `%` i `\\`
nie maja zadnej semantyki wzorca. Test renderuje blok `stdin` WPROST Z
PLAYBOOKA (nie z kopii), wykonuje z niego polecenia dotykajace
`mysql_servers_ssl_params` na tabeli w sqlite3 in-memory i sprawdza, ktore
wiersze przezyly.
"""

import sqlite3
import unittest
from pathlib import Path

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "playbooks" / "cluster_deregister.yml"
TASK_NAME = "Wyczysc wpisy klastra w tabelach ProxySQL (scisle zakresowane)"
TLS_DIR = "/etc/mysql/tls"
OWN_BACKENDS = ["10.10.0.11"]
SIBLING_NODE = "10.10.0.99"


def _load_stdin_block():
    """Zwraca surowy szablon bloku `stdin` zadania czyszczacego ProxySQL."""
    doc = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    play = next(
        item
        for item in doc
        if item.get("hosts") == "proxysql" and "F7" in item.get("name", "")
    )
    task = next(t for t in play["tasks"] if t.get("name") == TASK_NAME)
    return task["ansible.builtin.command"]["stdin"]


def _render_block(name, backends=None):
    """Renderuje blok `stdin` dokladnie tak, jak zrobilby to Ansible."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return env.from_string(_load_stdin_block()).render(
        cluster={"name": name},
        galera_writer_hg=10,
        galera_backup_hg=20,
        galera_reader_hg=30,
        galera_offline_hg=40,
        galera_app_user="app_user",
        galera_backends=OWN_BACKENDS if backends is None else backends,
    )


def _ssl_params_statements(sql):
    """Polecenia z bloku dotykajace wylacznie mysql_servers_ssl_params."""
    statements = []
    for raw in sql.split(";"):
        statement = " ".join(raw.split())
        if statement.upper().startswith("DELETE FROM MYSQL_SERVERS_SSL_PARAMS"):
            statements.append(statement + ";")
    return statements


def _ca_dir_statements(sql):
    """Te sposrod nich, ktore zakreslaja wiersze po katalogu CA (nie po hostname)."""
    return [s for s in _ssl_params_statements(sql) if "substr(" in s.lower()]


class _ProxySqlSslParamsFixture:
    """Minimalna tabela mysql_servers_ssl_params w sqlite3 (ProxySQL admin = SQLite)."""

    def __init__(self, rows):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE mysql_servers_ssl_params (hostname TEXT, ssl_ca TEXT)"
        )
        self.conn.executemany(
            "INSERT INTO mysql_servers_ssl_params (hostname, ssl_ca) VALUES (?, ?)",
            rows,
        )
        self.conn.commit()

    def run(self, sql):
        for statement in _ssl_params_statements(sql):
            self.conn.execute(statement)
        self.conn.commit()

    def hostnames(self):
        rows = self.conn.execute(
            "SELECT hostname FROM mysql_servers_ssl_params ORDER BY hostname"
        ).fetchall()
        return [row[0] for row in rows]

    def close(self):
        self.conn.close()


def _rows(name, sibling_dir):
    """Wiersze: dwa wlasne, wiersz sasiada (inny katalog) i obcy najemca."""
    return [
        (OWN_BACKENDS[0], TLS_DIR + "/" + name + "/ca-1111.pem"),
        ("10.10.0.12", TLS_DIR + "/" + name + "/ca-2222.pem"),
        (SIBLING_NODE, TLS_DIR + "/" + sibling_dir + "/ca-3333.pem"),
        ("10.10.0.98", TLS_DIR + "/other/ca-4444.pem"),
    ]


class DeregisterSslCaScopeTests(unittest.TestCase):
    """Kasowanie po katalogu CA musi byc doslowne, mimo `_` i `%` w nazwie najemcy."""

    def _deregister(self, name, rows):
        fixture = _ProxySqlSslParamsFixture(rows)
        try:
            fixture.run(_render_block(name))
            return fixture.hostnames()
        finally:
            fixture.close()

    def test_underscore_name_does_not_touch_dotted_sibling(self):
        """`tenant_a` nie moze skasowac wierszy sasiada `tenant-a` (defekt z audytu)."""
        # Wiersz sasiada lezy w katalogu `tenant-a` — dokladnie ta para nazw
        # rozstrzyga o defekcie, bo `_` w LIKE pasuje do `-`.
        remaining = self._deregister("tenant_a", _rows("tenant_a", "tenant-a"))
        self.assertNotIn(OWN_BACKENDS[0], remaining)
        self.assertNotIn("10.10.0.12", remaining)
        self.assertIn(
            SIBLING_NODE,
            remaining,
            "sasiad `tenant-a` ma przezyc derejestracje `tenant_a`",
        )
        self.assertIn("10.10.0.98", remaining)

    def test_plain_name_still_deletes_own_rows(self):
        """Nazwa bez wildcardow nadal kasuje wlasne wiersze (brak false negative)."""
        remaining = self._deregister("tenant-a", _rows("tenant-a", "other-neighbor"))
        self.assertNotIn(OWN_BACKENDS[0], remaining)
        self.assertNotIn("10.10.0.12", remaining)
        self.assertIn(SIBLING_NODE, remaining)
        self.assertIn("10.10.0.98", remaining)

    def test_percent_in_name_is_literal(self):
        """`%` w nazwie najemcy jest znakiem doslownym, nie wildcardem."""
        rows = [
            ("10.10.0.12", TLS_DIR + "/tenant%a/ca-1111.pem"),
            (SIBLING_NODE, TLS_DIR + "/tenanta/ca-2222.pem"),
            ("10.10.0.98", TLS_DIR + "/tenantXa/ca-3333.pem"),
        ]
        remaining = self._deregister("tenant%a", rows)
        self.assertNotIn("10.10.0.12", remaining)
        self.assertEqual(remaining, ["10.10.0.98", SIBLING_NODE])

    def test_own_rows_deleted_from_ca_directory_alone(self):
        """Sam predykat katalogu CA (bez trafienia w hostname) kasuje wlasne smieci."""
        rows = [
            ("10.10.0.12", TLS_DIR + "/tenant_a/ca-2222.pem"),
            (SIBLING_NODE, TLS_DIR + "/tenant-a/ca-3333.pem"),
        ]
        # Bez backendow w inwentarzu dziala WYLACZNIE sciezka po katalogu CA,
        # wiec test pinuje sam predykat katalogu, a nie liste hostname.
        sql = _render_block("tenant_a", backends=[])
        self.assertEqual(len(_ssl_params_statements(sql)), 1)
        self.assertEqual(
            len(_ca_dir_statements(sql)),
            1,
            "playbook musi emitowac dokladnie jedno kasowanie po katalogu CA",
        )
        fixture = _ProxySqlSslParamsFixture(rows)
        try:
            fixture.run(sql)
            self.assertEqual(fixture.hostnames(), [SIBLING_NODE])
        finally:
            fixture.close()

    def test_apostrophe_in_name_is_escaped(self):
        """Apostrof w nazwie nie rozjezdza literalu SQL (escaping jak dla hasel)."""
        rows = [
            ("10.10.0.12", TLS_DIR + "/o'brien/ca-1111.pem"),
            (SIBLING_NODE, TLS_DIR + "/obrien/ca-2222.pem"),
        ]
        remaining = self._deregister("o'brien", rows)
        self.assertNotIn("10.10.0.12", remaining)
        self.assertEqual(remaining, [SIBLING_NODE])


if __name__ == "__main__":
    unittest.main()
