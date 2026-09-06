"""Rotacja monitora ProxySQL ma kolejnosc faz, ktora jest bezpiecznikiem.

PROBLEM. `mysql-monitor_username`/`mysql-monitor_password` sa GLOBALNE dla
instancji ProxySQL, a konto backendu zaklada kazdy najemca osobno. Zmiana jednej
strony bez drugiej daje okno, w ktorym monitor nie moze sie zalogowac i ProxySQL
shunuje ZDROWE backendy — calej floty naraz, bo jedna para obsluguje wszystkich
najemcow.

Kolejnosc expand -> switch -> contract likwiduje to okno:
* expand   — obie tozsamosci istnieja rownoczesnie, nowa potwierdzona na KAZDYM
             backendzie KAZDEGO najemcy,
* switch   — pojedyncza zmiana pary w ProxySQL,
* contract — dopiero teraz znika konto, ktorego ProxySQL juz nie uzywa.

Contract NIE wierzy wyborowi z pre_tasks: tuz przed kasowaniem PONOWNIE czyta
runtime i konfiguracje dyskowa OBU proxy i kasuje wylacznie wtedy, gdy kazdy
odczyt wskazuje ta sama znana tozsamosc aktywna. Bramke testuje prawdziwy
ansible-playbook (szkielet jak w test_cluster_upgrade_node.py): atrapa `mariadb`
serwuje odpowiedzi wg ksztaltu zapytania i tozsamosci peera, a zadanie kasujace
jest w kopii testowej zadaniem-znacznikiem — marker za bramka to jedyny dowod
„kasowanie dopuszczone”, a brak markera w scenariuszach negatywnych dowodzi, ze
bramka przerwala przebieg przed kasowaniem.
"""

import copy
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
MAKEFILE = REPO / "Makefile"
TENANT_PLAY = REPO / "playbooks" / "monitor_rotate.yml"
SWITCH_PLAY = REPO / "playbooks" / "platform_monitor_switch.yml"
IDENTITY_REAL = "/etc/proxysql/admin-check.cnf"
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")

USER_A = "proxysql_monitor"
USER_B = "proxysql_monitor_b"


def _recipe(target: str) -> str:
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(target)}:.*?$(.*?)(?=^\S|\Z)", text, re.M | re.S)
    assert match, f"brak celu {target} w Makefile"
    return match.group(1)


class PhaseOrderIsEnforcedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recipe = _recipe("platform-monitor-rotate")

    def test_all_three_phases_are_present(self):
        for phase in ("rotation_phase=expand", "platform_monitor_switch.yml", "rotation_phase=contract"):
            self.assertIn(phase, self.recipe, f"brak fazy: {phase}")

    def test_expand_precedes_switch_precedes_contract(self):
        expand = self.recipe.index("rotation_phase=expand")
        switch = self.recipe.index("platform_monitor_switch.yml")
        contract = self.recipe.index("rotation_phase=contract")
        self.assertLess(
            expand, switch,
            "switch przed expand = ProxySQL wskazuje konto, ktorego backendy nie znaja",
        )
        self.assertLess(
            switch, contract,
            "contract przed switch = kasujemy konto, z ktorego ProxySQL wlasnie korzysta",
        )

    def test_rotation_is_confirm_gated_and_needs_new_secret(self):
        self.assertIn('test "$(CONFIRM)" = "yes"', self.recipe)
        self.assertIn("PROXYSQL_MONITOR_PASSWORD_NEXT", self.recipe)

    def test_converge_identity_is_external_source_of_truth(self):
        for path in (REPO / "playbooks" / "platform_proxysql.yml", REPO / "playbooks" / "f7_proxysql.yml"):
            body = path.read_text(encoding="utf-8")
            self.assertIn("PROXYSQL_MONITOR_USER", body)
            self.assertNotIn('proxysql_monitor_user: "proxysql_monitor"', body)

    def test_every_tenant_is_covered_and_failure_stops_rotation(self):
        self.assertIn("$(TENANTS)", self.recipe, "rotacja obejmuje flote, nie jeden CLUSTER=")
        self.assertIn("|| exit 1", self.recipe, "porazka najemcy nie moze isc dalej po cichu")


class DeletionTargetingTests(unittest.TestCase):
    """Kasowac wolno wylacznie tozsamosc bezczynna wyznaczona z odczytow pary.

    Odczyt z JEDNEGO wezla (byly `groups['proxysql'][0]`) i asercja
    `idle != active` (zawsze prawdziwa, bo idle wyznaczano z active) nie
    chronily przed niczym — dlatego tu sa testy calej pary, obu warstw
    i polozenia bramki bezposrednio przed kasowaniem.
    """

    @classmethod
    def setUpClass(cls):
        cls.plays = yaml.safe_load(TENANT_PLAY.read_text(encoding="utf-8"))
        cls.tasks = []
        for play in cls.plays:
            cls.tasks.extend(play.get("pre_tasks") or [])
            cls.tasks.extend(play.get("tasks") or [])

    def _mariadb_user_tasks(self):
        return [t for t in self.tasks if "ansible.mariadb.mariadb_user" in t]

    def test_absent_task_targets_the_idle_identity_only(self):
        removals = [
            t for t in self._mariadb_user_tasks()
            if t["ansible.mariadb.mariadb_user"].get("state") == "absent"
        ]
        self.assertTrue(removals, "faza contract musi usuwac konto")
        for task in removals:
            name = task["ansible.mariadb.mariadb_user"]["name"]
            self.assertIn(
                "monitor_idle_user", name,
                "kasujemy WYLACZNIE tozsamosc bezczynna; uzycie 'active' albo "
                "'current'/'target' juz raz doprowadzilo do skasowania konta uzywanego",
            )
            self.assertNotIn("monitor_active_user", name)

    def test_creation_targets_the_idle_identity(self):
        creations = [
            t for t in self._mariadb_user_tasks()
            if t["ansible.mariadb.mariadb_user"].get("state") == "present"
        ]
        self.assertTrue(creations)
        for task in creations:
            self.assertIn("monitor_idle_user", task["ansible.mariadb.mariadb_user"]["name"])

    def test_reads_cover_whole_pair_in_both_layers(self):
        blob = yaml.safe_dump(self.plays, allow_unicode=True)
        self.assertNotIn(
            "groups['proxysql'][0]", blob,
            "odczyt z jednego wezla pary nie wykrywa rozjazdu i pozwolil skasowac "
            "konto uzywane przez drugi wezel",
        )
        for register in ("monitor_runtime_reads", "monitor_disk_reads", "monitor_contract_reads"):
            task = next(t for t in self.tasks if t.get("register") == register)
            self.assertEqual(task["delegate_to"], "{{ item }}")
            self.assertEqual(task["loop"], "{{ groups['proxysql'] }}")

    def test_recheck_and_refusal_immediately_precede_removal(self):
        body = []
        for play in self.plays:
            body.extend(play.get("tasks") or [])
        removal = next(
            i for i, t in enumerate(body)
            if "ansible.mariadb.mariadb_user" in t
            and t["ansible.mariadb.mariadb_user"].get("state") == "absent"
        )
        recheck = next(i for i, t in enumerate(body) if t.get("register") == "monitor_contract_reads")
        gate = next(i for i, t in enumerate(body) if "monitor_contract_lines" in yaml.safe_dump(t))
        self.assertEqual(
            recheck, removal - 2,
            "ponowny odczyt OBU proxy musi byc ostatnim odczytem przed kasowaniem",
        )
        self.assertEqual(
            gate, removal - 1,
            "miedzy bramka odmowy a kasowaniem nie moze nic wnikac",
        )


class SwitchIsGatedOnMonitorLoginTests(unittest.TestCase):
    """Bramka musi mierzyc logowanie monitora, nie status backendu."""

    @classmethod
    def setUpClass(cls):
        cls.text = SWITCH_PLAY.read_text(encoding="utf-8")

    def test_pair_decides_target_identity_once(self):
        """Regresja zmierzona na green (2026-08-24).

        Przy `serial: 1` kazdy wezel pary liczyl cel osobno, a ProxySQL Cluster
        synchronizowal zmienna miedzy nimi: grp1 poszedl b->a, grp2 zaraz potem
        a->b. Para rozjechala sie, a `contract` skasowal konto uzywane przez
        grp2 (3 bledy logowania monitora). Dokumentacja Ansible mowi wprost, ze
        `run_once` z `serial` uruchamia sie raz NA KAZDA partie, wiec sam
        `run_once` nie wystarcza — `serial` musi zniknac."""
        plays = yaml.safe_load(SWITCH_PLAY.read_text(encoding="utf-8"))
        for play in plays:
            self.assertNotIn(
                "serial", play,
                "serial rozbija decyzje na osobne partie i pozwala parze sie rozjechac",
            )
            tasks = play.get("tasks") or []
            decisive = [
                t for t in tasks
                if any(k in t for k in ("set_fact", "ansible.builtin.set_fact"))
                and "monitor_idle_user" in yaml.safe_dump(t)
            ]
            self.assertTrue(decisive, "brak zadania wyznaczajacego tozsamosc docelowa")
            for task in decisive:
                self.assertTrue(
                    task.get("run_once"),
                    "tozsamosc docelowa musi byc wyliczona raz dla calej pary",
                )

    def test_gate_reads_monitor_connect_log(self):
        self.assertIn("monitor.mysql_server_connect_log", self.text)
        self.assertIn("connect_error", self.text)

    def test_gate_does_not_wait_for_absence_of_shunned(self):
        # Przy max_writers=1 nie-writery w writer hostgroup sa SHUNNED w stanie
        # ustalonym (zmierzone na green: 2 z 3 wezlow). Bramka oparta na tym
        # nigdy by nie przeszla.
        gate = self.text.split("Potwierdz, ze monitor loguje")[-1]
        self.assertNotIn("SHUNNED", gate)

    def test_gate_requires_evidence_from_after_the_switch(self):
        self.assertIn("time_start_us >", self.text)
        self.assertIn("monitor_log_watermark", self.text)

    def test_switch_verifies_every_registered_backend_first(self):
        """Faza expand chodzi po liscie operatora; ProxySQL zna prawdziwa liste.

        Niepelne TENANTS oznaczaloby, ze switch odcina monitoring najemcy,
        ktorego pominieto. Dlatego przed przelaczeniem sprawdzamy logowanie na
        kazdym hoscie z `runtime_mysql_servers`, nie na tym, co podano."""
        before_switch = self.text.split("Przelacz globalna pare monitora")[0]
        self.assertIn("runtime_mysql_servers", before_switch)
        self.assertIn("monitor_registered_backends", before_switch)
        self.assertIn("PRZED switchem", before_switch)

    def test_gate_rejects_monitor_that_is_not_running_at_all(self):
        self.assertIn("COUNT(DISTINCT hostname)", self.text)
        self.assertIn("runtime_mysql_servers", self.text)


# Atrapa `mariadb` (admin-interfejs ProxySQL): odpowiada wg KSZTALTU zapytania
# i tozsamosci peera z environmental FAKE_MONITOR_PEER. Stan scenariusza czyta
# z JSON (FAKE_MONITOR_STATE): {"px1": {"runtime": "<user>", "disk": "<user>"}}.
# Opcjonalnie per peer: "recheck" = linie odpowiedzi dla zapytania UNION
# (ponowny odczyt tuz przed kasowaniem — modeluje zmiane stanu MIEDZY wyborem
# kandydata a kasowaniem), "recheck_fail" = niedostepnosc peera przy ponownym
# odczycie. Domyslnie recheck = [runtime, disk], czyli stan stabilny.
FAKE_MARIADB = r"""#!/usr/bin/env python3
import json
import os
import sys


def main():
    argv = sys.argv[1:]
    sql = ""
    for i, arg in enumerate(argv):
        if arg == "-e" and i + 1 < len(argv):
            sql = argv[i + 1]
    if not sql:
        sys.stderr.write("fake-mariadb: brak zapytania -e\n")
        return 4
    with open(os.environ["FAKE_MONITOR_STATE"], encoding="utf-8") as handle:
        peers = json.load(handle)
    peer = peers[os.environ.get("FAKE_MONITOR_PEER", "px1")]
    upper = sql.upper()
    if "UNION" in upper:
        if peer.get("recheck_fail"):
            sys.stderr.write("fake-mariadb: peer niedostepny\n")
            return 3
        lines = peer.get("recheck") or [peer["runtime"], peer["disk"]]
        sys.stdout.write("".join(line + "\n" for line in lines))
        return 0
    if "RUNTIME_GLOBAL_VARIABLES" in upper:
        sys.stdout.write(peer["runtime"] + "\n")
        return 0
    if "GLOBAL_VARIABLES" in upper:
        sys.stdout.write(peer["disk"] + "\n")
        return 0
    sys.stderr.write("fake-mariadb: nieznane zapytanie: " + sql + "\n")
    return 4


if __name__ == "__main__":
    sys.exit(main())
"""

INVENTORY = {
    "all": {
        "vars": {
            "ansible_connection": "local",
            "ansible_become": False,
            "ansible_python_interpreter": sys.executable,
        },
        "children": {
            "proxysql": {"hosts": {"px1": None, "px2": None}},
            "galera": {"hosts": {"db1": {"galera_node_address": "127.0.0.1"}}},
        },
    }
}

# Jedna modyfikacja ciala w kopii testowej: zadania ansible.mariadb.mariadb_user
# (wymagaja zywego serwera) sa zastapione znacznikiem zapisujacym TOZSAMOSC
# przekazana do kasowania/zakladania. Wszystkie bramy zostaja oryginalne.
READ_REGISTERS = ("monitor_runtime_reads", "monitor_disk_reads", "monitor_contract_reads")


def deep_replace(value, old, new):
    if isinstance(value, dict):
        return {deep_replace(k, old, new): deep_replace(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [deep_replace(item, old, new) for item in value]
    if isinstance(value, str):
        return value.replace(old, new)
    return value


def build_fixture(base):
    bin_dir = base / "bin"
    bin_dir.mkdir(parents=True)

    fake = bin_dir / "mariadb"
    fake.write_text(FAKE_MARIADB, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    identity = base / "admin-check.cnf"
    identity.write_text("[client]\nuser=admin\n", encoding="utf-8")

    plays = deep_replace(
        yaml.safe_load(TENANT_PLAY.read_text(encoding="utf-8")), IDENTITY_REAL, str(identity)
    )
    for play in plays:
        for section in ("pre_tasks", "tasks"):
            for task in play.get(section) or []:
                if task.get("register") in READ_REGISTERS:
                    task["environment"] = {"FAKE_MONITOR_PEER": "{{ item }}"}
                if "ansible.mariadb.mariadb_user" in task:
                    spec = task.pop("ansible.mariadb.mariadb_user")
                    action = "expand-create" if spec.get("state") == "present" else "contract-delete"
                    task.pop("no_log", None)
                    task["ansible.builtin.copy"] = {
                        "content": action + " {{ monitor_idle_user }}\n",
                        "dest": "{{ lookup('ansible.builtin.env', 'SMOKE_MARKER') }}",
                        "mode": "0644",
                    }
    play_path = base / "monitor_rotate.fixture.yml"
    play_path.write_text(yaml.safe_dump(plays, sort_keys=False), encoding="utf-8")

    inventory = base / "inventory.yml"
    inventory.write_text(yaml.safe_dump(INVENTORY, sort_keys=False), encoding="utf-8")
    cfg = base / "ansible.cfg"
    cfg.write_text(
        "[defaults]\nretry_files_enabled = False\ninterpreter_python = auto_silent\n",
        encoding="utf-8",
    )
    return {
        "base": base,
        "bin": bin_dir,
        "cfg": cfg,
        "inventory": inventory,
        "play": play_path,
        "marker": base / "marker.txt",
    }


def tail(text, lines=40):
    return "\n".join(text.splitlines()[-lines:])


# Stan zgody: oba wezly, obie warstwy wskazuja B (po switchu) — stara A kandydatem.
B_AGREED = {
    "px1": {"runtime": USER_B, "disk": USER_B},
    "px2": {"runtime": USER_B, "disk": USER_B},
}


@unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostepny w PATH")
class ContractGateSmokeTests(unittest.TestCase):
    """Prawdziwy ansible-playbook na kopii fazy contract z atrapa mariadb.

    Marker = jedyny dowod „kasowanie dopuszczone”; kazdy scenariusz negatywny
    konczy sie brakiem markera (bramka przerwala przebieg przed kasowaniem)
    i komunikatem wlasnej bramki, nie stacktrace'em.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="rotate-smoke-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.fx = build_fixture(Path(cls._tmp.name))

    def run_case(self, peers, phase="contract"):
        fx = self.fx
        state = fx["base"] / "state.json"
        state.write_text(json.dumps(peers), encoding="utf-8")
        if fx["marker"].exists():
            fx["marker"].unlink()
        env = os.environ.copy()
        env.update(
            {
                "PATH": str(fx["bin"]) + os.pathsep + env.get("PATH", ""),
                "FAKE_MONITOR_STATE": str(state),
                "SMOKE_MARKER": str(fx["marker"]),
                "PROXYSQL_MONITOR_PASSWORD_NEXT": "smoke-next-password",
                "ANSIBLE_CONFIG": str(fx["cfg"]),
            }
        )
        return subprocess.run(
            [
                ANSIBLE_PLAYBOOK,
                "-i", str(fx["inventory"]),
                str(fx["play"]),
                "-e", "rotation_phase=" + phase,
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(fx["base"]),
            timeout=240,
        )

    def assert_deletes(self, proc, identity):
        self.assertEqual(proc.returncode, 0, tail(proc.stdout + proc.stderr))
        self.assertTrue(self.fx["marker"].exists(), tail(proc.stdout))
        self.assertEqual(
            self.fx["marker"].read_text(encoding="utf-8"),
            f"contract-delete {identity}\n",
            "kasowana jest wylacznie tozsamosc bezczynna (poprzednia)",
        )

    def assert_blocked(self, proc, fragment):
        self.assertNotEqual(proc.returncode, 0, tail(proc.stdout + proc.stderr))
        self.assertFalse(self.fx["marker"].exists(), "marker za bramka po odmowie")
        self.assertIn(fragment, proc.stdout)

    def test_agreed_pair_on_b_deletes_old_a(self):
        """Oba proxy w obu warstwach potwierdzaja B => stara A nadaje sie do usuniecia."""
        self.assert_deletes(self.run_case(B_AGREED), USER_A)

    def test_peer_split_blocks_deletion(self):
        """px2 juz wrócil na A (runtime), px1 zostal na B — zadne kasowanie."""
        peers = copy.deepcopy(B_AGREED)
        peers["px2"]["runtime"] = USER_A
        self.assert_blocked(
            self.run_case(peers), "nie potwierdza jednej znanej tozsamosci"
        )

    def test_stale_disk_identity_blocks_deletion(self):
        """Runtime przelaczony, disk nie — restart wezla wskrzesilby stare konto."""
        peers = {
            node: {"runtime": USER_B, "disk": USER_A}
            for node in ("px1", "px2")
        }
        self.assert_blocked(
            self.run_case(peers), "nie potwierdza jednej znanej tozsamosci"
        )

    def test_unknown_identity_blocks_deletion(self):
        peers = {
            node: {"runtime": "proxysql_monitor_x", "disk": "proxysql_monitor_x"}
            for node in ("px1", "px2")
        }
        self.assert_blocked(
            self.run_case(peers), "nie potwierdza jednej znanej tozsamosci"
        )

    def test_unavailable_peer_at_recheck_blocks_deletion(self):
        """Para zgodna przy wyborze, ale peer pada przy ponownym odczycie."""
        peers = copy.deepcopy(B_AGREED)
        peers["px2"]["recheck_fail"] = True
        self.assert_blocked(self.run_case(peers), "NIC nie skasowano")

    def test_candidate_taken_between_selection_and_removal_blocks(self):
        """Wyscig: kandydat w uzyciu na jednym proxy w momencie ponownego odczytu."""
        peers = copy.deepcopy(B_AGREED)
        peers["px1"]["recheck"] = [USER_A, USER_B]
        self.assert_blocked(self.run_case(peers), "NIC nie skasowano")

    def test_truncated_recheck_response_blocks_deletion(self):
        """Odpowiedz ucieta (jedna warstwa zamiast dwoch) nie przejdzie."""
        peers = copy.deepcopy(B_AGREED)
        peers["px1"]["recheck"] = [USER_B]
        self.assert_blocked(self.run_case(peers), "NIC nie skasowano")


if __name__ == "__main__":
    unittest.main()
