#!/usr/bin/env python3
"""Testy kontraktu tworzenia VM w tools/pve-create-vm.sh (AUDIT 2026-09-07, 2026-09-21).

Piny zachowania, ktore przejechaly cicho przez zamiane curl->api():
  - zadanie CREATE MUSI zawierac vmid=$VMID — bez niego PVE generuje wlasne
    ID i caly pre-flight unikalnosci VMID staje sie martwy,
  - start MUSI zaczekac na task (UPID) — brak wyciagniecia UPID z odpowiedzi
    przy set -u konczy sie cichym "uruchomiona" bez oczekiwania,
  - nieudany task (exitstatus != OK) MUST zwrocic niezerowy rc.

AUDIT 2026-09-21 (przenosnosc): narzedzie jest generyczne, wiec testy pinuja
jeszcze transport jawnej konfiguracji infrastruktury:
  - `ipconfig0` niesie adres z DOKLADNIE tym prefiksem, ktory podal wywolujacy
    (zadnego doklejonego /24) i brame z --gateway,
  - `net0` niesie most z --bridge, a `pool` wartosc rozstrzygnieta przez
    --pool > FLEET_POOL,
  - brak wymaganej konfiguracji konczy sie kodem 2 BEZ zadnego zadania HTTP.

Adresy sa dokumentacyjne (RFC 5737). Atrapa curl loguje argv, zero ruchu sieciowego.
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

# Komplet jawnych ustawien infrastruktury. Adresy dokumentacyjne (RFC 5737),
# pula/most/wezel/storage/obraz/DNS sa argumentami, nie wartosciami skryptu.
INFRA = {
    "ip": "192.0.2.10/24",
    "gateway": "192.0.2.1",
    "cluster": "example-c1",
    "pool": "example-pool",
    "bridge": "vmbr0",
    "node": "node1",
    "storage": "local-zfs",
    "image": "local:import/rocky.qcow2",
    "nameserver": "203.0.113.53",
}


def infra_args(omit=(), **overrides):
    """Argumenty CLI z jawna konfiguracja; `omit` usuwa flagi, `overrides` podmienia."""
    infra = dict(INFRA)
    infra.update(overrides)
    args = []
    for flag, value in infra.items():
        if flag in omit:
            continue
        args += [f"--{flag}", value]
    return args


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

    def run_script(self, env_extra=None, omit=(), args=None, **overrides):
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
        self.curl_log.write_text("", encoding="utf-8")
        tool_args = args if args is not None else (
            ["--vmid", "10020", "--name", "testvm"]
            + infra_args(omit=omit, **overrides)
            + ["--key-file", str(self.key_file), "--no-wait-ssh"]
        )
        proc = subprocess.run(
            [str(SCRIPT)] + tool_args,
            capture_output=True, text=True, env=env, timeout=30,
        )
        calls = [json.loads(line) for line in self.curl_log.read_text().splitlines()]
        return proc, calls

    def _create_call(self, proc, calls):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        create_calls = [
            c for c in calls
            if any(a.endswith("/qemu") for a in c) and "-X" in c and "POST" in c
        ]
        self.assertEqual(len(create_calls), 1, f"expected 1 create call, got {create_calls}")
        return create_calls[0]

    def test_create_request_carries_explicit_vmid(self):
        """Zadanie POST /qemu MUSI zawierac vmid=10020; inaczej PVE przydziela
        wlasne ID i pre-flight unikalnosci jest martwy (audit 2026-09-07)."""
        proc, calls = self.run_script()
        create = self._create_call(proc, calls)
        self.assertIn("vmid=10020", create)

    def test_failed_create_task_fails_run(self):
        proc, _ = self.run_script({"TASK_EXITSTATUS": "ERR-import-failed"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("zakończył się błędem", proc.stderr)

    def test_failed_start_task_fails_run(self):
        """Start nie moze oglosic 'uruchomiona' dla taska z bledem."""
        proc, _ = self.run_script({"TASK_EXITSTATUS": "ERR-start-failed"})
        self.assertNotEqual(proc.returncode, 0)

    def test_network_config_is_explicit_and_not_shortened(self):
        """`ipconfig0` musi nieść adres z prefiksem wywołującego i jego bramę.

        Adresacja jest konfiguracją infrastruktury: skrypt nie ma prawa dokleić
        /24 ani podmienić bramy. Sprawdzamy przy okazji, że most, storage i DNS
        trafiają do requestu jako wartości podane, nie jako stałe labu.
        """
        proc, calls = self.run_script(
            ip="10.200.0.0/16",          # sieć z zerami oktetów i nie-/24
            gateway="10.200.254.1",
            bridge="vmbr7",
            storage="tank",
        )
        create = self._create_call(proc, calls)
        joined = " ".join(create)

        self.assertIn("ipconfig0=gw=10.200.254.1,ip=10.200.0.0/16", create,
                      f"adresacja nie dotarla w podanej postaci: {create}")
        self.assertNotIn("/24", joined, f"skrypt dokleil prefiks labu: {joined}")
        self.assertIn("net0=virtio,bridge=vmbr7,firewall=0", create)
        self.assertIn("virtio0=tank:0,import-from=local:import/rocky.qcow2", joined,
                      f"dysk nie wskazuje podanego storage: {joined}")
        self.assertIn("ide2=tank:cloudinit", create)
        self.assertIn("nameserver=203.0.113.53", create)

    def test_separate_prefix_flag_is_used_verbatim(self):
        """Prefiks podany osobno (--prefix) daje tę samą wartość na drucie co CIDR."""
        args = (
            ["--vmid", "10020", "--name", "testvm"]
            + infra_args(ip="198.51.100.7")
            + ["--prefix", "20", "--key-file", str(self.key_file), "--no-wait-ssh"]
        )
        proc, calls = self.run_script(args=args)
        create = self._create_call(proc, calls)
        self.assertIn("ipconfig0=gw=192.0.2.1,ip=198.51.100.7/20", create)

    def test_pool_flag_overrides_fleet_pool_env(self):
        """Pierwszeństwo puli: --pool wygrywa ze zmienną środowiskową FLEET_POOL."""
        proc, calls = self.run_script({"FLEET_POOL": "example-pool"}, pool="customer-west")
        create = self._create_call(proc, calls)
        self.assertIn("pool=customer-west", create)
        self.assertNotIn("pool=example-pool", create)

    def test_fleet_pool_env_used_when_flag_absent(self):
        """Bez --pool pula bierze się z FLEET_POOL (jawnej dla tej infrastruktury)."""
        proc, calls = self.run_script({"FLEET_POOL": "customer-east"}, omit=("pool",))
        create = self._create_call(proc, calls)
        self.assertIn("pool=customer-east", create)

    def test_missing_pool_makes_no_request(self):
        """Brak puli (bez --pool i bez FLEET_POOL) to błąd konfiguracji bez ruchu sieciowego."""
        proc, calls = self.run_script(omit=("pool",))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("BŁĄD: --pool jest wymagany", proc.stderr)
        self.assertEqual(calls, [], f"brak konfiguracji wykonał zapytanie: {calls}")

    def test_missing_infrastructure_config_makes_no_request(self):
        """Każdy brak ustawienia infrastruktury kończy pracę bez żadnego zapytania do PVE."""
        for flag in ("ip", "gateway", "bridge", "node", "storage", "image", "nameserver", "cluster"):
            with self.subTest(flag=flag):
                proc, calls = self.run_script(omit=(flag,))
                self.assertEqual(proc.returncode, 2, f"{flag}: {proc.stderr}")
                self.assertIn(f"BŁĄD: --{flag}", proc.stderr)
                self.assertEqual(calls, [], f"{flag}: brak konfiguracji wykonał zapytanie: {calls}")

    def test_environment_missing_makes_no_request(self):
        """Brak poświadczeń PVE również nie generuje zapytania (walidacja przed curl-em)."""
        proc, calls = self.run_script({"PROXMOX_VE_ENDPOINT": "", "PROXMOX_VE_API_TOKEN": ""})
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("PROXMOX_VE_ENDPOINT", proc.stderr)
        self.assertEqual(calls, [], f"brak poświadczeń wykonał zapytanie: {calls}")


if __name__ == "__main__":
    unittest.main()
