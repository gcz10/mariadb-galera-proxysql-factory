#!/usr/bin/env python3
"""Testy kontraktu tworzenia VM w tools/pve-create-vm.sh (AUDIT 2026-09-07).

Piny zachowania, ktore przejechaly cicho przez zamiane curl->api():
  - zadanie CREATE MUSI zawierac vmid=$VMID — bez niego PVE generuje wlasne
    ID i caly pre-flight unikalnosci VMID staje sie martwy,
  - start MUSI zaczekac na task (UPID) — brak wyciagniecia UPID z odpowiedzi
    przy set -u konczy sie cichym "uruchomiona" bez oczekiwania,
  - nieudany task (exitstatus != OK) MUSI zwrocic niezerowy rc.

Atrapa curl loguje argv, zero ruchu sieciowego.
"""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "pve-create-vm.sh"

FAKE_CURL = """#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["CURL_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")

if any("/tasks/" in a and a.endswith("/status") for a in argv):
    print(json.dumps({"data": {"status": "stopped",
                               "exitstatus": os.environ.get("TASK_EXITSTATUS", "OK")}}))
    sys.exit(0)

if any(a.endswith("/qemu") and "-X" in argv and "POST" in argv for a in argv):
    print(json.dumps({"data": "UPID:pve:create"}))
    sys.exit(0)

if any(a.endswith("/resize") for a in argv):
    print(json.dumps({"data": "UPID:pve:resize"}))
    sys.exit(0)

if any(a.endswith("/status/start") for a in argv):
    print(json.dumps({"data": "UPID:pve:start"}))
    sys.exit(0)

print(json.dumps({"data": []}))
"""


def _write_executable(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class CreateVmCreateContractTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        self.curl_log = root / "calls.jsonl"
        self.curl_log.write_text("", encoding="utf-8")
        _write_executable(self.bin_dir / "curl", FAKE_CURL)
        self.key_file = root / "ssh_key.pub"
        self.key_file.write_text("ssh-ed25519 AAAA-test test@lab\n", encoding="utf-8")

    def run_script(self, env_extra=None):
        env = {
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PROXMOX_VE_ENDPOINT": "https://never.invalid:8006",
            "PROXMOX_VE_API_TOKEN": "root@pam!test=secret",
            "CURL_LOG": str(self.curl_log),
            "PVE_SSH_WAIT_RETRIES": "1",
            "PVE_SSH_WAIT_SLEEP": "0",
            # timeout — brak w PATH, pętla SSH pomijalna przez --no-wait-ssh
        }
        env.update(env_extra or {})
        proc = subprocess.run(
            [str(SCRIPT), "--vmid", "10020", "--name", "testvm",
             "--ip", "40", "--cluster", "test-c", "--key-file", str(self.key_file),
             "--no-wait-ssh"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        calls = [json.loads(line) for line in self.curl_log.read_text().splitlines()]
        return proc, calls

    def test_create_request_carries_explicit_vmid(self):
        """Zadanie POST /qemu MUSI zawierac vmid=10020; inaczej PVE przydziela
        wlasne ID i pre-flight unikalnosci jest martwy (audit 2026-09-07)."""
        proc, calls = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        create_calls = [
            c for c in calls
            if any(a.endswith("/qemu") for a in c) and "-X" in c and "POST" in c
        ]
        self.assertEqual(len(create_calls), 1, f"expected 1 create call, got {create_calls}")
        self.assertIn("vmid=10020", create_calls[0])

    def test_failed_create_task_fails_run(self):
        proc, _ = self.run_script({"TASK_EXITSTATUS": "ERR-import-failed"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("zakończył się błędem", proc.stderr)

    def test_failed_start_task_fails_run(self):
        """Start nie moze oglosic 'uruchomiona' dla taska z bledem."""
        proc, _ = self.run_script({"TASK_EXITSTATUS": "ERR-start-failed"})
        self.assertNotEqual(proc.returncode, 0)

    def test_sshkeys_transport_is_single_encoded_in_argv(self):
        """Obserwowalne w argv atrapy: skrypt podaje do --data-urlencode
        wartosc JEDNOKROTNIE zakodowana (prekodowanie quote(safe="")).

        Celowe i NIE jest defektem (patrz: PVE API2/Qemu.pm:1281-1284 robi
        uri_unescape po warstwie HTTP, Cloudinit.pm:170/361/394 znowu przy
        user-data; UI PVE robi encodeURIComponent/decodeURIComponent).
        Wariant "naprawiony" (surowy klucz wprost do --data-urlencode)
        zepsulby klucze z sekwencjami wygladajacymi jak percent-escapes w
        komentarzu: PVE zdjalby z nich druga warstwe.

        Piny:
          1. argv: spacja jest %20, plus %2B — a nie surowe znaki,
          2. literalny `%20` w komentarzu jest PODWOJNIE zakodowany (%2520),
          3. koniec lancucha: po dwoch dekodowaniach PVE klucz znak w znak.
        """
        key = "ssh-ed25519 AAAA+base/64=comment%20literal test@lab\n"
        self.key_file.write_text(key, encoding="utf-8")
        proc, calls = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)

        create = next(
            c for c in calls
            if any(a.endswith("/qemu") for a in c) and "-X" in c and "POST" in c
        )
        value = next(a.split("=", 1)[1] for a in create if a.startswith("sshkeys="))

        self.assertIn("%20", value, "spacja musi byc %20 (prekodowanie)")
        self.assertIn("%2B", value, "znak '+' z base64 musi byc %2B")
        self.assertIn("%2520", value, f"literal '%20' w komentarzu musi byc podwojnie zakodowany: {value}")

        import urllib.parse as up
        import re
        wire = up.quote("sshkeys=" + value, safe="=&").split("=", 1)[1]
        after_http = up.unquote_plus(wire)
        after_pve = re.sub(r"%([0-9A-Fa-f]{2})",
                           lambda m: chr(int(m.group(1), 16)), after_http)
        self.assertEqual(
            after_pve, key,
            "po dwoch dekodowaniach PVE klucz w user-data musi byc znak w znak",
        )


#!/usr/bin/env python3
"""Testy kontraktu tworzenia VM w tools/pve-create-vm.sh (AUDIT 2026-09-07).

Piny zachowania, ktore przejechaly cicho przez zamiane curl->api():
  - zadanie CREATE MUSI zawierac vmid=$VMID — bez niego PVE generuje wlasne
    ID i caly pre-flight unikalnosci VMID staje sie martwy,
  - start MUSI zaczekac na task (UPID) — brak wyciagniecia UPID z odpowiedzi
    przy set -u konczy sie cichym "uruchomiona" bez oczekiwania,
  - nieudany task (exitstatus != OK) MUSI zwrocic niezerowy rc.

Atrapa curl loguje argv, zero ruchu sieciowego.
"""


if __name__ == "__main__":
    unittest.main()
