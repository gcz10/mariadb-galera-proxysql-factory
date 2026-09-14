"""Haslo aplikacji nie trafia do argv procesu ansible na kontrolerze (ISC-43).

ZMIERZONE 2026-09-14: probe-app-conformance.py i bench-app.py budowaly
`MYSQL_PWD='<sekret>' mariadb ...` jako tresc skryptu przekazywanego przez
`ansible ... -a <script>` — czyli haslo bylo argumentem PROCESU na hoscie
kontrolnym (argv[7]), widocznym kazdemu przez `ps`, mimo ze docelowy klient
dostawal je zmienna srodowiskowa.

Poprawka idzie tym samym wzorcem co chaos-app-degradation.py i
chaos-proxysql-failover.py: plik opcji klienta [client] wgrany modul `copy`
(mode=0600, owner=root), `--defaults-extra-file` w poleceniu, usuniecie po
pomiarze. mariadb-slap czyta ten sam plik (potwierdzone --print-defaults).

Testy nie dotykaja zadnego zywego hosta: `subprocess.run` jest stubowane, wiec
zaden proces ansible nie powstaje.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

WORKSPACE = Path(__file__).resolve().parents[2]
LAB_DIR = WORKSPACE / "tests" / "lab"

CLUSTER_NAME = "probe-r10"
CLUSTER_YML = {
    "cluster": {"name": CLUSTER_NAME, "environment": "lab"},
    "galera": {"nodes_expected": 3},
    "proxysql": {
        "app_user": "app_user_probe",
        "hostgroup_base": 10,
        "endpoint": {"address": "192.0.2.10", "port": 6033},
    },
    "tls": {"mode": "disabled"},
    "application": {},
}
INVENTORY_YML = {"all": {"children": {
    "galera": {"hosts": {"gnode1": {"ansible_host": "192.0.2.1"}}},
    "proxysql": {"hosts": {"pnode1": {"ansible_host": "192.0.2.11"}}},
    "app": {"hosts": {"appnode": {"ansible_host": "192.0.2.20"}}},
}}}

# Wartosc celowo nie wyglada jak przypisanie sekretu (bramka ISC-43 skanuje
# tez pliki testow), a mimo to zawiera znaki, ktore lamalyby niecytowany
# zapis w pliku opcji: apostrof, kratka, spacja.
FAKE_APP_PASSWORD = "probe pass'#value"


def _write(tmp, name, payload):
    path = tmp / name
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _run_module(tmp, probe_name):
    spec = importlib.util.spec_from_file_location(
        probe_name.replace("-", "_"), LAB_DIR / f"{probe_name}.py"
    )
    env = {
        "CLUSTER": CLUSTER_NAME,
        "CLUSTER_CONFIG": str(_write(tmp, "cluster.yml", CLUSTER_YML)),
        "CLUSTER_INVENTORY": str(_write(tmp, "inventory.yml", INVENTORY_YML)),
        "APP_DB_PASSWORD": FAKE_APP_PASSWORD,
        "APP_VIP_VERIFIED_TLS": "pass",
    }
    if str(LAB_DIR) not in sys.path:
        sys.path.insert(0, str(LAB_DIR))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, env, clear=False):
        spec.loader.exec_module(module)
    return module


class AppConformanceTransportTests(unittest.TestCase):
    def test_module_never_builds_a_client_command_with_the_password(self):
        with tempfile.TemporaryDirectory() as td:
            mod = _run_module(Path(td), "probe-app-conformance")

        cmd = mod.mariadb("SELECT 1", "192.0.2.10", 6033)
        self.assertIn("--defaults-extra-file=", cmd)
        self.assertNotIn("MYSQL_PWD", cmd)
        self.assertNotIn(FAKE_APP_PASSWORD, cmd)
        # Sekret jest w wartosci option-file, ktora moze zawierac apostrof:
        # polecenie klienta nie moze zalezic od tresci hasla.
        self.assertNotIn(f"'{FAKE_APP_PASSWORD}'", cmd)

    def test_client_command_starts_with_the_option_file_flag(self):
        # Kontrakt plikow opcji MariaDB: --defaults-extra-file jest przetwarzany
        # poprawnie tylko jako PIERWSZY argument klienta.
        with tempfile.TemporaryDirectory() as td:
            mod = _run_module(Path(td), "probe-app-conformance")
        cmd = mod.mariadb("SELECT 1", "192.0.2.10", 6033)
        self.assertTrue(
            cmd.startswith("mariadb --defaults-extra-file="),
            f"flaga pliku opcji musi byc pierwsza: {cmd}",
        )

    def test_profile_lifecycle_install_before_measure_remove_always(self):
        # Odpowiedzi snippeta celowo puste: pomiar konczy sie FAIL, a my
        # weryfikujemy LIFECYCLE profilu (instalacja -> pomiary -> usuniecie),
        # nie tresc odpowiedzi. Porazka pomiaru nie zwalnia z posprzatania.
        with tempfile.TemporaryDirectory() as td:
            mod = _run_module(Path(td), "probe-app-conformance")
            argv_seen = []

            def fake_run(argv, **kwargs):
                argv_seen.append(list(argv))
                return mock.Mock(returncode=0, stdout="", stderr="")

            fake_ansible = mock.Mock(
                spec=["returncode", "stdout", "stderr", "bodies", "errors", "body"])
            fake_ansible.bodies = {mod.APP_HOST: ""}
            fake_ansible.errors = {}
            fake_ansible.body = lambda host: "PROBE_RC=0\n"
            import _probe_common
            with mock.patch.object(mod, "run_ansible", return_value=fake_ansible):
                with mock.patch.object(
                        _probe_common.subprocess, "run", fake_run):
                    rc = mod.main()

        self.assertEqual(rc, 1, "pomiar na atrapie nie moze przejsc zielono")
        installs = [a for a in argv_seen if "ansible.builtin.copy" in a]
        removals = [a for a in argv_seen if "state=absent" in " ".join(a)]
        self.assertEqual(len(installs), 1, "profil klienta nie zostal wgrany")
        self.assertEqual(len(removals), 1, "profil klienta nie zostal usuniety po pomiarze")
        # Plik lokalny ginie po transferze, wiec zadne wywolanie nie niesie
        # tresci pliku: argv przenosi wylacznie sciezki.
        self.assertFalse(any(FAKE_APP_PASSWORD in " ".join(a) for a in argv_seen))
        # Usuniecie nastepuje po pomiarach: kopia jest pierwsza, state=absent ostatnie.
        self.assertGreater(argv_seen.index(removals[0]), argv_seen.index(installs[0]))


class BenchAppTransportTests(unittest.TestCase):
    def test_module_never_builds_a_client_command_with_the_password(self):
        # bench-app czyta config przy imporcie; APP_DB_PASSWORD nie moze
        # pojawic sie w ZADNYM budowanym poleceniu klienta.
        with tempfile.TemporaryDirectory() as td:
            mod = _run_module(Path(td), "bench-app")

        cases = (
            ("writer", lambda: mod.active_writer_address()),
            ("cleanup", lambda: mod.cleanup("192.0.2.1")),
            ("leftovers", lambda: mod.report_foreign_leftovers("192.0.2.20")),
        )
        for name, call in cases:
            argv = []

            def fake_on_app(script, timeout=600, _argv=argv):
                _argv.append(script)
                return 0, ""

            with mock.patch.object(mod, "on_app", fake_on_app):
                call()
            self.assertTrue(argv, f"{name}: brak wywolania klienta")
            for script in argv:
                self.assertIn("--defaults-extra-file=", script)
                self.assertNotIn("MYSQL_PWD", script)
                self.assertNotIn(FAKE_APP_PASSWORD, script)


    def test_table_cleanup_precedes_profile_removal(self):
        """Prawdziwy epilog modulu: tabele sprzatane PRZED usunieciem profilu.

        Test uruchamia `bench-app.py` jako `__main__` (runpy), wiec wykonuje sie
        faktyczny blok `finally`, a nie jego kopia w tescie. Wszystkie wywolania
        ansible ida do atrapy, ktora notuje tresc skryptu — kolejnosc widac
        w tym, co poleciało najpierw: `DROP TABLE ...` czy `state=absent`.

        Defekt byl realny: usuniecie profilu przed sprzatanie odcina DROP TABLE
        od bazy (profil jest jedynym nosnikiem poswiadczen), wiec tabele
        pomiarowe zostaja w `isa_test` najemcy.
        """
        import runpy

        captured = []

        def fake_run(argv, **kwargs):
            joined = " ".join(str(a) for a in argv)
            captured.append(joined)
            if "SELECT @@wsrep_node_address" in joined:
                return mock.Mock(returncode=0, stdout="appnode | SUCCESS | rc=0 >>\n192.0.2.1", stderr="")
            if "Average number of seconds" in joined or "mariadb-slap" in joined:
                return mock.Mock(
                    returncode=0,
                    stdout="appnode | SUCCESS | rc=0 >>\n"
                           "Average number of seconds to run all queries: 1.000\n",
                    stderr="",
                )
            return mock.Mock(returncode=0, stdout="appnode | SUCCESS | rc=0 >>\n", stderr="")

        with tempfile.TemporaryDirectory() as td:
            env = {
                "CLUSTER": CLUSTER_NAME,
                "CLUSTER_CONFIG": str(_write(Path(td), "cluster.yml", CLUSTER_YML)),
                "CLUSTER_INVENTORY": str(_write(Path(td), "inventory.yml", INVENTORY_YML)),
                "APP_DB_PASSWORD": FAKE_APP_PASSWORD,
            }
            if str(LAB_DIR) not in sys.path:
                sys.path.insert(0, str(LAB_DIR))
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch("subprocess.run", fake_run):
                    with mock.patch("time.sleep", lambda *_: None):
                        with self.assertRaises(SystemExit):
                            runpy.run_path(str(LAB_DIR / "bench-app.py"), run_name="__main__")

        drops = [c for c in captured if "DROP TABLE" in c]
        removals = [c for c in captured if "state=absent" in c]
        self.assertTrue(drops, f"sprzatanie tabel nie zostalo wywolane: {captured}")
        self.assertEqual(len(removals), 1, captured)
        self.assertLess(
            captured.index(drops[-1]),
            captured.index(removals[0]),
            "profil klienta usunieto przed skasowaniem wlasnych tabel pomiarowych",
        )
        self.assertFalse(any(FAKE_APP_PASSWORD in c for c in captured))


if __name__ == "__main__":
    unittest.main()
