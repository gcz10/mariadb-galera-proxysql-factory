#!/usr/bin/env python3
"""Baza hostgroup rezerwuje CZTERY grupy, nie jedna.

POWSTAL PO REALNEJ KOLIZJI (blue-r9, 2026-08-24). Drugi najemca wspolnego
ProxySQL dostal `hostgroup_base: 900`, a pierwszy mial 890. Bazy sa rozne, wiec
sonda rozlacznosci przepuscila konfiguracje — tyle ze 890 rozwija sie na
890/900/910/920, czyli 900 to backup_writer pierwszego najemcy. Rejestracja
drugiego wstawila jego wezly do puli, na ktora pierwszy przelacza sie przy
failoverze writera: `green` moglby zaczac pisac do bazy `blue`.

Objaw byl mylacy — `cluster-proxysql` wisial na bramce ISC-20 ("jeden aktywny
writer"), bo monitor Galery nie mial dla drugiego najemcy wlasnego wiersza w
`mysql_galera_hostgroups`. Awaria wygladala jak problem z monitorem, a byla
kolizja adresacji hostgroup.

Kontrakt: sonda musi porownywac PELNY zakres bazy, nie sama jej wartosc.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "tests" / "validation" / "probe-proxysql-tenancy.py"
HOSTGROUPS_VARS = REPO / "playbooks" / "vars" / "proxysql_hostgroups.yml"


def tenant(name, base, app_user, endpoint="10.9.9.9"):
    return {
        "cluster": {"name": name, "environment": "laboratory", "profile": "laboratory"},
        "proxysql": {
            "nodes_expected": 2,
            "hostgroup_base": base,
            "app_user": app_user,
            "endpoint": {"type": "keepalived_vip", "address": endpoint, "port": 6033},
        },
    }


class HostgroupSpanContractTests(unittest.TestCase):
    def run_probe(self, tenants):
        """Uruchamia sonde na tymczasowym drzewie klastrow."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for cfg in tenants:
                d = root / "clusters" / cfg["cluster"]["name"]
                d.mkdir(parents=True)
                (d / "cluster.yml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(PROBE)],
                cwd=root,
                capture_output=True,
                text=True,
            )

    def test_offsets_match_the_playbook_variables(self):
        """Sonda i runtime musza liczyc te same przesuniecia.

        Gdyby playbook zmienil krok z 10 na inny, kontrakt sondy stalby sie
        fikcja — dlatego czytamy wartosci z jedynego zrodla prawdy.
        """
        text = HOSTGROUPS_VARS.read_text(encoding="utf-8")
        for offset in (10, 20, 30):
            self.assertIn(
                f"galera_hostgroup_base | int + {offset}",
                text,
                f"playbook nie wyprowadza juz hostgroupy z przesunieciem {offset}",
            )

    def test_adjacent_bases_are_rejected(self):
        """890 i 900 to kolizja: 900 jest backup_writerem najemcy z baza 890."""
        result = self.run_probe(
            [tenant("first", 890, "app_first"), tenant("second", 900, "app_second")]
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("900", result.stdout)

    def test_overlap_at_any_offset_is_rejected(self):
        """Zachodzenie na reader (+20) albo offline (+30) tez jest kolizja."""
        for base in (910, 920):
            with self.subTest(base=base):
                result = self.run_probe(
                    [tenant("first", 890, "app_first"), tenant("second", base, "app_second")]
                )
                self.assertEqual(result.returncode, 1, f"baza {base} przeszla mimo zachodzenia")

    def test_disjoint_bases_pass(self):
        """930 to pierwsza wolna baza po zakresie 890-920."""
        result = self.run_probe(
            [tenant("first", 890, "app_first"), tenant("second", 930, "app_second")]
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_single_tenant_needs_no_disjointness(self):
        """Jeden najemca nie ma z kim kolidowac."""
        result = self.run_probe([tenant("only", 890, "app_only")])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
