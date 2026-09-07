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


if __name__ == "__main__":
    unittest.main()
