"""Testy kontraktu skryptu tools/pve-pool-teardown.sh.

POWSTALY PO AUDYCIE 2026-09-07, ktory odtworzyl dwa realne defekty
(reprodukcje w sandboxie z atrapami terraform/curl — zero ruchu sieciowego):

  1. Nieczytelny `terraform output` (backend/lock/state) zostawial `managed`
     PUSTY, wiec zywa flota kwalifikowala sie jako "sieroty" — CONFIRM kasowal
     cala flote. Kontrakt: stan nieodczytalny = odmowa przed raportem.
  2. Odpowiedz DELETE (UPID) byla odrzucana i "skasowana" drukowano dla
     zadania, ktore moglo wlasnie padnac. Kontrakt: status zadania jest
     odpytywany i porazka taska konczy skrypt niezerem.
Testy uruchamiaja skrypt NAPRADE, z atrapami `terraform` i `curl` na PATH.
"""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "pve-pool-teardown.sh"

FAKE_TERRAFORM_OK = """#!/bin/sh
case "$*" in
  *output*) printf '{"zyska1":{"vmid":10040},"zyska2":{"vmid":10041}}' ;;
esac
exit 0
"""

FAKE_TERRAFORM_BROKEN = """#!/bin/sh
echo "state backend unavailable" >&2
exit 1
"""

FAKE_CURL = """#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["CURL_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")

if any("/cluster/resources" in a for a in argv):
    print(json.dumps({"data": [
        {"vmid": 10040, "node": "pve", "name": "managed", "status": "stopped",
         "pool": os.environ.get("POOL_NAME", "claude-isa")},
        {"vmid": 9999, "node": "pve", "name": "orphan", "status": "stopped",
         "pool": os.environ.get("POOL_NAME", "claude-isa")},
    ]}))
    sys.exit(0)

if any("/tasks/" in a and "/status" in a for a in argv):
    print(json.dumps({"data": {"status": os.environ.get("TASK_STATUS", "stopped"),
                               "exitstatus": os.environ.get("TASK_EXITSTATUS", "OK")}}))
    sys.exit(0)

if any("/qemu/9999" in a for a in argv):
    if "DELETE" in argv:
        print(json.dumps({"data": os.environ.get("DELETE_UPID", "UPID:pve:pending")}))
    else:
        print(json.dumps({"data": "UPID:pve:stop"}))
    sys.exit(0)

print(json.dumps({"data": "UPID:pve:other"}))
"""


def _write_executable(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class PoolTeardownHarness:
    def __init__(self, terraform_body):
        self.workdir = Path(tempfile.mkdtemp(prefix="pool-teardown-"))
        (self.workdir / "tools").mkdir()
        (self.workdir / "terraform" / "tenant").mkdir(parents=True)
        bin_dir = self.workdir / "bin"
        bin_dir.mkdir()
        (self.workdir / "terraform" / "tenant" / "main.tf").write_text("# atrapa\n")
        self.curl_log = self.workdir / "calls.jsonl"
        self.curl_log.write_text("", encoding="utf-8")
        _write_executable(bin_dir / "terraform", terraform_body)
        _write_executable(bin_dir / "curl", FAKE_CURL)
        (self.workdir / "tools" / "pve-pool-teardown.sh").write_bytes(
            (REPO / "tools" / "pve-pool-teardown.sh").read_bytes()
        )

    def run(self, env_extra=None):
        env = {
            "PATH": f"{self.workdir / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "PROXMOX_VE_ENDPOINT": "https://never.invalid:8006",
            "PROXMOX_VE_API_TOKEN": "root@pam!test=secret",
            "CURL_LOG": str(self.curl_log),
        }
        env.update(env_extra or {})
        proc = subprocess.run(
            ["bash", str(self.workdir / "tools" / "pve-pool-teardown.sh")],
            capture_output=True, text=True, env=env, timeout=30,
        )
        calls = [json.loads(line) for line in self.curl_log.read_text().splitlines()]
        return proc, calls

    def cleanup(self):
        import shutil
        shutil.rmtree(self.workdir, ignore_errors=True)


class PoolTeardownContractTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "harness", None) is not None:
            self.harness.cleanup()

    def test_unreadable_terraform_output_aborts_before_any_delete(self):
        """Atrapa terraform rc=1: skrypt MUSI odmowic i NIE wolno mu wyslac DELETE."""
        self.harness = PoolTeardownHarness(FAKE_TERRAFORM_BROKEN)
        proc, calls = self.harness.run({"CONFIRM_POOL": "claude-isa"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("nieczytelny", proc.stderr)
        deletes = [c for c in calls if "DELETE" in c]
        self.assertEqual(deletes, [], "DELETE przy nieodczytanym stanie = katastrofa floty")

    def test_delete_requires_exact_pool_confirmation(self):
        """CONFIRM=yes (stary odruch) NIE kasuje; wymagane powtorzenie nazwy puli."""
        self.harness = PoolTeardownHarness(FAKE_TERRAFORM_OK)
        proc, calls = self.harness.run({"CONFIRM": "yes"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("CONFIRM_POOL='claude-isa'", proc.stderr)
        self.assertEqual([c for c in calls if "DELETE" in c], [])

    def test_wrong_pool_confirmation_refuses(self):
        self.harness = PoolTeardownHarness(FAKE_TERRAFORM_OK)
        proc, calls = self.harness.run({"CONFIRM_POOL": "inna-pula"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual([c for c in calls if "DELETE" in c], [])

    def test_failed_delete_task_fails_the_run(self):
        """UPID zwrocony, ale task konczy sie bledem: rc MUSI byc niezerowe
        i banner nie wolno oglosic sukcesu."""
        self.harness = PoolTeardownHarness(FAKE_TERRAFORM_OK)
        proc, calls = self.harness.run({
            "CONFIRM_POOL": "claude-isa",
            "DELETE_UPID": "UPID:pve:failing",
            "TASK_STATUS": "stopped",
            "TASK_EXITSTATUS": "ERR-delete-failed",
        })
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("zakonczone bledem", proc.stderr + proc.stdout)

    def test_report_mode_never_deletes(self):
        self.harness = PoolTeardownHarness(FAKE_TERRAFORM_OK)
        proc, calls = self.harness.run({})
        self.assertNotEqual(proc.returncode, 0, "brak potwierdzenia = rc != 0")
        self.assertIn("9999", proc.stdout)
        self.assertEqual([c for c in calls if "DELETE" in c], [])


if __name__ == "__main__":
    unittest.main()
