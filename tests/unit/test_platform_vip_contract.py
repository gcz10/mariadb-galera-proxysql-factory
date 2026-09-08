#!/usr/bin/env python3
"""Sonda warstwy i bramka Keepalived muszą opisywać ten sam stan tak samo.

`check_proxysql.sh` (ISC-26) rozróżnia dwa przypadki: zero aktywnych grup Galera
to świeża warstwa bez najemców — VIP zostaje; aktywne grupy bez writera ONLINE
to stan, w którym adres jest ŚWIADOMIE zdejmowany, bo nie ma dokąd kierować
ruchu. `probe-platform.py` żądała VIP-a bezwarunkowo, więc ten sam stan
opisywała jako naruszenie: na warstwie z zatrzymanymi najemcami
`platform-verify` padał zawsze i przestawał cokolwiek znaczyć (zmierzone
2026-09-05 na xenonv11).

Testowana jest decyzja, nie tekst komunikatu: kiedy brak VIP-a jest awarią,
a kiedy pomiarem niemożliwym do rozstrzygnięcia.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "tests" / "lab" / "probe-platform.py"
GATE = REPO / "roles" / "proxysql_endpoint" / "files" / "check_proxysql.sh"

# Sondy importuja `_probe_common` po sasiedzku, tak jak przy uruchomieniu
# z `tests/lab/` — bez tego modul nie da sie zaladowac w tescie.
sys.path.insert(0, str(PROBE.parent))
_spec = importlib.util.spec_from_file_location("probe_platform_under_test", PROBE)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

HOSTS = ["p1", "p2"]


def state(groups: int, writers: int, hosts=HOSTS) -> dict:
    # Obie instancje pary widzą TĘ SAMĄ konfigurację runtime i raportują
    # identyczne liczby — fixture musi to odwzorowywać, inaczej maskuje błąd
    # agregacji (sumowanie podwajałoby obie wartości).
    return {host: {"GROUPS": str(groups), "WRITERS": str(writers)} for host in hosts}


def install_timeout_shim(workdir: Path) -> None:
    """`timeout` (coreutils) nie istnieje na macOS, a bramka mierzy nim port.

    Bez niego skrypt konczy sie na kroku 1 NIEZALEZNIE od stanu pary, wiec
    porownanie werdyktow nie mierzyloby niczego. Zastepnik odrzuca sam limit
    czasu i uruchamia polecenie — reszta bramki wykonuje sie bez zmian.
    """
    if shutil.which("timeout"):
        return
    shim = workdir / "timeout"
    shim.write_text('#!/bin/sh\nshift\nexec "$@"\n', encoding="utf-8")
    shim.chmod(0o755)



class PlatformVipContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Bramka najpierw sprawdza port klienta 6033; bez nasluchu konczy sie
        # na kroku 1 i nigdy nie doszlaby do liczenia writerow.
        cls._listener = socket.socket()
        cls._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cls._listener.bind(("127.0.0.1", 6033))
        cls._listener.listen(8)
        cls.addClassCleanup(cls._listener.close)

    def run_gate(self, groups: int, writers: int, admin_fails: bool = False) -> int:
        """Uruchamia PRAWDZIWY skrypt bramki z klientem zwracajacym zadany stan."""
        workdir = Path(tempfile.mkdtemp(prefix="vip-gate-"))
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        (workdir / "admin-check.cnf").write_text("[client]\n", encoding="utf-8")
        client = workdir / "mariadb"
        client.write_text(
            "#!/bin/sh\n"
            + ("exit 1\n" if admin_fails else f"printf '{groups}\\t{writers}\\n'\n"),
            encoding="utf-8",
        )
        client.chmod(0o755)
        install_timeout_shim(workdir)
        return subprocess.run(
            ["bash", str(GATE)],
            env={
                **os.environ,
                "PATH": f"{workdir}:{os.environ['PATH']}",
                "PROXYSQL_ADMIN_CNF": str(workdir / "admin-check.cnf"),
            },
            capture_output=True,
        ).returncode

    def test_stopped_tenants_make_the_missing_vip_unmeasurable(self):
        """Aktywne grupy + zero writerów = bramka zdjęła adres celowo."""
        self.assertTrue(probe.withdrawal_is_expected(state(groups=4, writers=0), HOSTS))

    def test_live_writer_keeps_the_vip_mandatory(self):
        """Jest komu obsłużyć ruch — brak VIP-a pozostaje awarią."""
        self.assertFalse(probe.withdrawal_is_expected(state(groups=4, writers=1), HOSTS))

    def test_unreadable_counter_does_not_excuse_a_missing_vip(self):
        """Śmieć po błędzie klienta liczy się jako 0 — w stronę rygoru.

        Gdyby nieczytelny odczyt szedł w stronę pobłażliwości, awaria klienta
        admina uciszałaby bramkę VIP-a dokładnie wtedy, gdy nic nie wiadomo.
        """
        garbage = {host: {"GROUPS": "ERROR 2002", "WRITERS": ""} for host in HOSTS}
        self.assertFalse(probe.withdrawal_is_expected(garbage, HOSTS))

    def test_pair_disagreement_keeps_the_vip_required(self):
        """Jedna instancja widzi writera, druga nie — VIP zostaje wymagany.

        Rozjazd w parze jest sam w sobie anomalią, ale nie wolno mu ZŁAGODZIĆ
        bramki: dopóki ktokolwiek widzi writera ONLINE, jest komu obsłużyć ruch,
        więc brak adresu pozostaje awarią. Odwrotna reguła (dowolny węzeł
        raportujący zero rozgrzesza brak VIP-a) zamieniłaby błąd odczytu
        na ciszę.
        """
        divergent = {
            "p1": {"GROUPS": "4", "WRITERS": "1"},
            "p2": {"GROUPS": "4", "WRITERS": "0"},
        }
        self.assertFalse(probe.withdrawal_is_expected(divergent, HOSTS))

    def test_layer_without_tenants_still_must_hold_the_vip(self):
        """Zero grup to przypadek (a) bramki: adres ma być trzymany.

        Regresja z 2026-08-21: świeża para nie mogła wstać, bo bramka myliła
        „zero najemców" z „najemca bez writera".
        """
        self.assertFalse(probe.withdrawal_is_expected(state(groups=0, writers=0), HOSTS))

    def test_incomplete_pair_reading_grants_no_leniency(self):
        """Brakujący host mógł być właśnie tym, który trzymał adres."""
        partial = {"p1": {"GROUPS": "4", "WRITERS": "0"}}
        self.assertFalse(probe.withdrawal_is_expected(partial, HOSTS))
        self.assertFalse(probe.withdrawal_is_expected({}, HOSTS))

    def test_probe_and_gate_reach_the_same_verdict_on_the_same_state(self):
        """Rozstrzyga WYKONANIE obu bramek, nie podobienstwo ich tekstu.

        Poprzednia wersja tego testu szukala fragmentow SQL w obu plikach —
        przechodzila takze wtedy, gdy sonda i bramka liczyly rozne rzeczy tymi
        samymi slowami. Tutaj bramka Keepalived dostaje prawdziwe dane przez
        klienta zwracajacego stan pary, a sonda te same liczby: brak VIP-a jest
        niemierzalny DOKLADNIE wtedy, gdy bramka adres zdjela.
        """
        for groups, writers in ((0, 0), (2, 0), (2, 1), (2, 2)):
            with self.subTest(groups=groups, writers=writers):
                gate_withdraws = self.run_gate(groups, writers) != 0
                self.assertEqual(
                    gate_withdraws,
                    probe.withdrawal_is_expected(state(groups, writers), HOSTS),
                    "sonda i bramka rozstrzygaja ten sam stan inaczej",
                )

    def test_gate_withdraws_the_vip_when_the_admin_interface_fails(self):
        """Nieczytelny stan pary = brak dowodu, ze jest komu obsluzyc ruch."""
        self.assertNotEqual(self.run_gate(2, 2, admin_fails=True), 0)


if __name__ == "__main__":
    unittest.main()
