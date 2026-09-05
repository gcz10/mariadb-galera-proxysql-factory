"""Kontrakt mapowania zmiennych patcha F12: zakresowe -> wewnętrzne.

PROBLEM. `f12_apply_patch.yml` czyta WYLACZNIE `f12_apply_packages` /
`f12_apply_command`, a playe, ktore go wlaczaja, musza je zmapowac ze
zmiennych ZAKRESOWYCH operatora (`f12_galera_patch_*` dla playow Galera,
`f12_proxysql_patch_*` dla playow ProxySQL). Trzy historyczne awarie:

* wyciek: operatorowa lista pakietow MariiDB podana jako globalna extra-var
  trafiala do play'a ProxySQL i wykonywala tam `dnf update` zamiast dry-run
  (extra-vars maja pierwszenstwo nad play-vars);
* martwe mapowanie: play 4 mapowal na stare nazwy, ktorych include juz nie
  czytal, wiec `f12_proxysql_patch_*` nigdy nie uruchamial realnego patcha
  ProxySQL — zielone przebiegi dowodzily tylko DRY-RUN;
* sprzezenie wlascicielskie: patch pary ProxySQL — zasobu PLATFORMY — dalo
  sie uruchomic WYLACZNIE przez `f12_patch.yml`, ktory przerywa na play'u 0,
  gdy zaden writer NAJEMCY nie jest ONLINE. Warstwa z zatrzymanymi najemcami
  nie miala zadnej sciezki patcha i stala na wydaniu z luka (zmierzone
  2026-09-05 na parze `xenonv11`: ProxySQL 3.0.10 przy pinie 3.0.11).

Dwie pierwsze to regresje tekstu miedzy plikami, trzecia to regresja
zaleznosci. Test asertuje wiec kontrakt dla KAZDEGO playbooka wlaczajacego
`f12_apply_patch.yml` (nowy playbook jest objety automatycznie), a osobno
brak zaleznosci sciezki platformowej od zywego najemcy.
"""

import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "playbooks"
PATCH_PLAY = PLAYBOOKS / "f12_patch.yml"
PLATFORM_PATCH_PLAY = PLAYBOOKS / "platform_patch.yml"
APPLY_INCLUDE = PLAYBOOKS / "f12_apply_patch.yml"

SCOPED_BY_ROLE = {
    "galera": ("f12_galera_patch_packages", "f12_galera_patch_command"),
    "proxysql": ("f12_proxysql_patch_packages", "f12_proxysql_patch_command"),
}
STALE_NAMES = ("f12_patch_packages", "f12_patch_command")

# Nazwy, ktore czynia play zaleznym od ZYWEGO najemcy. W sciezce platformowej
# kazda z nich to nawrot sprzezenia wlascicielskiego: pary ProxySQL nie da sie
# wtedy spatchowac, dopoki ktos nie uruchomi cudzego klastra.
TENANT_COUPLING = (
    "groups['galera']",
    'groups["galera"]',
    "galera_writer_hg",
    "proxysql_hostgroups.yml",
    "PROXYSQL_ADMIN_PASSWORD",
)


def patch_playbooks():
    """Playbooki wlaczajace wspolny include patcha — zrodlo prawdy dla kontraktu."""
    found = {}
    for path in sorted(PLAYBOOKS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if "f12_apply_patch.yml" in text and path != APPLY_INCLUDE:
            found[path] = text
    return found


class TestF12VariableContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playbooks = patch_playbooks()
        cls.apply_text = APPLY_INCLUDE.read_text(encoding="utf-8")

    def test_both_entry_points_include_shared_apply(self):
        # Jedna implementacja patcha dla obu sciezek. Kopia zamiast include'a
        # oznaczalaby, ze poprawka w jednej z nich omija druga.
        self.assertIn(PATCH_PLAY, self.playbooks, "sciezka klastra musi istniec")
        self.assertIn(
            PLATFORM_PATCH_PLAY, self.playbooks, "sciezka platformy musi istniec"
        )

    def test_every_play_maps_scoped_vars_of_its_role(self):
        seen_roles = set()
        for path, text in self.playbooks.items():
            for play in yaml.safe_load(text):
                role = (play.get("vars") or {}).get("f12_role")
                if role is None:
                    continue
                seen_roles.add(role)
                pkg_var, cmd_var = SCOPED_BY_ROLE[role]
                with self.subTest(playbook=path.name, play=play["name"]):
                    self.assertEqual(
                        play["vars"]["f12_apply_packages"],
                        "{{ %s | default([]) }}" % pkg_var,
                    )
                    self.assertEqual(
                        play["vars"]["f12_apply_command"],
                        "{{ %s | default('') }}" % cmd_var,
                    )
        self.assertEqual(seen_roles, set(SCOPED_BY_ROLE), "oba zakresy maja playe")

    def test_no_stale_global_patch_names_anywhere(self):
        # Stare nazwy jako NAZWY ZMIENNYCH do ustawiania nie moga wystapic
        # ani w playach (mapowanie), ani w includzie (czytanie) — to one
        # byly wektorem wycieku extra-vars i martwego mapowania.
        for name in STALE_NAMES:
            with self.subTest(stale=name):
                for path, text in self.playbooks.items():
                    self.assertNotIn(name, text, path.name)
                self.assertNotIn(name, self.apply_text)

    def test_apply_include_reads_only_internal_vars(self):
        # Fragmenty stabilne wzgledem zlamania linii i nawiasow — kontrakt
        # czyta WYLACZNIE wewnetrzne f12_apply_*, nie nazwy operatora.
        self.assertIn("f12_apply_packages | list", self.apply_text)
        self.assertIn('ansible.builtin.shell: "{{ f12_apply_command }}"', self.apply_text)
        self.assertIn("f12_apply_packages | default([])", self.apply_text)
        self.assertIn("f12_apply_command | default('')", self.apply_text)

    def test_every_scoped_var_has_a_consumer(self):
        # Martwa zmienna operatora bylaby cichym dry-run zamiast realnego patcha.
        all_text = "".join(self.playbooks.values())
        for role, (pkg_var, cmd_var) in SCOPED_BY_ROLE.items():
            with self.subTest(role=role):
                self.assertIn(pkg_var, all_text)
                self.assertIn(cmd_var, all_text)

    def test_platform_path_does_not_depend_on_live_tenant(self):
        # Sedno luki z 2026-09-05: patch zasobu platformowego nie moze wymagac
        # zywego klastra najemcy ani jego sekretu.
        #
        # Skanowana jest STRUKTURA (YAML po parsowaniu), nie surowy plik:
        # komentarz wyjasniajacy, ze ta sciezka NIE potrzebuje
        # PROXYSQL_ADMIN_PASSWORD, zapalalby test na czerwono za samo
        # nazwanie problemu. Kontrakt dotyczy tresci wykonywalnej.
        plays = yaml.safe_load(PLATFORM_PATCH_PLAY.read_text(encoding="utf-8"))
        executable = yaml.safe_dump(plays, allow_unicode=True)
        for name in TENANT_COUPLING:
            with self.subTest(coupling=name):
                self.assertNotIn(name, executable)

    def test_platform_path_targets_proxysql_pair_one_at_a_time(self):
        # ISC-57: para aktualizuje sie po jednej instancji. Rownolegly patch
        # obu wezlow zdejmuje endpoint calkowicie.
        plays = yaml.safe_load(PLATFORM_PATCH_PLAY.read_text(encoding="utf-8"))
        self.assertEqual(len(plays), 1, "sciezka platformowa to jeden play")
        self.assertEqual(plays[0]["hosts"], "proxysql")
        self.assertEqual(plays[0]["serial"], 1)


if __name__ == "__main__":
    unittest.main()
