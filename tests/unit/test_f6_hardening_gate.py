#!/usr/bin/env python3
"""Playbook, ktory nie moze wykonac ZADNEJ kontroli, nie moze meldowac sukcesu.

ZMIERZONE 2026-09-14 prawdziwym `ansible-playbook` z klientem zwracajacym blad
2002: `f6_hardening.yml` konczyl sie `meta: end_play`, gdy MariaDB nie
odpowiadala po sockecie. Caly hardening byl wiec pomijany, a przebieg
raportowal `exit 0` / `failed=0` — zadna z asercji ISC-40/41/42 nie zostala
wykonana, a operator uruchamiajacy sam `make cluster-hardening` dostawal
zielony wynik.

Ten test wykonuje PRAWDZIWY playbook na inwentarzu z `connection: local` i
podstawionym klientem `mariadb`. Werdykt opiera sie wylacznie na zachowaniu:
kod wyjscia i licznik `failed` z PLAY RECAP.

Trzy przypadki:
  1. klient zwraca blad  -> przebieg MUSI byc czerwony (fail-closed),
  2. klient odpowiada poprawnie -> przebieg MUSI byc zielony (bramka nie jest
     slepa na zdrowy klaster),
  3. statyczny kontrakt: `meta: end_play` nie wraca do pliku.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "playbooks" / "f6_hardening.yml"

INVENTORY = {
    "all": {
        "vars": {"ansible_connection": "local", "ansible_become": False},
        "children": {"galera": {"hosts": {"g1": {}}}},
    }
}
CLUSTER = {
    "cluster": {"name": "probe", "environment": "laboratory"},
    "tls": {"mode": "disabled"},
}

BROKEN_CLIENT = """#!/bin/sh
echo "ERROR 2002 (HY000): Can't connect to local server through socket '/var/lib/mysql/mysql.sock' (2)" >&2
exit 1
"""

# Odpowiada kazde zapytanie, ktore zadaje playbook, w stanie ZGODNYM z polityka:
# zero anonimowych kont, brak bazy `test`, root tylko lokalnie, granty SST
# i PMM na miejscu.
HEALTHY_CLIENT = """#!/bin/sh
sql=""
for a in "$@"; do case "$a" in *SELECT*|*SHOW*|*CONCAT*) sql="$a";; esac; done
case "$sql" in
  *"SELECT 1"*) echo 1 ;;
  *"user='root' AND host NOT IN"*) exit 0 ;;
  *"authentication_string=''"*) exit 0 ;;
  *"SHOW GRANTS FOR 'sst_user'"*) echo "GRANT RELOAD, PROCESS, LOCK TABLES, BINLOG ADMIN, REPLICATION CLIENT ON *.* TO \`sst_user\`@\`localhost\`";;
  *"COUNT(*) FROM mysql.user"*) echo 1 ;;
  *"SHOW GRANTS FOR 'pmm_monitor'"*) echo "GRANT PROCESS, SELECT ON *.* TO \`pmm_monitor\`@\`%\`";;
  *) exit 0 ;;
esac
exit 0
"""


class HardeningGateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="f6-hardening-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        (self.root / "inventory.yml").write_text(yaml.safe_dump(INVENTORY), encoding="utf-8")
        (self.root / "cluster.yml").write_text(yaml.safe_dump(CLUSTER), encoding="utf-8")

    def _install_client(self, body):
        client = self.bindir / "mariadb"
        client.write_text(body, encoding="utf-8")
        client.chmod(client.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _run_playbook(self):
        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{self.bindir}{os.pathsep}{env.get('PATH', '')}",
                "ANSIBLE_NOCOLOR": "1",
                "ANSIBLE_LOCAL_TEMP": str(self.root / "ansible-tmp"),
                "HOME": str(self.root),
            }
        )
        return subprocess.run(
            [
                "ansible-playbook",
                str(PLAYBOOK),
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

    def test_unreachable_database_fails_instead_of_skipping_the_whole_play(self):
        self._install_client(BROKEN_CLIENT)
        result = self._run_playbook()

        self.assertNotEqual(
            result.returncode,
            0,
            f"hardening bez wykonanej kontroli zakonczyl sie sukcesem:\n{result.stdout}",
        )
        recap = re.search(r"^g1\s*:.*$", result.stdout, re.M)
        self.assertIsNotNone(recap, result.stdout)
        self.assertIn("failed=1", recap.group(0))
        self.assertIn("NIE zostal wykonany", result.stdout)
        # Komunikat musi niesc PRZYCZYNE. Sonda z `2>/dev/null` czynila z niej
        # puste nawiasy `()` — operator nie wiedzial, czy serwer nie zyje, czy
        # klient nie istnieje.
        self.assertIn(
            "ERROR 2002",
            result.stdout,
            f"diagnoza klienta nie dotarla do operatora:\n{result.stdout}",
        )

    def test_healthy_database_still_passes(self):
        self._install_client(HEALTHY_CLIENT)
        result = self._run_playbook()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        recap = re.search(r"^g1\s*:.*$", result.stdout, re.M)
        self.assertIn("failed=0", recap.group(0))
        # Kontrole faktycznie sie wykonaly — inaczej zielony wynik nic nie znaczy.
        for probe in ("hardening kont i baz MariaDB",):
            self.assertIn(probe, result.stdout)
        self.assertIn("Hardening zakończony", result.stdout)

    def test_playbook_never_ends_the_play_instead_of_failing(self):
        text = PLAYBOOK.read_text(encoding="utf-8")
        self.assertNotIn(
            "ansible.builtin.meta: end_play",
            text,
            "pominiecie calego play'a to fałszywy sukces: asercje nie wykonane, kod 0",
        )


if __name__ == "__main__":
    unittest.main()
