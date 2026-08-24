"""Zawieszony SST ma wlasciciela deadline'u, a nie wisi w nieskonczonosc.

DEFEKT, KTORY TO ZAMYKA. `f5_join.yml` instalowal drop-in
`TimeoutStartSec=infinity`, a nastepnie startowal jednostke BLOKUJACO
(`ansible.builtin.systemd: state=started`). Wlasny bounded wait
(`until wsrep_local_state == 4`, retries/delay) stal ZA tym zadaniem, wiec przy
zawieszonym SST nigdy do niego nie docieral: `systemctl start` nie wracal,
bo dla `Type=notify` start konczy sie dopiero na `READY=1`, a `infinity`
wylacza logike timeoutu. Playbook wisial bez limitu i bez diagnostyki.

USTALENIA Z DOKUMENTACJI (nie z pomiaru):
* systemd, man 5 systemd.service, `TimeoutStartSec=`: "Configures the time to
  wait for start-up. (...) Pass 'infinity' to disable the timeout logic."
  Dla `Type=notify` start jest ukonczony, gdy uslugowy proces wysle "READY=1".
* Ansible, `ansible.builtin.systemd_service`, parametr `no_block`: "Do not
  synchronously wait for the requested operation to finish. Enqueued job will
  continue without Ansible blocking on its completion."
* Ansible, playbooks_loops, `until`: "The normal use case for `until` has to do
  with tasks that are likely to fail" — zadanie jest ponawiane mimo bledu
  i zglasza porazke dopiero po wyczerpaniu `retries`. Dlatego po odblokowaniu
  startu pierwsze proby (brak socketu) sa poprawnym stanem przejsciowym.

DLACZEGO `infinity` ZOSTAJE. MariaDB dokumentuje, ze od systemd 236 dziala
`EXTEND_TIMEOUT_USEC=` i "manual override of TimeoutStartSec is often
unnecessary" (starting-and-stopping-mariadb/systemd). Flota spelnia ten prog
(systemd 252, mariadbd 11.4.12). Mimo to zostawiamy `infinity`: skonczony
timeout oznaczalby, ze systemd ubija mariadbd w trakcie transferu danych, gdyby
przedluzanie z jakiegokolwiek powodu nie zadzialalo. Deadline nalezy do
playbooka, ktory potrafi zebrac diagnostyke; systemd ma nie zabijac SST.
"""

import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
JOIN = REPO / "playbooks" / "f5_join.yml"


def _flatten(tasks):
    """Zadania z blokow block/rescue/always, w kolejnosci wystepowania."""
    out = []
    for task in tasks or []:
        out.append(task)
        for key in ("block", "rescue", "always"):
            if isinstance(task.get(key), list):
                out.extend(_flatten(task[key]))
    return out


class JoinDeadlineOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        plays = yaml.safe_load(JOIN.read_text(encoding="utf-8"))
        cls.tasks = []
        for play in plays:
            cls.tasks.extend(_flatten(play.get("tasks")))
        cls.text = JOIN.read_text(encoding="utf-8")

    def _systemd_start_tasks(self):
        found = []
        for task in self.tasks:
            for key in ("ansible.builtin.systemd", "ansible.builtin.systemd_service", "systemd"):
                spec = task.get(key)
                if isinstance(spec, dict) and spec.get("state") == "started":
                    found.append((task, spec))
        return found

    def test_start_of_joining_node_does_not_block(self):
        starts = self._systemd_start_tasks()
        self.assertTrue(starts, "brak zadania startujacego mariadb w f5_join.yml")
        for task, spec in starts:
            self.assertTrue(
                spec.get("no_block"),
                f"zadanie {task.get('name')!r} startuje jednostke blokujaco; "
                "przy TimeoutStartSec=infinity zawieszony SST nigdy nie oddaje sterowania "
                "do bounded waita ponizej",
            )

    def test_bounded_wait_runs_after_start_and_has_finite_deadline(self):
        names = [t.get("name", "") for t in self.tasks]
        start_idx = next(
            i for i, t in enumerate(self.tasks)
            if any(
                isinstance(t.get(k), dict) and t[k].get("state") == "started"
                for k in ("ansible.builtin.systemd", "ansible.builtin.systemd_service", "systemd")
            )
        )
        wait = [(i, t) for i, t in enumerate(self.tasks) if t.get("until")]
        self.assertTrue(wait, "brak bounded waita na stan Synced")
        wait_idx, wait_task = wait[0]
        self.assertGreater(
            wait_idx, start_idx,
            f"bounded wait musi stac PO starcie; kolejnosc zadan: {names}",
        )
        retries = wait_task.get("retries")
        self.assertIsNotNone(retries, "bounded wait bez `retries` to brak deadline'u")
        self.assertNotIn(
            str(retries).lower(), ("infinity", "-1", "0"),
            "deadline musi byc skonczony",
        )

    def test_exceeded_deadline_produces_diagnostics(self):
        """Sam blad 'until nie spelnione' nie mowi, CZY i CZEMU SST utknal."""
        rescue_tasks = []
        for play in yaml.safe_load(self.text):
            for task in play.get("tasks") or []:
                if isinstance(task.get("rescue"), list):
                    rescue_tasks.extend(_flatten(task["rescue"]))
        self.assertTrue(
            rescue_tasks,
            "przekroczony deadline musi zebrac diagnostyke, nie tylko zwrocic blad",
        )
        blob = yaml.safe_dump(rescue_tasks)
        for probe in ("systemctl", "journalctl", "wsrep_local_state"):
            self.assertIn(
                probe, blob,
                f"diagnostyka po przekroczeniu okna SST nie zbiera {probe!r}",
            )


class SystemdMustNotKillTransferTests(unittest.TestCase):
    def test_dropin_keeps_timeout_disabled_for_systemd(self):
        """Skonczony TimeoutStartSec = systemd ubija mariadbd w polowie transferu."""
        text = JOIN.read_text(encoding="utf-8")
        self.assertIn(
            "TimeoutStartSec=infinity", text,
            "deadline nalezy do playbooka; systemd nie moze przerywac SST w trakcie",
        )


if __name__ == "__main__":
    unittest.main()
