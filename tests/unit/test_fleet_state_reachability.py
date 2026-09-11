#!/usr/bin/env python3
"""`fleet-state` nie moze oglaszac awarii floty, gdy to ON nie ma trasy.

Zmierzony przypadek (2026-09-10): VIP `192.168.1.74:6033` byl zdrowy — sonda
aplikacyjna chodzila przez niego z hosta `app` — a raport floty pisal
„NIE odpowiada", bo interpreter bez uprawnienia do sieci lokalnej (macOS
przyznaje je KONKRETNEJ binarce) dostawal `EHOSTUNREACH`. Roznica jest
kierunkowa: „nie odpowiada" to twierdzenie o flocie, „brak trasy" o hoscie
pomiarowym, i tylko pierwsze uzasadnia alarm.
"""
from __future__ import annotations

import errno
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]


def load_fleet_state():
    spec = importlib.util.spec_from_file_location(
        "fleet_state", REPO / "tests" / "lab" / "fleet-state.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EndpointReachabilityTests(unittest.TestCase):
    def setUp(self):
        self.module = load_fleet_state()

    def test_open_endpoint_answers(self):
        with mock.patch.object(self.module.socket, "create_connection"):
            self.assertIs(self.module.endpoint_reachable("192.0.2.10", 6033), True)

    def test_refused_and_timeout_are_fleet_verdicts(self):
        for failure in (OSError(errno.ECONNREFUSED, "refused"), TimeoutError("timed out")):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(
                    self.module.socket, "create_connection", side_effect=failure
                ):
                    self.assertIs(self.module.endpoint_reachable("192.0.2.10", 6033), False)

    def test_missing_route_is_not_a_fleet_verdict(self):
        for code in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EPERM, errno.EACCES):
            with self.subTest(errno=errno.errorcode[code]):
                with mock.patch.object(
                    self.module.socket,
                    "create_connection",
                    side_effect=OSError(code, "no route"),
                ):
                    self.assertIsNone(self.module.endpoint_reachable("192.0.2.10", 6033))


if __name__ == "__main__":
    unittest.main()
