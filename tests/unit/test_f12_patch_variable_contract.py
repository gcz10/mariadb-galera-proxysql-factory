"""Kontrakt mapowania zmiennych patcha F12: zakresowe -> wewnętrzne.

PROBLEM. `f12_apply_patch.yml` czyta WYLACZNIE `f12_apply_packages` /
`f12_apply_command`, a playe `f12_patch.yml` musza je zmapowac ze zmiennych
ZAKRESOWYCH operatora (`f12_galera_patch_*` dla playow Galera,
`f12_proxysql_patch_*` dla play'a ProxySQL). Dwie historyczne awarie tej
pary:

* wyciek: operatorowa lista pakietow MariiDB podana jako globalna extra-var
  trafiala do play'a ProxySQL i wykonywala tam `dnf update` zamiast dry-run
  (extra-vars maja pierwszenstwo nad play-vars);
* martwe mapowanie: play 4 mapowal na stare nazwy, ktorych include juz nie
  czytal, wiec `f12_proxysql_patch_*` nigdy nie uruchamial realnego patcha
  ProxySQL — zielone przebiegi dowodzily tylko DRY-RUN.

Obie sa regresjami tekstu miedzy dwoma plikami, wiec test asertuje sam
kontrakt: kazdy play z `f12_apply_*` mapuje je ze zmiennej swojego zakresu,
a zadne stare/globalne nazwy nie wystepuja ani w playach, ani w includzie.
"""

import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PATCH_PLAY = REPO / "playbooks" / "f12_patch.yml"
APPLY_INCLUDE = REPO / "playbooks" / "f12_apply_patch.yml"

SCOPED_BY_ROLE = {
    "galera": ("f12_galera_patch_packages", "f12_galera_patch_command"),
    "proxysql": ("f12_proxysql_patch_packages", "f12_proxysql_patch_command"),
}
STALE_NAMES = ("f12_patch_packages", "f12_patch_command")


class TestF12VariableContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plays = yaml.safe_load(PATCH_PLAY.read_text(encoding="utf-8"))
        cls.apply_text = APPLY_INCLUDE.read_text(encoding="utf-8")

    def test_every_galera_play_maps_scoped_galera_vars(self):
        galera_plays = [
            p for p in self.plays if (p.get("vars") or {}).get("f12_role") == "galera"
        ]
        self.assertTrue(galera_plays, "f12_patch.yml musi miec playe Galera")
        for play in galera_plays:
            with self.subTest(play=play["name"]):
                self.assertEqual(
                    play["vars"]["f12_apply_packages"],
                    "{{ f12_galera_patch_packages | default([]) }}",
                )
                self.assertEqual(
                    play["vars"]["f12_apply_command"],
                    "{{ f12_galera_patch_command | default('') }}",
                )

    def test_proxysql_play_maps_scoped_proxysql_vars(self):
        proxysql_plays = [
            p for p in self.plays if (p.get("vars") or {}).get("f12_role") == "proxysql"
        ]
        self.assertEqual(len(proxysql_plays), 1, "dokladnie jeden play ProxySQL")
        play = proxysql_plays[0]
        self.assertEqual(
            play["vars"]["f12_apply_packages"],
            "{{ f12_proxysql_patch_packages | default([]) }}",
        )
        self.assertEqual(
            play["vars"]["f12_apply_command"],
            "{{ f12_proxysql_patch_command | default('') }}",
        )

    def test_no_stale_global_patch_names_anywhere(self):
        # Stare nazwy jako NAZWY ZMIENNYCH do ustawiania nie moga wystapic
        # ani w playach (mapowanie), ani w includzie (czytanie) — to one
        # byly wektorem wycieku extra-vars i martwego mapowania.
        for name in STALE_NAMES:
            with self.subTest(stale=name):
                self.assertNotIn(name, PATCH_PLAY.read_text(encoding="utf-8"))
                self.assertNotIn(name, self.apply_text)

    def test_apply_include_reads_only_internal_vars(self):
        # Fragmenty stabilne wzgledem zlamania linii i nawiasow — kontrakt
        # czyta WYLACZNIE wewnetrzne f12_apply_*, nie nazwy operatora.
        self.assertIn("f12_apply_packages | list", self.apply_text)
        self.assertIn('ansible.builtin.shell: "{{ f12_apply_command }}"', self.apply_text)
        self.assertIn("f12_apply_packages | default([])", self.apply_text)
        self.assertIn("f12_apply_command | default('')", self.apply_text)
        # Kazda zmienna zakresowa ma konsumenta w playach — martwa zmienna
        # operatora bylaby cichym dry-run zamiast realnego patcha.
        play_text = PATCH_PLAY.read_text(encoding="utf-8")
        for role, (pkg_var, cmd_var) in SCOPED_BY_ROLE.items():
            with self.subTest(role=role):
                self.assertIn(pkg_var, play_text)
                self.assertIn(cmd_var, play_text)


if __name__ == "__main__":
    unittest.main()
