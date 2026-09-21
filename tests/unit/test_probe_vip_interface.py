#!/usr/bin/env python3
"""Interfejs noszacy VIP jest ROZSTRZYGANY, nie zgadywany (ISC-24/26).

Sondy endpointu i warstwy wspolnej wpisywaly `eth0` na sztywno: na parze, ktorej
interfejs nazywa sie inaczej (Rocky/KVM: `ens18`), `ip addr show dev eth0`
nie pokazal nic i sonda oglaszala "VIP trzymalo 0 wezlow" — falszywe naruszenie
ISC-24 zamiast braku pomiaru. Kontrakt jest ten sam co w f8_keepalived.yml:
PROXYSQL_ENDPOINT_INTERFACE wygrywa, a bez niej interfejs domyslnej trasy
hosta; brak rozstrzygniecia to `IFACE_UNRESOLVED`, nigdy `VIP=0`.

Test uruchamia WYGENEROWANY skrypt ad-hoc na zywym shellu z atrapa `ip` —
kontrakt to to, co skrypt wypisze, nie jego tresc.
"""

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "lab"))

COMMON_SPEC = importlib.util.spec_from_file_location(
    "probe_common_under_test", REPO / "tests" / "lab" / "_probe_common.py")
COMMON = importlib.util.module_from_spec(COMMON_SPEC)
COMMON_SPEC.loader.exec_module(COMMON)

VIP = "10.0.1.20"


class VipHolderShellTests(unittest.TestCase):
    def run_shell(self, env_extra, route_dev="alt0"):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        stub = Path(directory, "ip")
        lines = ["#!/bin/sh", 'case "$*" in']
        if route_dev:
            lines.append(
                '  "-o -4 route show default") echo "default via 10.0.0.1 dev '
                + route_dev + ' proto static";;')
        else:
            lines.append('  "-o -4 route show default") exit 0;;')
        lines.append(
            '  "link show dev alt0"|"link show dev decl0") exit 0;;')
        lines.append('  "link show dev ghost0") exit 1;;')
        lines.append(
            '  "-o -4 addr show dev alt0") echo "2: alt0 inet ' + VIP +
            '/24 brd 10.0.1.255 scope global alt0";;')
        lines.append(
            '  "-o -4 addr show dev decl0") echo "2: decl0 inet ' + VIP +
            '/24 brd 10.0.1.255 scope global decl0";;')
        lines.append('  *) exit 0;;')
        lines.append("esac")
        stub.write_text("\n".join(lines) + "\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        with mock.patch.dict(os.environ, env_extra):
            script = COMMON.vip_holder_shell(VIP)
        env = dict(os.environ)
        env["PATH"] = f"{directory}:{env['PATH']}"
        return subprocess.run(
            ["sh", "-c", script],
            capture_output=True, text=True, env=env, timeout=30)

    def test_default_route_interface_is_discovered(self):
        result = self.run_shell({})
        self.assertIn("IFACE=alt0", result.stdout)
        self.assertIn("VIP=1", result.stdout)
        self.assertNotIn("eth0", result.stdout)

    def test_missing_default_route_is_unresolved_not_vip_zero(self):
        result = self.run_shell({}, route_dev=None)
        self.assertIn("IFACE_UNRESOLVED", result.stdout)
        self.assertNotIn("VIP=0", result.stdout)

    def test_explicit_declaration_wins_over_discovery(self):
        result = self.run_shell({"PROXYSQL_ENDPOINT_INTERFACE": "decl0"}, route_dev="alt0")
        # Atrapa `ip` odpowiada na `link show dev <x>` i `addr show dev <x>` tylko
        # dla interfejsow znanych z testu: deklaracja musi PRZEZYC jako zrodlo.
        self.assertIn("IFACE=decl0", result.stdout)
        self.assertNotIn("IFACE=alt0", result.stdout)

    def test_declaration_of_absent_interface_fails_closed(self):
        result = self.run_shell({"PROXYSQL_ENDPOINT_INTERFACE": "ghost0"}, route_dev="alt0")
        self.assertIn("IFACE_UNRESOLVED", result.stdout)
        self.assertNotIn("VIP=0", result.stdout)


def load_probe(name, source):
    spec = importlib.util.spec_from_file_location(name, REPO / "tests" / "lab" / source)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"CLUSTER": "probe"}):
        spec.loader.exec_module(module)
    return module


class StubContext:
    def __init__(self, config, inventory):
        self.config = config
        self.inventory = inventory
        self.cluster_name = "probe"

    def group_hosts(self, group):
        hosts = self.inventory.get("all", {}).get("children", {}).get(group, {}).get("hosts", {})
        return list(hosts.keys())

    def host_address(self, name, group):
        entry = self.inventory.get("all", {}).get("children", {}).get(group, {}).get("hosts", {}).get(name)
        return (entry or {}).get("ansible_host")

    def env_secret(self, name):
        return {"PMM_ADMIN_PASSWORD": "x"}.get(name, "")


def probe_verdict(bodies_by_pattern, module, *, inventory=None, config=None):
    """Przepusci stan przez sone z atrapa ansible; zwraca (kod, wiersze wyjscia)."""
    from _probe_common import AnsibleResult

    def fake_ansible(ctx, pattern, script, timeout=120):
        result = AnsibleResult()
        group = pattern.split("[")[0]
        if group == "proxysql":
            result.bodies = bodies_by_pattern["proxysql"]
        else:
            result.bodies = bodies_by_pattern.get(group, {})
        return result

    with mock.patch.object(module, "ProbeContext",
                           lambda: StubContext(config or CONFIG, inventory or INVENTORY)), \
            mock.patch.object(module, "run_ansible", fake_ansible), \
            mock.patch("builtins.print") as printed:
        code = module.main()
    return code, "\n".join(" ".join(str(arg) for arg in call.args)
                            for call in printed.call_args_list)


CONFIG = {"proxysql": {"endpoint": {"type": "keepalived_vip", "address": VIP, "port": 6033}},
          "monitoring": {"pmm": {"server_url": "https://pmm.invalid", "cluster_name": "probe",
                                 "validate_certs": True}},
          "cluster": {"name": "probe", "environment": "laboratory"},
          "versions": {"lock_file": "versions/versions.lock.yml"},
          "proxysql_hostgroup_base": 10}
INVENTORY = {"all": {"children": {
    "proxysql": {"hosts": {"p1": {"ansible_host": "10.0.1.21"},
                           "p2": {"ansible_host": "10.0.1.22"}}},
    "galera": {"hosts": {"g1": {"ansible_host": "10.0.1.11"}}},
}}}

TLS_BODY = {"g1": "EXPIRY_OK\nsubject=CN=x\nissuer=CN=shared-ca"}


class ProbeEndpointInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.probe = load_probe("probe_endpoint_under_test", "probe-endpoint.py")

    def test_alternative_interface_is_a_measured_holder(self):
        bodies = {
            "p1": "IFACE=ens18 VIP=1 PROXYSQL=1",
            "p2": "IFACE=ens18 VIP=0 PROXYSQL=1",
        }
        code, output = probe_verdict({"proxysql": bodies, "galera": TLS_BODY}, self.probe)
        self.assertEqual(code, 0, output)
        self.assertIn("VIP 10.0.1.20 on p1 via ens18", output)

    def test_unresolved_interface_is_undetermined_not_a_false_violation(self):
        bodies = {"p1": "IFACE_UNRESOLVED PROXYSQL=1", "p2": "IFACE_UNRESOLVED PROXYSQL=1"}
        code, output = probe_verdict({"proxysql": bodies, "galera": TLS_BODY}, self.probe)
        self.assertEqual(code, 2, output)
        self.assertIn("nie rozstrzygnieto interfejsu VIP-a", output)
        self.assertNotIn("held by 0 nodes", output)

    def test_vip_missing_on_resolved_interface_is_still_a_violation(self):
        bodies = {"p1": "IFACE=ens18 VIP=1 PROXYSQL=1",
                  "p2": "IFACE=ens18 VIP=1 PROXYSQL=1"}
        code, output = probe_verdict({"proxysql": bodies, "galera": TLS_BODY}, self.probe)
        self.assertEqual(code, 1, output)
        self.assertIn("expected exactly 1", output)


PLATFORM_CONFIG = {
    "proxysql": {"endpoint": {"type": "keepalived_vip", "address": VIP, "port": 6033},
                 "nodes_expected": 2},
    "monitoring": {"pmm": {"server_url": "https://pmm.invalid", "cluster_name": "probe",
                           "validate_certs": True}},
}
PLATFORM_INVENTORY = {"all": {"children": {
    "proxysql": INVENTORY["all"]["children"]["proxysql"],
    "app": {"hosts": {"a1": {"ansible_host": "10.0.1.41"}}},
}}}
PMM_NODES = {"generic": [{"address": "10.0.1.21", "node_name": "probe-p1"},
                         {"address": "10.0.1.22", "node_name": "probe-p2"}]}
APP_TLS = {"a1": "CA=OK;Verify return code: 0 (ok)"}


class ProbePlatformInterfaceTests(unittest.TestCase):
    """Warstwa wspolna mierzy interfejs VIP te sama regula co f8_keepalived.yml."""

    def setUp(self):
        self.probe = load_probe("probe_platform_under_test", "probe-platform.py")

    def run_platform(self, bodies):
        up_series = [{"metric": {"node_name": "probe-p1"}, "value": [0, "1"]},
                     {"metric": {"node_name": "probe-p2"}, "value": [0, "1"]}]
        with mock.patch.object(self.probe, "pmm_json", return_value=PMM_NODES), \
                mock.patch.object(self.probe, "pmm_query", return_value=up_series):
            return probe_verdict({"proxysql": bodies, "app": APP_TLS}, self.probe,
                                 inventory=PLATFORM_INVENTORY, config=PLATFORM_CONFIG)

    def test_unresolved_interface_is_undetermined_not_a_false_withdrawal(self):
        bodies = {"p1": "IFACE_UNRESOLVED PROC=1 ADMIN=1 GROUPS=2 WRITERS=0",
                  "p2": "IFACE_UNRESOLVED PROC=1 ADMIN=1 GROUPS=2 WRITERS=0"}
        code, output = self.run_platform(bodies)
        self.assertEqual(code, 2, output)
        self.assertIn("nie rozstrzygnieto interfejsu VIP-a", output)
        # Werdykt "VIP zdjety zgodnie z ISC-26" nie moze powstac z pomiaru,
        # ktorego nie bylo: pary nie da sie ocenic bez obu odczytow.
        self.assertNotIn("VIP zdjety zgodnie z ISC-26", output)

    def test_holder_on_alternative_interface_passes(self):
        bodies = {"p1": "IFACE=ens18 VIP=1 PROC=1 ADMIN=1 GROUPS=0 WRITERS=0",
                  "p2": "IFACE=ens18 VIP=0 PROC=1 ADMIN=1 GROUPS=0 WRITERS=0"}
        code, output = self.run_platform(bodies)
        self.assertEqual(code, 0, output)


if __name__ == "__main__":
    unittest.main()
