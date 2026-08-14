"""Unit tests for check_proxysql.sh healthcheck logic (ISC-26).

Verifies the SQL predicate for ProxySQL health reporting across single and
multi-tenant clusters, including offline failover and partial outages.
"""

import sqlite3
import unittest


class CheckProxysqlQueryTests(unittest.TestCase):
    """Tests the SQL query used in check_proxysql.sh against sqlite3."""

    QUERY = """
    SELECT COUNT(*) FROM runtime_mysql_servers s
      JOIN runtime_mysql_galera_hostgroups g
        ON s.hostgroup_id = g.writer_hostgroup
     WHERE g.active = 1 AND s.status = 'ONLINE'
    """

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
        self.cursor.execute(self.QUERY)
        return self.cursor.fetchone()[0]

    def test_zero_groups_is_unhealthy(self):
        """No active Galera groups configured -> 0 online writers -> fails check."""
        self.assertEqual(self.run_query(), 0)

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
