"""Behawioralne regresje dla zakresu FLOTOWEGO sondy gcache (ISC-68).

DLACZEGO TEN TEST ISTNIEJE: `deployed_gcache()` czytal `/etc/my.cnf.d/server.cnf`
WYLACZNIE z `GALERA[0]`, a werdykt `main()` i podsumowanie PASS twierdzily, ze
wartosc obowiazuje CALY klaster. Odtworzenie: g1=512M, g2=g3=128M, wymagane 344M
-> sonda odpytywala tylko g1 i zwracala exit 0, choc dwa pozostale wezly wrocilyby
po awarii pelnym SST zamiast IST. Rozjazd konfiguracji miedzy wezlami przechodzil
bramke jako PASS.

Testy wymuszaja trzy wlasnosci, ktore czynia z odczytu twierdzenie o flocie:
- odczyt gcache.size z KAZDEGO wezla grupy galera,
- ocena po NAJMNIEJSZEJ wartosci (o wyniku decyduje najslabszy wezel),
- host bez odpowiedzi jest jawnie nierozstrzygniety (exit 2), nigdy cicho pominiety.

Bez infrastruktury: `run_ansible` jest podstawiony atrapa per-host, wiec zaden
SSH, Proxmox, klaster Galery ani ProxySQL nie jest dotykany.
"""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT_PATH = WORKSPACE / "tests" / "lab" / "probe-gcache.py"
PROBE_COMMON_DIR = WORKSPACE / "tests" / "lab"

CLUSTER_YML = {
    "cluster": {"environment": "laboratory", "name": "test-cluster"},
    "proxysql": {"hostgroup_base": 10},
}

INVENTORY_YML = {
    "all": {
        "children": {
            "galera": {
                "hosts": {
                    "gnode1": {"ansible_host": "192.0.2.11"},
                    "gnode2": {"ansible_host": "192.0.2.12"},
                    "gnode3": {"ansible_host": "192.0.2.13"},
                }
            },
            "proxysql": {"hosts": {"pnode1": {"ansible_host": "192.0.2.10"}}},
        }
    }
}

# 200000 B/s x 30 min x 60 s = 360000000 B = 343.3 MiB -> wymagane 344M.
WRITE_RATE_BPS = 200000
REQUIRED_MB = 344
IST_WINDOW_MIN = 30


class _FleetAnsible:
    """Atrapa `run_ansible`: ciala per host zamiast prawdziwej floty."""

    def __init__(self, per_host, result_cls, writer="gnode1"):
        self.per_host = per_host
        self.result_cls = result_cls
        self.writer = writer
        self.calls = []

    def __call__(self, ctx, pattern, script, timeout=120):
        self.calls.append(pattern)
        result = self.result_cls()
        if pattern == "pnode1":
            # ProxySQL wskazuje writer; adres mapuje sie na gnode1 z inwentarza.
            result.bodies["pnode1"] = "192.0.2.11\n"
        elif pattern == self.writer:
            result.bodies[self.writer] = (
                f"RATE_BPS={WRITE_RATE_BPS} DELTA=360000000 ELAPSED=20s\n"
            )
        elif pattern == "galera":
            for host, body in self.per_host.items():
                result.bodies[host] = body
        else:
            raise AssertionError(f"nieoczekiwany pattern ansible: {pattern!r}")
        return result


class ProbeGcacheFleetScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cfg_file = root / "cluster.yml"
        inv_file = root / "inventory.yml"
        cfg_file.write_text(yaml.safe_dump(CLUSTER_YML), encoding="utf-8")
        inv_file.write_text(yaml.safe_dump(INVENTORY_YML), encoding="utf-8")

        if str(PROBE_COMMON_DIR) not in sys.path:
            sys.path.insert(0, str(PROBE_COMMON_DIR))
        from _probe_common import AnsibleResult

        cls.AnsibleResult = AnsibleResult

        spec = importlib.util.spec_from_file_location("probe_gcache_fleet", SCRIPT_PATH)
        with mock.patch.dict(
            os.environ,
            {
                "CLUSTER": "test-cluster",
                "CLUSTER_CONFIG": str(cfg_file),
                "CLUSTER_INVENTORY": str(inv_file),
                "ISC68_IST_WINDOW_MIN": str(IST_WINDOW_MIN),
            },
        ):
            cls.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.mod)

    def _run_main(self, per_host):
        fake = _FleetAnsible(per_host, self.AnsibleResult)
        out = io.StringIO()
        with mock.patch.object(self.mod, "run_ansible", new=fake), \
                contextlib.redirect_stdout(out):
            code = self.mod.main()
        return code, out.getvalue(), fake

    def test_divergent_sizes_below_requirement_fail_and_name_lagging_hosts(self):
        code, out, fake = self._run_main({
            "gnode1": "gcache.size=512M\n",
            "gnode2": "gcache.size=128M\n",
            "gnode3": "gcache.size=128M\n",
        })

        self.assertEqual(code, 1, out)
        self.assertIn("nodes below the requirement: gnode2, gnode3", out)
        self.assertIn(f"required {REQUIRED_MB}M", out)
        self.assertNotIn("PASS:", out)
        # Odczyt musi objac CALA grupe, nie pierwszy host z inwentarza.
        self.assertIn("galera", fake.calls, "deployed read must target the whole group")
        for host, value in (("gnode1", "512M"), ("gnode2", "128M"), ("gnode3", "128M")):
            self.assertIn(f"{host}={value}", out)

    def test_all_hosts_at_or_above_requirement_pass(self):
        code, out, _ = self._run_main({
            "gnode1": "gcache.size=512M\n",
            "gnode2": "gcache.size=512M\n",
            "gnode3": "gcache.size=512M\n",
        })

        self.assertEqual(code, 0, out)
        self.assertIn("PASS:", out)
        self.assertIn("512M on all 3 galera nodes", out)
        self.assertIn(f"required {REQUIRED_MB}M", out)

    def test_pass_summary_certifies_minimum_when_hosts_differ(self):
        code, out, _ = self._run_main({
            "gnode1": "gcache.size=1024M\n",
            "gnode2": "gcache.size=512M\n",
            "gnode3": "gcache.size=512M\n",
        })

        self.assertEqual(code, 0, out)
        self.assertIn("PASS:", out)
        # PASS nie moze twierdzic o 1024M, skoro IST gwarantuje tylko 512M.
        self.assertIn("min 512M of 1024M across 3 galera nodes", out)

    def test_host_missing_from_ansible_result_is_undetermined(self):
        code, out, _ = self._run_main({
            "gnode1": "gcache.size=512M\n",
            "gnode2": "gcache.size=512M\n",
            # gnode3 nie odpowiedzial wcale
        })

        self.assertEqual(code, 2, out)
        self.assertIn("gnode3", out)
        self.assertNotIn("PASS:", out)

    def test_missing_setting_counts_as_zero_and_fails_floor(self):
        code, out, _ = self._run_main({
            # grep bez trafienia: w server.cnf nie ma ustawionego gcache.size
            "gnode1": "",
            "gnode2": "gcache.size=512M\n",
            "gnode3": "gcache.size=512M\n",
        })

        self.assertEqual(code, 1, out)
        self.assertIn("gnode1=0M", out)
        self.assertIn("128M floor", out)
        self.assertIn("nodes below the requirement: gnode1", out)


if __name__ == "__main__":
    unittest.main()
