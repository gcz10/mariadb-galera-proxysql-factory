#!/usr/bin/env python3
"""chaos-proxysql-failover: "przywrocone" musi znaczyc "ProxySQL znow odpowiada".

Dwa defekty z przegladu repozytorium (2026-09-12), oba w tej samej sondzie
tests/lab/chaos-proxysql-failover.py:

1. FALSZYWY PASS PO NIEUDANYM PRZYWROCENIU. `restored` bylo ustawiane bez
   sprawdzenia czegokolwiek: w trybie `service` tuz po `systemctl start proxysql`
   (rc nie byl nawet czytany), a w trybie `node` takze wtedy, gdy petla
   oczekiwania na port administracyjny wyczerpala deadline (galez `else`
   drukowala tylko UWAGA). Sonda oglaszala wiec PASS przy wezle, ktory zostal
   martwy, a kolejne przebiegi mierzyly zdegradowana warstwe ProxySQL.
   Test (a) i (b) wymagaja wyjscia 1, gdy port 6032 nigdy nie odpowie;
   test (c) pilnuje, zeby naprawiony tryb `service` nadal konczyl sie PASS-em
   (poprawka nie moze karac zdrowego przebiegu).

2. HASLO APLIKACJI W ARGUMENTACH PROCESU. Profil klienta powstawal przez
   `sh(APP_HOST, "printf ... password={APP_PW} ... > {CNF_REMOTE}")`, czyli
   haslo jechalo jako argument `ansible -m shell -a` i bylo widoczne w `ps` na
   hoscie kontrolnym przez cale wywolanie. Test (d) zbiera KAZDY argv przekazany
   do `subprocess.run` oraz kazdy skrypt przekazany do `sh` i wymaga, zeby
   wartosci sentinel nie bylo w zadnym z nich — a takze, zeby haslo faktycznie
   dotarlo na wezel (modul `copy` z pliku lokalnego) i zeby lokalna kopia
   zniknela po zakonczeniu sondy.

Sonda jest uruchamiana bez infrastruktury: `sh` (ansible ad-hoc), `subprocess`,
`vip_holder`, `hostgroups_present`, `pve`, `committed_rows`, `committed_seqs`
i zegar sa atrapami. Modul ladujemy z run_name != "__main__", z konfiguracja
clusters/example-cluster, zeby `main()` nie odpalil sie przy imporcie.
"""

import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "tests" / "lab" / "chaos-proxysql-failover.py"
CLUSTER_DIR = REPO / "clusters" / "example-cluster"

# Sentinel, nie prawdziwe haslo: test ma wykryc WYCIEK, a nie znac wartosc.
SENTINEL_PW = "sentinel-app-password-must-not-leak-9d41"
VICTIM = "pnode1"
VICTIM_VMID = "9401"


def load_probe():
    """Laduje sonde po sciezce, z konfiguracja example-cluster i bez main()."""
    spec = importlib.util.spec_from_file_location("chaos_proxysql_under_test", PROBE)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        os.environ,
        {
            "CLUSTER_CONFIG": str(CLUSTER_DIR / "cluster.yml"),
            "CLUSTER_INVENTORY": str(CLUSTER_DIR / "inventory.yml"),
        },
    ):
        spec.loader.exec_module(module)
    return module



class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


class VirtualClock:
    """Zegar wirtualny: kazdy `sleep` przesuwa czas, wiec petle z deadline koncza."""

    def __init__(self, start=1000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeCompleted:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


class FakeAnsibleCopy:
    """Atrapa `subprocess.run`: rejestruje argv i czyta plik zrodlowy `-m copy`."""

    def __init__(self):
        self.argv = []
        self.transferred = []  # (src, zawartosc pliku w chwili kopiowania)

    def run(self, argv, **kwargs):
        args = [str(part) for part in argv]
        self.argv.append(args)
        if "copy" in args:
            spec = args[args.index("-a") + 1] if "-a" in args else ""
            fields = dict(part.split("=", 1) for part in spec.split() if "=" in part)
            src = fields.get("src")
            if src:
                with open(src, encoding="utf-8") as fh:
                    self.transferred.append((src, fh.read()))
        return FakeCompleted()


class FakeAnsible:
    """Atrapa `sh`: rozpoznaje skrypty po tresci, nie po kolejnosci wywolan.

    `admin_answers=False` odwzorowuje wezel, na ktorym ProxySQL nie wstaje —
    kazdy start/restart pada (rc 1) i port 6032 milczy.
    """

    def __init__(self, admin_answers=True, service_rc=0, raise_on_systemctl=False):
        self.calls = []
        self.admin_answers = admin_answers
        self.service_rc = service_rc
        self.raise_on_systemctl = raise_on_systemctl
        self.present_rows = 2

    def __call__(self, host, script, timeout=120, check=False):
        self.calls.append((host, script))
        if self.raise_on_systemctl and "systemctl start proxysql" in script:
            raise RuntimeError("connection to control host lost")
        rc, out = self._respond(host, script)
        if check and rc != 0:
            raise RuntimeError(f"{host}: {out}")
        return rc, out

    def _respond(self, host, script):
        if "CREATE TABLE" in script:
            return 0, ""
        if "nohup" in script and "SELECT SLEEP(90)" in script:
            return 0, "held"
        if script.startswith("touch /tmp/workload.run"):
            return 0, "start"
        if "pkill -9 -x proxysql" in script:
            return 0, "killed"
        if "pgrep -x proxysql" in script:
            return 0, "DEAD"
        if script == "echo up":
            return 0, "up"
        if "6032" in script:
            return (0, "ADMIN_OK") if self.admin_answers else (1, "ADMIN_DOWN")
        if "systemctl start proxysql" in script or "systemctl restart proxysql" in script:
            return self.service_rc, "job for proxysql.service failed"
        if "ip -4 -o addr show" in script:
            return 0, "YES"
        if "longsession.out" in script:
            return 0, "ERROR 2013 (HY000): Lost connection to server\nGONE"
        if "pkill -f 'SELECT SLEEP(90)'" in script:
            return 0, "done"
        if "COUNT(*) FROM isa_failover" in script:
            return 0, str(self.present_rows)
        if script.startswith("rm -f"):
            return 0, ""
        raise AssertionError(f"nieoczekiwany skrypt atrapy: {script!r}")


class ChaosProxysqlFailoverSafetyTests(unittest.TestCase):
    def run_main(self, mode, admin_answers=True, service_rc=0, seqs=(1, 2),
                 raise_on_systemctl=False):
        module = load_probe()
        clock = VirtualClock()
        ansible = FakeAnsible(admin_answers=admin_answers, service_rc=service_rc,
                              raise_on_systemctl=raise_on_systemctl)
        copy_stub = FakeAnsibleCopy()
        ansible.present_rows = len(seqs)
        pve_calls = []

        # committed_rows musi rosnac, inaczej petla ciaglosci nie skonczy sie
        # wnioskiem "aplikacja wrocila do zapisywania".
        seen = []

        def committed_rows():
            seen.append(len(seen))
            if len(seen) == 1:
                return 10  # zapisy przed awaria
            if len(seen) == 2:
                return 20  # znacznik przed petla ciaglosci
            return 24      # > 20 + 3

        with mock.patch.multiple(
            module,
            APP_PW=SENTINEL_PW,
            MODE=mode,
            sh=ansible,
            subprocess=copy_stub,
            time=clock,
            vip_holder=lambda: VICTIM,
            hostgroups_present=lambda host: True,
            pve=lambda vmid, action, node: pve_calls.append((str(vmid), action, node)),
            committed_rows=committed_rows,
            committed_seqs=lambda: ([1.0, 2.0], list(seqs)),
        ), mock.patch.dict(
            os.environ, {
                "PROXYSQL_VMIDS": f"{VICTIM}={VICTIM_VMID}",
                "PROXMOX_VE_ENDPOINT": "https://hv.example.invalid:8006",
                "PROXMOX_VE_API_TOKEN": "root@pam!probe=token",
                "PROXMOX_VE_NODE": "hvnode1",
            }
        ), mock.patch("builtins.print") as printed:
            code = module.main()

        output = "\n".join(" ".join(str(arg) for arg in call.args)
                           for call in printed.call_args_list)
        return code, output, ansible, copy_stub, pve_calls

    def test_node_restore_without_admin_port_is_failure(self):
        """Tryb node: VM wstala, ale ProxySQL nie odpowiada na 6032 -> exit 1."""
        code, output, _, _, pve_calls = self.run_main(
            "node", admin_answers=False, service_rc=1)
        self.assertEqual(code, 1, output)
        self.assertNotIn("PASS:", output)
        self.assertIn("nie odpowiada na porcie administracyjnym", output)
        self.assertIn("6032", output)
        # Przywracanie maszyny nadal sie odbywa: sonda oddaje lab w stanie, w jakim go zastala.
        self.assertEqual(pve_calls,
                         [(VICTIM_VMID, "stop", "hvnode1"), (VICTIM_VMID, "start", "hvnode1")])

    def test_service_restore_without_admin_port_is_failure(self):
        """Tryb service: `systemctl start` zwraca 0, ale usluga nie wstaje -> exit 1."""
        code, output, _, _, _ = self.run_main(
            "service", admin_answers=False, service_rc=1)
        self.assertEqual(code, 1, output)
        self.assertNotIn("PASS:", output)
        self.assertIn("nie odpowiada na porcie administracyjnym", output)

    def test_restore_exception_is_failure_not_swallowed_warning(self):
        """Wyjatek przy przywracaniu: samo `UWAGA:` obok `restored=False` = falszywy PASS."""
        code, output, _, _, _ = self.run_main("service", raise_on_systemctl=True)
        self.assertEqual(code, 1, output)
        self.assertNotIn("PASS:", output)
        self.assertIn("nie udalo sie przywrocic", output)

    def test_healthy_service_run_still_passes(self):
        """Poprawka nie moze karac zdrowego przebiegu: port odpowiada -> exit 0."""
        code, output, ansible, _, _ = self.run_main("service")
        self.assertEqual(code, 0, output)
        self.assertIn("PASS:", output)
        self.assertIn("przywrocony=True", output)
        # Zdrowy przebieg naprawde sprawdzil port administracyjny, zamiast go zalozyc.
        self.assertTrue(any("6032" in script for _, script in ansible.calls))

    def test_application_password_never_reaches_process_arguments(self):
        """Haslo aplikacji nie moze byc argumentem zadnego wywolanego polecenia."""
        code, output, ansible, copy_stub, _ = self.run_main("service")
        self.assertEqual(code, 0, output)

        argv_flat = [part for args in copy_stub.argv for part in args]
        scripts = [script for _, script in ansible.calls]
        haystack = argv_flat + scripts
        self.assertTrue(haystack, "atrapy nie zarejestrowaly zadnego polecenia")
        self.assertEqual([item for item in haystack if SENTINEL_PW in item], [])

        # ...ale haslo MUSI dotrzec na wezel: dowodem jest plik zrodlowy `copy`,
        # ktory w chwili kopiowania zawieral sentinel.
        copied = [(src, body) for src, body in copy_stub.transferred
                  if body == f"[client]\nuser=app_user_example\npassword={SENTINEL_PW}\n"]
        self.assertEqual(len(copied), 1, copy_stub.transferred)

        src, _ = copied[0]
        self.addCleanup(lambda: os.path.exists(src) and os.unlink(src))
        self.assertFalse(os.path.exists(src),
                         "lokalna kopia poswiadczen zostala po zakonczeniu sondy")


class ChaosPveTargetTests(unittest.TestCase):
    """Tryb `node` nie ma danych hypervisora skadkolwiek — tylko z deklaracji.

    Do 2026-09-21 endpoint mial labowy fallback, a wezel byl wpisany na sztywno:
    probe destrukcyjna wysylala stop() tam, gdzie kazal adres z repozytorium.
    """

    def run_main_node(self, env_extra):
        module = load_probe()
        clock = VirtualClock()
        ansible = FakeAnsible()
        copy_stub = FakeAnsibleCopy()
        pve_calls = []
        seen = []

        def committed_rows():
            seen.append(1)
            return {1: 10, 2: 20}.get(len(seen), 24)

        with mock.patch.multiple(
            module,
            APP_PW=SENTINEL_PW,
            MODE="node",
            sh=ansible,
            subprocess=copy_stub,
            time=clock,
            vip_holder=lambda: VICTIM,
            hostgroups_present=lambda host: True,
            pve=lambda vmid, action, node: pve_calls.append((str(vmid), action, node)),
            committed_rows=committed_rows,
            committed_seqs=lambda: ([1.0, 2.0], [1, 2]),
        ), mock.patch.dict(os.environ, env_extra), mock.patch("builtins.print") as printed:
            code = module.main()
        output = "\n".join(" ".join(str(arg) for arg in call.args)
                           for call in printed.call_args_list)
        return code, output, ansible, pve_calls

    def test_node_mode_refuses_without_declared_hypervisor(self):
        code, output, ansible, pve_calls = self.run_main_node({
            "PROXYSQL_VMIDS": f"{VICTIM}={VICTIM_VMID}",
            "PROXMOX_VE_ENDPOINT": "",
            "PROXMOX_VE_API_TOKEN": "",
        })
        self.assertEqual(code, 1, output)
        self.assertIn("PROXMOX_VE_ENDPOINT", output)
        self.assertEqual(pve_calls, [])
        self.assertFalse(
            any("pkill" in script or "workload.run" in script for _, script in ansible.calls),
            "sonda mutowala flote przed rozstrzygnieciem danych hypervisora")

    def test_node_mode_reports_the_resolved_target_before_mutation(self):
        code, output, ansible, pve_calls = self.run_main_node({
            "PROXYSQL_VMIDS": f"{VICTIM}={VICTIM_VMID}",
            "PROXMOX_VE_ENDPOINT": "https://hv.example.invalid:8006/",
            "PROXMOX_VE_API_TOKEN": "root@pam!probe=token",
            "PROXMOX_VE_NODE": "hvnode1",
        })
        self.assertEqual(code, 0, output)
        self.assertIn("hvnode1", output)
        self.assertEqual(
            pve_calls,
            [(VICTIM_VMID, "stop", "hvnode1"), (VICTIM_VMID, "start", "hvnode1")])

    def test_node_resolution_prefers_declaration_and_falls_back_to_cluster_resources(self):
        module = load_probe()
        with mock.patch.dict(os.environ, {"PROXMOX_VE_NODE": "decl-node"}):
            self.assertEqual(module.pve_node("https://hv.example.invalid", "tok", "9401"),
                             "decl-node")
        with mock.patch.dict(os.environ, {"PROXMOX_VE_NODE": ""}), \
                mock.patch.object(
                    module.urllib.request, "urlopen",
                    return_value=_FakeResponse(b'{"data": [{"vmid": 9401, "node": "list-node"}]}')):
            self.assertEqual(module.pve_node("https://hv.example.invalid", "tok", "9401"),
                             "list-node")
        with mock.patch.dict(os.environ, {"PROXMOX_VE_NODE": ""}), \
                mock.patch.object(
                    module.urllib.request, "urlopen",
                    return_value=_FakeResponse(b'{"data": [{"vmid": 2, "node": "other"}]}')):
            with self.assertRaises(RuntimeError):
                module.pve_node("https://hv.example.invalid", "tok", "9401")


if __name__ == "__main__":
    unittest.main()
