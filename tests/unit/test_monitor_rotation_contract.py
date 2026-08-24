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

Przestawienie tych krokow przywraca dokladnie ten defekt, wiec kolejnosc jest
kontacktem testowanym, nie konwencja w komentarzu.
"""

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
MAKEFILE = REPO / "Makefile"
TENANT_PLAY = REPO / "playbooks" / "monitor_rotate.yml"
SWITCH_PLAY = REPO / "playbooks" / "platform_monitor_switch.yml"


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


class NeverDropTheActiveIdentityTests(unittest.TestCase):
    """Najgrozniejszy blad tej procedury: skasowac konto, ktorego ProxySQL uzywa."""

    @classmethod
    def setUpClass(cls):
        cls.plays = yaml.safe_load(TENANT_PLAY.read_text(encoding="utf-8"))
        cls.tasks = []
        for play in cls.plays:
            cls.tasks.extend(play.get("pre_tasks") or [])
            cls.tasks.extend(play.get("tasks") or [])

    def _mysql_user_tasks(self):
        return [t for t in self.tasks if "ansible.mysql.mysql_user" in t]

    def test_absent_task_targets_the_idle_identity_only(self):
        removals = [
            t for t in self._mysql_user_tasks()
            if t["ansible.mysql.mysql_user"].get("state") == "absent"
        ]
        self.assertTrue(removals, "faza contract musi usuwac konto")
        for task in removals:
            name = task["ansible.mysql.mysql_user"]["name"]
            self.assertIn(
                "monitor_idle_user", name,
                "kasujemy WYLACZNIE tozsamosc bezczynna; uzycie 'active' albo "
                "'current'/'target' juz raz doprowadzilo do skasowania konta uzywanego",
            )
            self.assertNotIn("monitor_active_user", name)

    def test_creation_targets_the_idle_identity(self):
        creations = [
            t for t in self._mysql_user_tasks()
            if t["ansible.mysql.mysql_user"].get("state") == "present"
        ]
        self.assertTrue(creations)
        for task in creations:
            self.assertIn("monitor_idle_user", task["ansible.mysql.mysql_user"]["name"])

    def test_contract_asserts_identities_differ_before_dropping(self):
        blob = yaml.safe_dump(self.tasks, allow_unicode=True)
        self.assertIn("monitor_idle_user != monitor_active_user", blob)

    def test_unknown_identity_aborts_rotation(self):
        blob = yaml.safe_dump(self.tasks, allow_unicode=True)
        self.assertIn(
            "monitor_active_user in [monitor_user_a, monitor_user_b]", blob,
            "konto spoza pary rotacyjnej nie moze zostac skasowane",
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


if __name__ == "__main__":
    unittest.main()
