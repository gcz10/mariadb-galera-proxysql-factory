#!/usr/bin/env python3
"""Testy regresyjne rozwijania adresow zarezerwowanych i wykrywania kolizji.

Weryfikuje:
  1. Pojedynczy adres pod `reserved` trafia do puli zakazanej.
  2. Blok CIDR pod `reserved_ranges` rozwija sie do kompletu wszystkich IP w sieci.
  3. Rejestr odrzuca niepoprawny schemat (np. `address` pod `reserved_ranges` lub `cidr` pod `reserved`).
  4. Rzeczywisty plik repo `clusters/reserved-addresses.yml` rozwija .100 oraz blok .180/30 (.181).
  5. Kolizja z adresem .100 oraz adresem z bloku (.181) jest wykrywana dla wezla klastra.
  6. Wezel z wolnym adresem nie zglasza kolizji z rejestrem.
  7. Brak pliku rejestru zwraca None (fail-closed kontrakt sondy).
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tests" / "validation" / "probe-address-collision.py"

_spec = importlib.util.spec_from_file_location("probe_address_collision_under_test", SCRIPT)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class ProbeAddressCollisionExpansionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="reserved-addr-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write_yaml(self, name: str, content: dict) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(content), encoding="utf-8")
        return path

    def test_reserved_single_address_expansion(self):
        data = {
            "reserved": [
                {"address": "192.168.1.25", "reason": "Host laboratoryjny"},
            ]
        }
        path = self.write_yaml("reserved.yml", data)
        res = probe.reserved_addresses(path)
        self.assertIsNotNone(res)
        self.assertIn("192.168.1.25", res)
        self.assertEqual(res["192.168.1.25"], "Host laboratoryjny")

    def test_reserved_ranges_cidr_expansion(self):
        data = {
            "reserved_ranges": [
                {"cidr": "192.168.1.180/30", "reason": "Hypervisor management block"},
            ]
        }
        path = self.write_yaml("reserved.yml", data)
        res = probe.reserved_addresses(path)
        self.assertIsNotNone(res)
        for expected in ("192.168.1.180", "192.168.1.181", "192.168.1.182", "192.168.1.183"):
            self.assertIn(expected, res)
            self.assertIn("blok 192.168.1.180/30", res[expected])

    def test_invalid_schema_in_reserved_ranges_fails(self):
        """Wpis z kluczem 'address' pod reserved_ranges jest niepoprawny i musi zostac odrzucony."""
        data = {
            "reserved_ranges": [
                {"address": "192.168.1.100", "reason": "Zly klucz pod reserved_ranges"},
            ]
        }
        path = self.write_yaml("reserved.yml", data)
        with self.assertRaises(ValueError):
            probe.reserved_addresses(path)

    def test_invalid_schema_in_reserved_fails(self):
        """Wpis z kluczem 'cidr' pod reserved jest niepoprawny i musi zostac odrzucony."""
        data = {
            "reserved": [
                {"cidr": "192.168.1.64/30", "reason": "Zly klucz pod reserved"},
            ]
        }
        path = self.write_yaml("reserved.yml", data)
        with self.assertRaises(ValueError):
            probe.reserved_addresses(path)

    def test_missing_file_returns_none(self):
        missing = self.root / "nonexistent.yml"
        self.assertIsNone(probe.reserved_addresses(missing))

    def test_actual_repo_reserved_addresses_expansion(self):
        """Rzeczywisty rejestr repozytorium rozwija .100 i blok .180/30."""
        actual = REPO / "clusters" / "reserved-addresses.yml"
        resolved = probe.reserved_addresses(actual)
        self.assertIsNotNone(resolved)
        self.assertIn("192.168.1.100", resolved)
        self.assertIn("192.168.1.181", resolved)

    def test_fixture_collision_detection_on_reserved_and_range(self):
        """Kolizja wezla z adresem pojedynczym (.100) oraz adresem z bloku (.181)."""
        data = {
            "reserved": [
                {"address": "192.168.1.100", "reason": "Zywy host spoza floty"},
            ],
            "reserved_ranges": [
                {"cidr": "192.168.1.180/30", "reason": "Blok zarzadzania"},
            ],
        }
        reg_path = self.write_yaml("reserved.yml", data)
        reserved = probe.reserved_addresses(reg_path)
        self.assertIsNotNone(reserved)

        cluster_hosts = {
            "node-colliding-100": "192.168.1.100",
            "node-colliding-block": "192.168.1.181",
            "node-safe": "192.168.1.55",
        }

        collisions = {}
        for host, addr in cluster_hosts.items():
            if addr in reserved:
                collisions[host] = reserved[addr]

        self.assertIn("node-colliding-100", collisions)
        self.assertIn("node-colliding-block", collisions)
        self.assertNotIn("node-safe", collisions)


if __name__ == "__main__":
    unittest.main()
