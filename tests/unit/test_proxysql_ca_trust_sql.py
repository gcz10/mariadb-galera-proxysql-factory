"""Regresja SQL i efektów bazy danych dla ProxySQL CA trust (mysql_servers_ssl_params).

Weryfikuje realne efekty zapytań SQL generowanych przez tasks/proxysql_ca_trust.yml
na minimalnym fixture schematu ProxySQL w silniku SQLite (in-memory):
- Schemat tabeli mysql_servers_ssl_params zdefiniowany wg oficjalnej dokumentacji
  ProxySQL (https://proxysql.com/documentation/main-runtime/mysql-tables)
  z kluczem głównym (hostname, port, username).
- Weryfikuje izolację najemców: aktualizacja CA klastra A nie narusza wpisów klastra B.
- Weryfikuje zgodność predykatu sprawdzającego (COUNT(*) pasujących wierszy).
"""

import sqlite3
import unittest
from pathlib import Path
import jinja2
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_FILE = REPO_ROOT / "playbooks" / "tasks" / "proxysql_ca_trust.yml"


class ProxySqlCaTrustSqlEffectsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tasks = yaml.safe_load(TASK_FILE.read_text(encoding="utf-8"))
        cls.check_sql_template = next(
            t["ansible.builtin.command"]["stdin"]
            for t in tasks
            if "SELECT COUNT(*) FROM mysql_servers_ssl_params"
            in str(t.get("ansible.builtin.command", {}).get("stdin", ""))
        )
        cls.update_sql_template = next(
            t["ansible.builtin.command"]["stdin"]
            for t in tasks
            if "INSERT INTO mysql_servers_ssl_params"
            in str(t.get("ansible.builtin.command", {}).get("stdin", ""))
        )
        cls.jinja_env = jinja2.Environment()

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        # Minimalny fixture tabeli mysql_servers_ssl_params (ProxySQL schema)
        self.cursor.execute("""
            CREATE TABLE mysql_servers_ssl_params (
                hostname VARCHAR NOT NULL,
                port INT NOT NULL DEFAULT 3306,
                username VARCHAR NOT NULL DEFAULT '',
                ssl_ca VARCHAR NOT NULL DEFAULT '',
                ssl_cert VARCHAR NOT NULL DEFAULT '',
                ssl_key VARCHAR NOT NULL DEFAULT '',
                PRIMARY KEY (hostname, port, username)
            );
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def render_sql(self, template_str: str, context: dict) -> str:
        template = self.jinja_env.from_string(template_str)
        return template.render(context)

    def test_tenant_isolation_and_ca_update_effects(self):
        """Aktualizacja CA najemcy A aktualizuje jego węzły i nie narusza najemcy B."""
        cluster_a_backends = ["192.168.1.80", "192.168.1.81", "192.168.1.82"]
        cluster_b_backends = ["192.168.1.90", "192.168.1.91", "192.168.1.92"]

        # Stan początkowy: najemca A i B mają stare certyfikaty
        for b in cluster_a_backends:
            self.cursor.execute(
                "INSERT INTO mysql_servers_ssl_params (hostname, port, username, ssl_ca) "
                "VALUES (?, 3306, '', '/etc/mysql/tls/cluster_a/ca-old.pem')",
                (b,),
            )
        for b in cluster_b_backends:
            self.cursor.execute(
                "INSERT INTO mysql_servers_ssl_params (hostname, port, username, ssl_ca) "
                "VALUES (?, 3306, '', '/etc/mysql/tls/cluster_b/ca-b-initial.pem')",
                (b,),
            )
        self.conn.commit()

        # Wykonaj update dla klastra A z nowym CA
        new_ca_basename = "ca-a1b2c3d4e5f6.pem"
        tls_dir = "/etc/mysql/tls/cluster_a"
        ctx = {
            "galera_backends": cluster_a_backends,
            "proxysql_cluster_tls_dir": tls_dir,
            "proxysql_ca_basename": new_ca_basename,
        }
        rendered_update = self.render_sql(self.update_sql_template, ctx)

        # Filtrujemy instrukcje LOAD/SAVE specyficzne dla admin portu ProxySQL
        sql_statements = [
            stmt.strip()
            for stmt in rendered_update.split(";")
            if stmt.strip()
            and not stmt.strip().startswith("LOAD")
            and not stmt.strip().startswith("SAVE")
        ]

        for stmt in sql_statements:
            self.cursor.execute(stmt)
        self.conn.commit()

        # Sprawdzenie efektów dla klastra A: wszystkie węzły wskazują nową ścieżkę
        self.cursor.execute(
            "SELECT hostname, ssl_ca FROM mysql_servers_ssl_params "
            "WHERE hostname IN (?, ?, ?) ORDER BY hostname",
            tuple(cluster_a_backends),
        )
        rows_a = self.cursor.fetchall()
        expected_ca_a = f"{tls_dir}/{new_ca_basename}"
        self.assertEqual(len(rows_a), 3)
        for host, ca in rows_a:
            self.assertEqual(ca, expected_ca_a)

        # Sprawdzenie efektów dla klastra B: stan nienaruszony (brak wycieku DELETE)
        self.cursor.execute(
            "SELECT hostname, ssl_ca FROM mysql_servers_ssl_params "
            "WHERE hostname IN (?, ?, ?) ORDER BY hostname",
            tuple(cluster_b_backends),
        )
        rows_b = self.cursor.fetchall()
        self.assertEqual(len(rows_b), 3)
        for host, ca in rows_b:
            self.assertEqual(ca, "/etc/mysql/tls/cluster_b/ca-b-initial.pem")

        # Sprawdzenie zapytania weryfikującego (check_sql)
        rendered_check = self.render_sql(self.check_sql_template, ctx)
        self.cursor.execute(rendered_check)
        count_a = self.cursor.fetchone()[0]
        self.assertEqual(count_a, len(cluster_a_backends))


if __name__ == "__main__":
    unittest.main()
