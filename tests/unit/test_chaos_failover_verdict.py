#!/usr/bin/env python3
"""Regresje dla tests/lab/chaos-failover.py — gdy sonda klamie zielenia.

Sonda obiecuje trzy rzeczy, ktorych wczesniej NIE dowodzila, i kazda z nich
konczyla sie falszywym PASS (exit 0):

1. Odtworzenie klastra. Po zabiciu wezla `finally` czekal na `Synced`, ale gdy
   wezel nie wracal, wypisywal tylko `UWAGA:` i zwracal PASS — zdegradowany
   klaster szedl do kolejnych sond jako zielony wynik, mimo ze sonda zobowiazuje
   sie oddac klaster w stanie, w jakim go zastala.
2. Tryb `hard`. Wynik async-uruchomienia ansible (sysrq) byl ignorowany, wiec
   ODRZUCONE zadanie — czyli brak awarii — nadal przechodzilo, dopoki workload
   dalej commitowal. Test nie testowal awarii.
3. Odczyt logu transakcji. Nieudany odczyt (rc != 0) zamienial sie w „zero
   transakcji", a asercja ISC-28 na pustym zbiorze przechodzila WAKUATYJNIE.

Testy uruchamiaja `main()` z podstawionymi globalnymi modulu: atrapa `sh`,
atrapa `subprocess`, wirtualny zegar i zaslepione `active_writer_ip` /
`galera_ip_to_host` / `present_seqs_on`. Zaden prawdziwy proces nie ma prawa
powstac — globalne `subprocess.run`/`Popen` sa podmienione na wyjatek, wiec
kazda proba ucieczki z atrap konczy test bledem, a nie ruchem w labie.
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT_PATH = WORKSPACE / "tests" / "lab" / "chaos-failover.py"

# Start wirtualnego zegara; sonda przez 10 s przed zabiciem tylko spi, wiec
# kill_ts wypada dokladnie w CLOCK_START + 10.
CLOCK_START = 1000.0

CLUSTER_YML = {
    "cluster": {"name": "test-cluster", "environment": "laboratory"},
    "galera": {"nodes_expected": 3},
    "proxysql": {
        "hostgroup_base": 10,
        "app_user": "app_user_test",
        "endpoint": {"type": "keepalived_vip", "address": "192.0.2.200", "port": 6033},
    },
    "availability": {"rto_node_failure": "6"},
}

INVENTORY_YML = {
    "all": {
        "children": {
            "galera": {
                "hosts": {
                    "gnode1": {"ansible_host": "192.0.2.11", "galera_node_address": "192.0.2.11"},
                    "gnode2": {"ansible_host": "192.0.2.12", "galera_node_address": "192.0.2.12"},
                    "gnode3": {"ansible_host": "192.0.2.13", "galera_node_address": "192.0.2.13"},
                }
            },
            "proxysql": {
                "hosts": {
                    "pnode1": {"ansible_host": "192.0.2.21", "proxysql_node_address": "192.0.2.21"},
                    "pnode2": {"ansible_host": "192.0.2.22", "proxysql_node_address": "192.0.2.22"},
                }
            },
        }
    }
}

WORKLOAD_HOST = "gnode1"
WRITER_IP = "192.0.2.11"


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _VirtualClock:
    """Zegar sterowany snem: kazda przespana sekunda to commit workloadu.

    Dzieki temu okna czasowe sondy (RTO, karencja, powrot wezla) sa przeliczane
    natychmiast, a test mierzy logike werdyktu, nie rzeczywisty uplyw czasu.
    """

    def __init__(self, state):
        self.state = state
        self.now = CLOCK_START

    def time(self):
        return self.now

    def sleep(self, seconds):
        target = self.now + seconds
        seq = self.state["seq"]
        if self.state["running"]:
            moment = float(int(self.now) + 1)
            while moment <= target:
                seq += 1
                self.state["commits"].append((moment, seq))
                moment += 1.0
        self.state["seq"] = seq
        self.now = target


class _FakeSubprocess:
    """Atrapa modulu `subprocess`; zapisuje kazde wywolanie do stanu testu."""

    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, state):
        self.state = state

    def run(self, cmd, **kwargs):
        self.state["commands"].append(list(cmd))
        return self.state["run_hook"](cmd, kwargs)


def _make_sh(state):
    """Atrapa `sh(node, script)`: brak jakiegokolwiek procesu poza testem."""

    def fake_sh(node, script, timeout=60, check=False):
        state["sh"].append((node, script))
        rc, out = 0, ""
        if script.startswith("cat "):
            # Blad chwilowy w petli RTO jest OCZEKIWANY (host obciazenia moze byc
            # tym, ktory wlasnie zabijamy) — musi pozostac tolerancyjny. Blad
            # trwaly ustawia sie dopiero po zatrzymaniu workloadu.
            if state["transient_pending"] and state["killed"]:
                state["transient_pending"] = False
                rc = 1
            elif state["log_fail"]:
                rc, out = 1, ""
            else:
                out = "\n".join(f"{t:.0f} {s}" for t, s in state["commits"])
        elif "nohup bash" in script:
            state["running"] = True
            out = "launched"
        elif script == "rm -f /tmp/workload.run":
            state["running"] = False
            state["log_fail"] = state["fail_final_log"]
        elif "pkill -9 -x mariadbd" in script:
            state["killed"] = True
            if state["transient_fail_after_kill"]:
                state["transient_pending"] = True
            out = "killed"
        elif "wsrep_local_state_comment" in script:
            out = state["state_comment"]
        elif "test -w /proc/sysrq-trigger" in script:
            out = "yes"
        result = _Completed(rc, out)
        if check and rc != 0:
            raise RuntimeError(f"ansible {node} failed: {out}")
        return result

    return fake_sh


class ChaosFailoverVerdictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.cfg_file = root / "cluster.yml"
        cls.inv_file = root / "inventory.yml"
        cls.cfg_file.write_text(yaml.safe_dump(CLUSTER_YML), encoding="utf-8")
        cls.inv_file.write_text(yaml.safe_dump(INVENTORY_YML), encoding="utf-8")

        spec = importlib.util.spec_from_file_location("chaos_failover_under_test", SCRIPT_PATH)
        with mock.patch.dict(
            os.environ,
            {
                "CLUSTER_CONFIG": str(cls.cfg_file),
                "CLUSTER_INVENTORY": str(cls.inv_file),
            },
            clear=False,
        ):
            cls.mod = importlib.util.module_from_spec(spec)
            # run_name != "__main__" => modul nie odpala main() przy imporcie.
            spec.loader.exec_module(cls.mod)

    _PATCHED_GLOBALS = (
        "FAILOVER_MODE", "time", "sh", "subprocess", "active_writer_ip",
        "galera_ip_to_host", "present_seqs_on", "WORKLOAD_HOST", "ENVIRONMENT",
        "APP_PW", "RTO_SECONDS", "HARD_DOWN_TIMEOUT",
    )

    def setUp(self):
        # Sonda sprzed poprawki nie ma jeszcze czesci tych globalnych (np.
        # HARD_DOWN_TIMEOUT), a test ma padac na ZACHOWANIU, nie na AttributeError.
        missing = object()
        self._original = {
            name: getattr(self.mod, name, missing) for name in self._PATCHED_GLOBALS
        }
        self._missing = missing
        self.addCleanup(self._restore)

        # Zaden prawdziwy proces nie moze powstac: ucieczka z atrap to blad testu.
        for name in ("run", "Popen", "check_output", "check_call", "call"):
            patcher = mock.patch(
                "subprocess." + name,
                side_effect=AssertionError(f"real subprocess.{name} must never be invoked"),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore(self):
        for name, value in self._original.items():
            if value is self._missing:
                delattr(self.mod, name)
                continue
            setattr(self.mod, name, value)

    def _run_probe(self, mode="soft", **overrides):
        state = {
            "commits": [],
            "seq": 0,
            "running": False,
            "killed": False,
            "log_fail": False,
            "fail_final_log": False,
            "transient_pending": False,
            "transient_fail_after_kill": False,
            "state_comment": "Synced",
            "ping_stdout": "SUCCESS",          # ofiara odpowiada => nie zginela
            "kill_launch_rc": 0,
            "sh": [],
            "commands": [],
        }
        state.update(overrides)

        def run_hook(cmd, kwargs):
            if "-m" in cmd and "ansible.builtin.ping" in cmd:
                out = state["ping_stdout"]
                return _Completed(0 if "SUCCESS" in out else 2, out)
            if "-B" in cmd:  # async sysrq launch
                rc = state["kill_launch_rc"]
                return _Completed(rc, "" if rc == 0 else "unsupported parameter")
            return _Completed(0, "")

        state["run_hook"] = run_hook
        clock = _VirtualClock(state)

        self.mod.FAILOVER_MODE = mode
        self.mod.time = clock
        self.mod.sh = _make_sh(state)
        self.mod.subprocess = _FakeSubprocess(state)
        self.mod.WORKLOAD_HOST = WORKLOAD_HOST
        self.mod.active_writer_ip = lambda: WRITER_IP
        self.mod.galera_ip_to_host = lambda: {WRITER_IP: WORKLOAD_HOST}
        self.mod.present_seqs_on = lambda host: set(range(1, 100000))
        self.mod.ENVIRONMENT = "laboratory"
        self.mod.APP_PW = "test-password"
        self.mod.RTO_SECONDS = 6
        self.mod.HARD_DOWN_TIMEOUT = 20

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.mod.main()
        return rc, buf.getvalue(), state

    # --- Bramka 1: wezel musi wrocic do klastra ---

    def test_node_never_synced_fails_the_run(self):
        # Defekt: `finally` tylko PRINTS `UWAGA:` i oddaje PASS, wiec zdegradowany
        # klaster jest raportowany jako zielony wynik kolejnych sond.
        rc, out, state = self._run_probe(mode="soft", state_comment="Donor/Desynced")

        self.assertEqual(rc, 1, out)
        self.assertIn("did not return to Synced", out)
        self.assertIn(WORKLOAD_HOST, out)
        self.assertNotIn("PASS: failover survived", out)
        # Bramka dziala na OBSERWACJI: sonda pytala o stan az do wyczerpania okna.
        polls = [s for _, s in state["sh"] if "wsrep_local_state_comment" in s]
        self.assertGreater(len(polls), 1, "node state was never polled for recovery")

    # --- Bramka 2: tryb hard musi NAPRAWDE ubic maszyne ---

    def test_hard_mode_rejected_kill_fails_even_when_workload_keeps_committing(self):
        # Defekt: wynik async-uruchomienia sysrq byl ignorowany, wiec odrzucone
        # zadanie (maszyna zyje) przechodzilo jako PASS, o ile workload commitowal.
        rc, out, state = self._run_probe(
            mode="hard",
            kill_launch_rc=1,
            ping_stdout=f"{WORKLOAD_HOST} | SUCCESS => {{\"ping\": \"pong\"}}",
        )

        self.assertEqual(rc, 1, out)
        self.assertIn("did not take effect", out)
        self.assertIn(WORKLOAD_HOST, out)
        # Workload commitowal dalej PO rozkazie zabicia (failoveru nie bylo), wiec
        # czerwien nie moze wynikac z braku ruchu — jej powodem jest brak awarii.
        self.assertTrue(
            any(t > CLOCK_START + 10.5 for t, _ in state["commits"]),
            "workload did not keep committing past the rejected kill",
        )
        self.assertNotIn("PASS: failover survived", out)
        # Sonda naprawde czekala na znikniecie ofiary, a nie tylko wyslala rozkaz.
        pings = [c for c in state["commands"] if "ansible.builtin.ping" in c]
        self.assertGreater(len(pings), 1, "victim reachability was never polled")

    def test_hard_mode_machine_that_really_goes_down_passes(self):
        # Bramka nie moze byc slepa na udana awarie: maszyna przestaje odpowiadac
        # i przebieg jest zielony, mimo odrzuconego startu async (TimeoutExpired
        # jest naturalny, gdy ofiara znika w trakcie wykonania).
        rc, out, state = self._run_probe(
            mode="hard",
            kill_launch_rc=0,
            ping_stdout=f"{WORKLOAD_HOST} | UNREACHABLE! => {{\"unreachable\": true}}",
        )

        self.assertEqual(rc, 0, out)
        self.assertIn("PASS: failover survived", out)
        self.assertIn("unreachable", out)

    # --- Bramka 3: nieudany odczyt logu to nieudany POMIAR ---

    def test_failed_final_log_read_fails_and_never_claims_zero_transactions(self):
        # Defekt: rc odczytu byl ignorowany, wiec nieudany odczyt zamienial sie w
        # „zero transakcji", a ISC-28 na pustym zbiorze przechodzila wakatyjnie.
        rc, out, state = self._run_probe(mode="soft", fail_final_log=True)

        self.assertEqual(rc, 1, out)
        self.assertIn("committed-transaction log", out)
        self.assertNotIn("assertion not executable", out)
        self.assertNotIn("0 lost", out)
        self.assertNotIn("PASS: failover survived", out)

    def test_transient_in_loop_log_failure_does_not_abort_the_run(self):
        # Odczyt w petli RTO MUSI zostac tolerancyjny: host obciazenia moze byc
        # tym samym wezlem, ktory wlasnie zabijamy, wiec chwilowy blad jest
        # oczekiwany i nie moze wywracac przebiegu.
        rc, out, state = self._run_probe(mode="soft", transient_fail_after_kill=True)

        self.assertEqual(rc, 0, out)
        self.assertIn("PASS: failover survived", out)
        self.assertTrue(state["transient_pending"] is False, "transient failure never injected")

    # --- Zabezpieczenie przed nadmiarowa surowoscia ---

    def test_healthy_run_still_passes(self):
        rc, out, state = self._run_probe(mode="soft")

        self.assertEqual(rc, 0, out)
        self.assertIn("PASS: failover survived", out)
        self.assertIn("0 lost", out)
        # Kazde wywolanie to atrapa ansible; zaden inny program nie zostal uruchomiony.
        self.assertTrue(state["commands"], "probe never invoked the (fake) ansible")
        self.assertTrue(
            all(cmd[0] == self.mod.ANSIBLE for cmd in state["commands"]),
            f"unexpected commands: {state['commands']}",
        )


if __name__ == "__main__":
    unittest.main()
