#!/usr/bin/env python3
r"""cluster_upgrade_node.yml: kontrakt gcache + smoke bramki drain na prawdziwym
ansible-playbook.

Defekt wyjściowy: regexp `gcache\.size=[^ \t\n]+` połykał resztę listy opcji
wsrep_provider_options (page_size, socket.dynamic, TLS), bo między opcjami nie
ma spacji. Drugi kontrakt: stop MariaDB nie może się odbyć, dopóki KAŻDA
instancja ProxySQL nie potwierdzi drainu (rejestracja -> OFFLINE_SOFT w
runtime -> ConnUsed==0); brak rejestracji, błąd zapytania, śmieciowy wynik albo
--limit omijający ProxySQL muszą zablokować przebieg.

Smoke NIE emuluje Ansibla i NIE jest emulacją ProxySQL: playbook jest
wykonywany prawdziwym `ansible-playbook` na sztucznych hostach local, a
`mariadb` ze ścieżki PATH to atrapa na plikowej SQLite, która serwuje kształt
odpowiedzi admin-interfejsu (COUNT/SUM/ConnUsed) oraz sztywny stan wsrep dla
bramy zdrowia. Testuje orkiestrację: czy kolejne bramy przerywają przebieg
PRZED ciałem upgrade. Ciało upgrade (systemd/dnf/usługi) jest w kopii testowej
zastąpione zadaniem-znacznikiem tuż za właściwymi bramami (asercja potwierdzeń
drain, lockfile, brama zdrowia) — żadnych operacji systemowych.
"""

import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "playbooks" / "cluster_upgrade_node.yml"
IDENTITY_REAL = "/etc/proxysql/admin-check.cnf"

TARGET_NODE = "gnode2"
TARGET_IP = "10.53.0.12"
PEER_IP = "10.53.0.11"
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")

# Atrapa `mariadb`: czyta SQL z -e albo stdin, serwuje go do plikowej SQLite.
# LOAD MYSQL SERVERS TO RUNTIME kopiuje config do tabeli runtime; zapytania
# o wsrep dostają sztywno zdrowy klaster (dla bramy zdrowia ISC-55).
FAKE_MARIADB = r"""#!/usr/bin/env python3
import os
import sqlite3
import sys


def main():
    argv = sys.argv[1:]
    chunks = []
    i = 0
    while i < len(argv):
        if argv[i] == "-e" and i + 1 < len(argv):
            i += 1
            chunks.append(argv[i])
        i += 1
    if not chunks:
        chunks.append(sys.stdin.read())

    host = os.environ.get("FAKE_MARIADB_HOST", "galera")
    state = os.environ["FAKE_MARIADB_STATE"] + "." + host
    fault = os.environ.get("FAKE_MARIADB_FAULT", "")
    if fault == "peer_error" and host == "px2":
        sys.stderr.write("fake-mariadb: peer unavailable\n")
        return 3

    conn = sqlite3.connect(state)
    out = []
    for chunk in chunks:
        for statement in chunk.split(";"):
            statement = statement.strip()
            if not statement:
                continue
            upper = statement.upper()
            if "WSREP_LOCAL_STATE" in upper:
                out.append(
                    "wsrep_local_state\t4\nwsrep_cluster_status\tPrimary\n"
                    "wsrep_ready\tON\nwsrep_cluster_size\t"
                    + os.environ.get("FAKE_GALERA_SIZE", "3")
                )
                continue
            if upper.startswith("LOAD MYSQL SERVERS TO RUNTIME"):
                conn.execute("DELETE FROM runtime_mysql_servers")
                conn.execute(
                    "INSERT INTO runtime_mysql_servers "
                    "SELECT hostgroup_id, hostname, port, status FROM mysql_servers"
                )
                conn.commit()
                continue
            if "STATS_MYSQL_CONNECTION_POOL" in upper:
                if fault == "malformed":
                    out.append("n/a")
                    continue
                if fault == "busy":
                    conn.execute(
                        "UPDATE stats_mysql_connection_pool SET ConnUsed = 5"
                    )
                    conn.commit()
            if fault == "error" and "UPDATE MYSQL_SERVERS" in upper:
                sys.stderr.write("fake-mariadb: injected error\n")
                sys.exit(3)
            cur = conn.execute(statement)
            if cur.description is not None:
                for row in cur.fetchall():
                    out.append("\t".join(str(value) for value in row))
            conn.commit()
    if out:
        sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    sys.exit(main() or 0)
"""

INVENTORY = {
    "all": {
        "vars": {
            "ansible_connection": "local",
            "ansible_become": False,
            "ansible_python_interpreter": sys.executable,
            "galera": {"nodes_expected": 3},
        },
        "children": {
            "proxysql": {"hosts": {"px1": None, "px2": None}},
            "galera": {
                "hosts": {
                    "gnode1": {"galera_node_address": "10.53.0.11"},
                    "gnode2": {"galera_node_address": TARGET_IP},
                    "gnode3": {"galera_node_address": "10.53.0.13"},
                }
            },
        },
    }
}


def load_plays():
    return yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))


def deep_replace(value, old, new):
    if isinstance(value, dict):
        return {deep_replace(k, old, new): deep_replace(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [deep_replace(item, old, new) for item in value]
    if isinstance(value, str):
        return value.replace(old, new)
    return value


def build_fixture(base):
    """Kopia testowa: grafitowy inwentarz + wyciągnięte play 0-2.

    Jedyna ingerencja w treść playbooka: podmiana ścieżki tożsamości admina
    (admin-check.cnf) na fixture oraz wymiana CIAŁA play 2 (za właściwymi
    bramami) na zadanie-znacznik. Bramy zostają oryginalne.
    """
    bin_dir = base / "bin"
    play_dir = base / "playbooks"
    bin_dir.mkdir(parents=True)
    play_dir.mkdir(parents=True)

    fake = bin_dir / "mariadb"
    fake.write_text(FAKE_MARIADB, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    identity = base / "admin-check.cnf"
    identity.write_text("[client]\nuser=admin\n", encoding="utf-8")

    os.symlink(REPO / "playbooks" / "vars", play_dir / "vars")
    os.symlink(REPO / "playbooks" / "tasks", play_dir / "tasks")
    os.symlink(REPO / "versions", base / "versions")
    os.symlink(REPO / "versions", play_dir / "versions")

    inventory = base / "inventory.yml"
    inventory.write_text(yaml.safe_dump(INVENTORY, sort_keys=False), encoding="utf-8")
    cfg = base / "ansible.cfg"
    cfg.write_text(
        "[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n",
        encoding="utf-8",
    )
    marker = base / "marker.txt"

    plays = deep_replace(load_plays()[:3], IDENTITY_REAL, str(identity))
    plays[1]["environment"] = {"FAKE_MARIADB_HOST": "{{ inventory_hostname }}"}
    upgrade = plays[2]
    body_start = next(
        i
        for i, task in enumerate(upgrade["tasks"])
        if "innodb_fast_shutdown" in str(task)
    )
    upgrade["tasks"] = upgrade["tasks"][:body_start] + [
        {
            "name": "SMOKE MARKER — ciało upgrade osiągnięte",
            "ansible.builtin.copy": {
                "content": "upgrade-body-reached {{ inventory_hostname }}\n",
                "dest": "{{ lookup('ansible.builtin.env', 'SMOKE_MARKER') }}",
                "mode": "0644",
            },
        }
    ]
    site = play_dir / "smoke_site.yml"
    site.write_text(yaml.safe_dump(plays, sort_keys=False), encoding="utf-8")

    return {
        "base": base,
        "bin": bin_dir,
        "cfg": cfg,
        "inventory": inventory,
        "site": site,
        "marker": marker,
        "state": base / "proxysql.sqlite3",
    }


def seed_state(path, target_status="ONLINE"):
    for suffix in ("", ".count"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE mysql_servers (hostgroup_id INT, hostname TEXT, port INT, status TEXT);"
        "CREATE TABLE runtime_mysql_servers (hostgroup_id INT, hostname TEXT, port INT, status TEXT);"
        "CREATE TABLE stats_mysql_connection_pool "
        "(hostgroup_id INT, srv_host TEXT, srv_port INT, ConnUsed INT, ConnFree INT);"
    )
    rows = [(10, PEER_IP, "ONLINE"), (20, PEER_IP, "ONLINE")]
    if target_status is not None:
        rows += [(10, TARGET_IP, target_status), (20, TARGET_IP, target_status)]
    conn.executemany("INSERT INTO mysql_servers VALUES (?,?,3306,?)", rows)
    conn.executemany(
        "INSERT INTO stats_mysql_connection_pool VALUES (?, ?, 3306, ?, 0)",
        [(10, TARGET_IP, 0), (20, TARGET_IP, 0), (10, PEER_IP, 1)],
    )
    conn.commit()
    conn.close()




def tail(text, lines=40):
    return "\n".join(text.splitlines()[-lines:])


class GcacheReplaceTests(unittest.TestCase):
    """Regexp podmiany gcache musi być ograniczony do WARTOŚCI opcji."""

    def setUp(self):
        upgrade_play = next(
            play for play in load_plays() if "upgrade pakietów" in play.get("name", "")
        )
        self.spec = None
        for task in upgrade_play["tasks"]:
            replace = task.get("ansible.builtin.replace")
            if replace and "gcache" in replace.get("regexp", ""):
                self.spec = replace
                break
        self.assertIsNotNone(self.spec, "brak zadania replace dla gcache")
        self.pattern = re.compile(self.spec["regexp"], re.MULTILINE)
        self.replacement = self.spec["replace"]

    def apply(self, content):
        return self.pattern.sub(self.replacement, content)

    # Linia wyrenderowana z roles/mariadb_install/templates/server.cnf.j2
    # (tls.mode=full + socket.dynamic + gcache.page_size).
    TLS_LINE = (
        'wsrep_provider_options = "gcache.size=512M;gcache.page_size=128M;'
        "socket.dynamic=true;"
        "socket.ssl_ca=/etc/mysql/tls/ca.pem;"
        "socket.ssl_cert=/etc/mysql/tls/server-cert.pem;"
        'socket.ssl_key=/etc/mysql/tls/server-key.pem"'
    )

    def test_full_option_list_with_tls_survives_replacement(self):
        """Stary regexp [^ \\t\\n]+ połykał wszystko do białej spacji — tu nie."""
        upgraded = self.apply(self.TLS_LINE)
        self.assertEqual(
            upgraded, self.TLS_LINE.replace("gcache.size=512M", "gcache.size=2G")
        )
        self.assertIn("gcache.size=2G;gcache.page_size=128M", upgraded)
        self.assertIn("socket.dynamic=true;", upgraded)
        self.assertIn('socket.ssl_key=/etc/mysql/tls/server-key.pem"', upgraded)
        self.assertEqual(upgraded.count("gcache.size="), 1)

    def test_match_is_bounded_to_the_value(self):
        """Dowód boundingu: dopasowanie kończy się PRZED średnikiem."""
        match = self.pattern.search(self.TLS_LINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(0), "gcache.size=512M")

    def test_single_quoted_provider_options(self):
        line = "wsrep_provider_options = 'gcache.size=1G;gcache.page_size=128M'"
        self.assertEqual(
            self.apply(line),
            "wsrep_provider_options = 'gcache.size=2G;gcache.page_size=128M'",
        )

    def test_gcache_last_option_before_closing_quote(self):
        line = 'wsrep_provider_options = "gcache.size=1G"'
        self.assertEqual(self.apply(line), 'wsrep_provider_options = "gcache.size=2G"')

    def test_unquoted_value_at_end_of_line(self):
        line = "wsrep_provider_options = gcache.size=1G"
        self.assertEqual(self.apply(line), "wsrep_provider_options = gcache.size=2G")

    def test_replacement_is_idempotent(self):
        once = self.apply(self.TLS_LINE)
        self.assertEqual(self.apply(once), once)

    def test_other_config_lines_untouched(self):
        content = (
            "[mysqld]\ndatadir = /var/lib/mysql\n"
            + self.TLS_LINE
            + "\n\n[sst]\nstreamfmt = mbstream\n"
        )
        upgraded = self.apply(content)
        self.assertEqual(upgraded.count("gcache.size=2G"), 1)
        self.assertIn("datadir = /var/lib/mysql\n", upgraded)
        self.assertIn("[sst]\nstreamfmt = mbstream\n", upgraded)


@unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostępny w PATH")
class DrainGateSmokeTests(unittest.TestCase):
    """Prawdziwy ansible-playbook na wyciągniętych play 0-2 z atrapą mariadb.

    Marker za bramami = jedyny dowód 'ciało upgrade osiągnięte'. Każdy scenariusz
    negatywny kończy się brakiem markera (brama przerwała przebieg).
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="upgrade-smoke-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.fx = build_fixture(Path(cls._tmp.name))

    def run_case(self, fault="", limit=None, target_status="ONLINE"):
        fx = self.fx
        for host in ("px1", "px2"):
            seed_state(Path(str(fx["state"]) + "." + host), target_status)
        if fx["marker"].exists():
            fx["marker"].unlink()
        env = os.environ.copy()
        env.update(
            {
                "PATH": str(fx["bin"]) + os.pathsep + env.get("PATH", ""),
                "FAKE_MARIADB_STATE": str(fx["state"]),
                "FAKE_MARIADB_FAULT": fault,
                "FAKE_GALERA_SIZE": "3",
                "SMOKE_MARKER": str(fx["marker"]),
                "ANSIBLE_CONFIG": str(fx["cfg"]),
            }
        )
        cmd = [
            ANSIBLE_PLAYBOOK,
            "-i",
            str(fx["inventory"]),
            str(fx["site"]),
            "-e",
            "target_node=" + TARGET_NODE,
            "-e",
            "old_mariadb_version=11.4.12",
            # Skrócone oczekiwanie: mechanizm until/retries zostaje prawdziwy,
            # scenariusz negatywny nie może trwać minut.
            "-e",
            "upgrade_drain_retries=2",
            "-e",
            "upgrade_drain_delay=1",
        ]
        if limit:
            cmd += ["--limit", limit]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(fx["base"] / "playbooks"),
            timeout=240,
        )
        return proc

    def assert_reached(self, proc):
        self.assertEqual(proc.returncode, 0, tail(proc.stdout + proc.stderr))
        self.assertTrue(self.fx["marker"].exists(), tail(proc.stdout))
        self.assertIn("upgrade-body-reached " + TARGET_NODE, self.fx["marker"].read_text())
        for host in ("px1", "px2"):
            with sqlite3.connect(str(self.fx["state"]) + "." + host) as db:
                self.assertEqual(
                    db.execute("SELECT DISTINCT status FROM runtime_mysql_servers WHERE hostname=?", (TARGET_IP,)).fetchall(),
                    [("OFFLINE_SOFT",)],
                )
                self.assertEqual(
                    db.execute("SELECT DISTINCT status FROM runtime_mysql_servers WHERE hostname=?", (PEER_IP,)).fetchall(),
                    [("ONLINE",)],
                )

    def assert_blocked(self, proc):
        self.assertNotEqual(proc.returncode, 0, tail(proc.stdout + proc.stderr))
        self.assertFalse(self.fx["marker"].exists(), "marker za bramką po porażce drain")

    def test_drained_node_reaches_upgrade_marker(self):
        proc = self.run_case()
        self.assert_reached(proc)

    def test_already_drained_node_reaches_marker(self):
        """Już odłączony węzeł może przejść bramkę bez ponownego podłączenia."""
        proc = self.run_case(target_status="OFFLINE_SOFT")
        self.assert_reached(proc)

    def test_busy_connections_block_marker(self):
        proc = self.run_case(fault="busy")
        self.assert_blocked(proc)

    def test_query_error_blocks_marker(self):
        proc = self.run_case(fault="error")
        self.assert_blocked(proc)

    def test_malformed_wait_output_blocks_marker(self):
        """ConnUsed raportowany śmieciami (rc=0, stdout!='0') nie może przejść."""
        proc = self.run_case(fault="malformed")
        self.assert_blocked(proc)

    def test_unregistered_target_blocks_marker(self):
        proc = self.run_case(target_status=None)
        self.assert_blocked(proc)

    def test_peer_proxy_failure_blocks_marker(self):
        """Pierwsza instancja drainuje się, druga pada — całość abortuje."""
        proc = self.run_case(fault="peer_error")
        self.assert_blocked(proc)

    def test_limit_skipping_proxies_blocks_marker(self):
        """--limit omijający ProxySQL: ciche pominięcie drainu łapie asercja
        potwierdzeń na początku play 2 — marker niedostępny."""
        proc = self.run_case(limit=TARGET_NODE)
        self.assert_blocked(proc)


if __name__ == "__main__":
    unittest.main()
