#!/usr/bin/env python3
"""Lockfile musi przypinac REPOZYTORIUM, nie tylko numer wersji.

POWSTAL PO REALNEJ AWARII (blue-r9, 2026-08-24). Lockfile deklarowal
`mariadb.version: 11.4.12`, ale `repo_setup_args` konfigurowalo repozytorium
SERII (`--mariadb-server-version=11.4`). Repozytorium serii trzyma wylacznie
najnowsza latke: gdy upstream wydal 11.4.13 w trakcie budowy, wezly postawione
wczesniej mialy 11.4.12, a host dolaczany kilkanascie minut pozniej dostal
`No package MariaDB-server-11.4.12-1.el9 available` i build padl.

Deklaracja wersji bez przypietego zrodla pakietow nie jest przypieciem: build
przestaje byc odtwarzalny przy pierwszym wydaniu upstreamu, a klaster potrafi
skonczyc z mieszanymi wersjami na wezlach.

Kontrakt: argumenty repo_setup MUSZA wskazywac dokladnie te wersje, ktora
lockfile deklaruje, w formacie wymaganym przez dokumentacje MariaDB
(prefiks `mariadb-` + pelna wersja).
"""
import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
LOCKFILES = sorted((REPO / "versions").glob("versions*.lock.yml"))


class LockfileRepoPinningTests(unittest.TestCase):
    def test_lockfiles_exist(self):
        self.assertTrue(LOCKFILES, "brak jakiegokolwiek lockfile w versions/")

    def test_repo_args_pin_the_declared_version(self):
        for path in LOCKFILES:
            if path.name == "candidate.lock.yml":
                continue  # z zalozenia niedokonczony, nikt go nie wskazuje
            with self.subTest(lockfile=path.name):
                lock = yaml.safe_load(path.read_text(encoding="utf-8"))
                mariadb = lock.get("mariadb") or {}
                version = str(mariadb.get("version", ""))
                args = str(mariadb.get("repo_setup_args", ""))
                self.assertTrue(version, "lockfile nie deklaruje mariadb.version")
                self.assertIn(
                    f"mariadb-{version}",
                    args,
                    f"repo_setup_args={args!r} nie przypina wersji {version} — "
                    "repozytorium serii wyda najnowsza latke, nie te zadeklarowana",
                )

    def test_repo_args_are_not_a_series(self):
        """`--mariadb-server-version=11.4` to seria, nie wersja."""
        series = re.compile(r"--mariadb-server-version=(?:mariadb-)?\d+\.\d+(?:\s|$)")
        for path in LOCKFILES:
            if path.name == "candidate.lock.yml":
                continue
            with self.subTest(lockfile=path.name):
                lock = yaml.safe_load(path.read_text(encoding="utf-8"))
                args = str((lock.get("mariadb") or {}).get("repo_setup_args", ""))
                self.assertIsNone(
                    series.search(args),
                    f"{path.name}: repo_setup_args={args!r} wskazuje serie, "
                    "wiec build nie jest odtwarzalny po kolejnym wydaniu upstreamu",
                )


if __name__ == "__main__":
    unittest.main()
