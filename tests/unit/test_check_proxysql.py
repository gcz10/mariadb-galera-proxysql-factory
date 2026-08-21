"""Kontrakt bramki zdrowia check_proxysql.sh (ISC-26).

Bramka rozroznia DWA stany, ktore do 2026-08-21 byly sklejone w jeden:

  (a) ZERO aktywnych grup Galera — warstwa wspolna stoi, ale nie ma jeszcze
      zadnego najemcy. Legalny stan swiezo zbudowanej platformy: nie istnieje
      klient, ktoremu mozna by zle skierowac ruch. ZDROWY.
  (b) SA aktywne grupy, ale zaden writer nie jest ONLINE. To awaria, ktorej
      ISC-26 pilnuje: VIP nie moze wskazywac instancji odpowiadajacej bledem
      na kazde zapytanie. NIEZDROWY.

Stara wersja zwracala 1 takze w (a), przez co Keepalived nigdy nie bral VIP-a
na swiezej parze i `make platform-build` nie mogl sie skonczyc bez uprzedniego
zarejestrowania klastra — czyli warstwa NIE byla niezalezna od najemcow.

Zapytanie jest CZYTANE ZE SKRYPTU, nie kopiowane. Poprzednia wersja tego pliku
trzymala wlasna kopie SQL i dlatego przespala zmiane skryptu: testy dalej byly
zielone, mimo ze `test_zero_groups_is_unhealthy` bronil juz nieprawdziwego
kontraktu. Ta sama lekcja co w test_deregister_rule_pattern.py.
"""

import os
import sqlite3
import unittest

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "roles", "proxysql_endpoint", "files", "check_proxysql.sh",
)


def script_queries() -> tuple[str, str]:
    """Wyciaga ze skryptu podzapytania liczace grupy i ONLINE writerow.

    Licznik nawiasow, nie regexp: `COUNT(*)` samo zawiera nawiasy, wiec kazdy
    leniwy wzorzec konczy sie w zlym miejscu.
    """
    with open(SCRIPT, encoding="utf-8") as handle:
        body = handle.read()
    marker = "(SELECT COUNT(*)"
    found: list[str] = []
    idx = body.find(marker)
    while idx != -1:
        depth = 0
        for pos in range(idx, len(body)):
            if body[pos] == "(":
                depth += 1
            elif body[pos] == ")":
                depth -= 1
                if depth == 0:
                    found.append(body[idx + 1:pos].strip())
                    break
        idx = body.find(marker, idx + 1)
    if len(found) != 2:
        raise AssertionError(
            f"oczekiwano 2 podzapytan COUNT w {SCRIPT}, znaleziono {len(found)}"
        )
    return found[0], found[1]


class CheckProxysqlQueryTests(unittest.TestCase):
    """Sprawdza predykaty SQL bramki na sqlite3, na danych z realnych scenariuszy."""

    @classmethod
    def setUpClass(cls):
        cls.groups_query, cls.writers_query = script_queries()

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("""
            CREATE TABLE runtime_mysql_galera_hostgroups (
                writer_hostgroup INT,
                backup_writer_hostgroup INT,
                reader_hostgroup INT,
                offline_hostgroup INT,
                active INT
            )
        """)
        self.cursor.execute("""
            CREATE TABLE runtime_mysql_servers (
                hostgroup_id INT,
                hostname TEXT,
                port INT,
                status TEXT
            )
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def run_query(self) -> int:
        """Liczba ONLINE writerow — czlon (b) predykatu."""
        self.cursor.execute(self.writers_query)
        return self.cursor.fetchone()[0]

    def active_groups(self) -> int:
        self.cursor.execute(self.groups_query)
        return self.cursor.fetchone()[0]

    def healthy(self) -> bool:
        """Odwzorowuje decyzje skryptu: przy zerze grup nie ma czego wymagac."""
        return self.active_groups() == 0 or self.run_query() >= 1

    def test_zero_groups_is_healthy(self):
        """Swieza warstwa bez najemcow MUSI byc zdrowa — inaczej VIP nigdy nie wstanie."""
        self.assertEqual(self.active_groups(), 0)
        self.assertTrue(self.healthy())

    def test_active_group_without_online_writer_is_unhealthy(self):
        """Jest najemca, nie ma writera — dokladnie ten stan chroni ISC-26."""
        self.cursor.execute(
            "INSERT INTO runtime_mysql_galera_hostgroups VALUES (10, 20, 30, 40, 1)")
        self.cursor.execute(
            "INSERT INTO runtime_mysql_servers VALUES (40, '10.0.0.1', 3306, 'ONLINE')")
        self.conn.commit()
        self.assertEqual(self.active_groups(), 1)
        self.assertEqual(self.run_query(), 0)
        self.assertFalse(self.healthy())

    def test_single_healthy_cluster(self):
        """1 active cluster with 1 ONLINE writer -> count=1 -> healthy."""
        self.cursor.execute("INSERT INTO runtime_mysql_galera_hostgroups VALUES (10, 20, 30, 40, 1)")
        self.cursor.execute("INSERT INTO runtime_mysql_servers VALUES (10, '192.168.1.140', 3306, 'ONLINE')")
        self.cursor.execute("INSERT INTO runtime_mysql_servers VALUES (30, '192.168.1.141', 3306, 'ONLINE')")
        self.cursor.execute("INSERT INTO runtime_mysql_servers VALUES (30, '192.168.1.142', 3306, 'ONLINE')")
        self.conn.commit()

        self.assertEqual(self.run_query(), 1)

    def test_nodes_moved_to_offline_hostgroup_is_unhealthy(self):
        """When Galera nodes lose sync, ProxySQL moves them to offline_hostgroup (40).

        Even if status='ONLINE' in offline_hostgroup, writer_hostgroup has 0 -> count=0.
        """
        self.cursor.execute("INSERT INTO runtime_mysql_galera_hostgroups VALUES (10, 20, 30, 40, 1)")
        # All 3 nodes degraded/shunned and moved to offline_hostgroup=40:
        self.cursor.execute("INSERT INTO runtime_mysql_servers VALUES (40, '192.168.1.140', 3306, 'ONLINE')")
        self.cursor.execute("INSERT INTO runtime_mysql_servers VALUES (40, '192.168.1.141', 3306, 'ONLINE')")
        self.cursor.execute("INSERT INTO runtime_mysql_servers VALUES (40, '192.168.1.142', 3306, 'ONLINE')")
        self.conn.commit()

        self.assertEqual(self.run_query(), 0)

    def test_multi_tenant_one_healthy_one_dead(self):
        """Shared ProxySQL pair: Cluster A (HG 110) healthy, Cluster B (HG 10) dead.

        Count=1 -> keeps VIP active so Cluster A continues serving application traffic.
        """
        # Cluster A (HG 110) - healthy writer
        self.cursor.execute("INSERT INTO runtime_mysql_galera_hostgroups VALUES (110, 120, 130, 140, 1)")
        self.cursor.execute("INSERT INTO runtime_mysql_servers VALUES (110, '192.168.1.150', 3306, 'ONLINE')")

        # Cluster B (HG 10) - dead (all nodes in offline HG 40)
        self.cursor.execute("INSERT INTO runtime_mysql_galera_hostgroups VALUES (10, 20, 30, 40, 1)")
        self.cursor.execute("INSERT INTO runtime_mysql_servers VALUES (40, '192.168.1.140', 3306, 'ONLINE')")
        self.conn.commit()

        self.assertEqual(self.run_query(), 1)

    def test_multi_tenant_both_dead(self):
        """Shared ProxySQL pair: both clusters have 0 writers -> count=0 -> withdraws VIP."""
        self.cursor.execute("INSERT INTO runtime_mysql_galera_hostgroups VALUES (110, 120, 130, 140, 1)")
        self.cursor.execute("INSERT INTO runtime_mysql_servers VALUES (140, '192.168.1.150', 3306, 'ONLINE')")

        self.cursor.execute("INSERT INTO runtime_mysql_galera_hostgroups VALUES (10, 20, 30, 40, 1)")
        self.cursor.execute("INSERT INTO runtime_mysql_servers VALUES (40, '192.168.1.140', 3306, 'ONLINE')")
        self.conn.commit()

        self.assertEqual(self.run_query(), 0)

    def test_inactive_group_ignored(self):
        """Disabled Galera group (active=0) is not counted."""
        self.cursor.execute("INSERT INTO runtime_mysql_galera_hostgroups VALUES (10, 20, 30, 40, 0)")
        self.cursor.execute("INSERT INTO runtime_mysql_servers VALUES (10, '192.168.1.140', 3306, 'ONLINE')")
        self.conn.commit()

        self.assertEqual(self.run_query(), 0)


if __name__ == "__main__":
    unittest.main()
