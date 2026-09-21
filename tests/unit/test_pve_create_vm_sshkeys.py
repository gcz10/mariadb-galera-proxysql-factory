#!/usr/bin/env python3
"""Kontrakt kodowania `sshkeys` w tools/pve-create-vm.sh (audyt 2026-09-14).

Hipoteza z przegladu repo brzmiala: skrypt koduje klucz DWA razy (prekodowanie
`urllib.parse.quote(safe="")` + `curl --data-urlencode`), wiec PVE dostaje
literalne `%20` w `authorized_keys` i logowanie kluczem nie dziala.

ZMIERZONE 2026-09-14 i hipoteza jest FALSZYWA. PVE dekoduje te wartosc dwa
razy — raz w warstwie HTTP, raz wlasnym `URI::Escape::uri_unescape`:

  * `src/PVE/API2/Qemu.pm:1281-1284` — po dekodowaniu POST robi
    `uri_unescape($ssh_keys)` i dopiero wtedy `validate_ssh_public_keys`,
  * `src/PVE/QemuServer/Cloudinit.pm:170/361/394` — `uri_unescape` przy
    generowaniu user-data (nocloud / configdrive2 / cloudbase),
  * ten sam wzorzec w UI PVE (`www/manager6/qemu/SSHKey.js`:
    `encodeURIComponent` przy zapisie, `decodeURIComponent` przy odczycie).

Skrypt jest POPRAWNY. Ten plik pinuje kontrakt, bo KAZDA "oczywista naprawa" tego miejsca psuje
transport kluczy zawierajacych sekwencje wygladajace jak percent-escape
(np. komentarz z `%20`): po jednym dekodowaniu zostalyby literalne, a PVE
zdjalby z nich druga warstwe i zamienil je na spacje.

Atrapa curl nic nie koduje (zapisuje argv), wiec test odtwarza obie warstwy
dekodowania jawnie — dokladnie te, ktore wykonuje PVE.
"""

import json
import os
import stat
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "pve-create-vm.sh"

FAKE_CURL = """#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["CURL_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")

if any("/tasks/" in a and a.endswith("/status") for a in argv):
    print(json.dumps({"data": {"status": "stopped", "exitstatus": "OK"}}))
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

# Klucz z sekwencjami, na ktorych kazda "naprawa" sie wywraca: spacja, '+'
# z base64, '@' i LITERALNE `%20` w komentarzu.
KEY = "ssh-ed25519 AAAA+base/64=comment%20literal test@lab\n"


def _write_executable(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class SshKeyTransportTests(unittest.TestCase):
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
        self.key_file.write_text(KEY, encoding="utf-8")

    def _create_call(self):
        env = {
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PROXMOX_VE_ENDPOINT": "https://never.invalid:8006",
            "PROXMOX_VE_API_TOKEN": "root@pam!test=secret",
            "CURL_LOG": str(self.curl_log),
            "PVE_SSH_WAIT_RETRIES": "1",
            "PVE_SSH_WAIT_SLEEP": "0",
        }
        proc = subprocess.run(
            [str(SCRIPT),
             "--vmid", "10020", "--name", "testvm",
             "--ip", "192.0.2.10/24", "--gateway", "192.0.2.1",
             "--cluster", "example-c1", "--pool", "example-pool",
             "--bridge", "vmbr0", "--node", "node1",
             "--storage", "local-zfs", "--image", "local:import/rocky.qcow2",
             "--nameserver", "203.0.113.53",
             "--key-file", str(self.key_file),
             "--no-wait-ssh"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = [json.loads(line) for line in self.curl_log.read_text().splitlines()]
        return next(
            c for c in calls
            if any(a.endswith("/qemu") for a in c) and "-X" in c and "POST" in c
        )

    def test_value_handed_to_curl_is_fully_percent_encoded(self):
        """Skrypt pre-koduje klucz, zanim poda go curl-owi.

        Atrapa widzi argv, wiec widzi wlasnie ten etap: pojedyncze kodowanie.
        Spacja => `%20`, `+` => `%2B`, literalny `%` => `%25`.
        """
        create = self._create_call()
        value = next(a.split("=", 1)[1] for a in create if a.startswith("sshkeys="))

        self.assertIn("%20", value, f"spacja niezakodowana: {value}")
        self.assertIn("%2B", value, "znak '+' z base64 musi byc zakodowany")
        self.assertIn("%25", value, "literalny '%' w komentarzu musi byc zakodowany")
        self.assertNotIn(" ", value, "wartosc nie moze zawierac surowej spacji")
        self.assertNotIn("\n", value, "wartosc nie moze zawierac surowego newline")

    def test_key_arrives_intact_after_the_two_pve_unescapes(self):
        """Po dekodowaniu HTTP i wlasnym `uri_unescape` PVE klucz jest 1:1.

        To sedno kontraktu: dwa kodowania klienta rownowaza dwa dekodowania po
        stronie PVE. Ciag odtwarzamy wprost:
          argv (pojedyncze kodowanie) -> curl dokodowuje -> serwer HTTP dekoduje
          -> `URI::Escape::uri_unescape` (API2/Qemu.pm:1282, Cloudinit.pm:170).
        Wynik musi byc identyczny z plikiem klucza.
        """
        create = self._create_call()
        argv_value = next(a.split("=", 1)[1] for a in create if a.startswith("sshkeys="))
        expected_argv_value = urllib.parse.quote(KEY, safe="")
        self.assertEqual(
            argv_value,
            expected_argv_value,
            "skrypt nie przekazal klucza w postaci raz zakodowanej",
        )

        on_the_wire = urllib.parse.quote(argv_value, safe="")   # --data-urlencode
        self.assertIn("%2520", on_the_wire, f"brak drugiego kodowania: {on_the_wire}")

        after_http = urllib.parse.unquote_plus(on_the_wire)     # warstwa HTTP PVE
        after_pve = urllib.parse.unquote(after_http)            # uri_unescape PVE

        self.assertEqual(
            after_pve,
            KEY,
            "klucz nie dociera do cloud-init znak w znak po dwoch dekodowaniach PVE",
        )

    def test_removing_the_preencoding_would_corrupt_percent_literals(self):
        """Kontrola negatywna: wariant "naprawiony" gubi literalne `%20`.

        Dowod, ze zmiana tego miejsca nie jest kosmetyczna — surowy klucz
        wprost do `--data-urlencode` po dwoch dekodowaniach PVE zamienia
        komentarz `%20` na spacje.
        """
        naive_wire = urllib.parse.quote(KEY, safe="")
        naive_after_http = urllib.parse.unquote_plus(naive_wire)
        naive_after_pve = urllib.parse.unquote(naive_after_http)

        self.assertNotEqual(
            naive_after_pve,
            KEY,
            "wariant bez prekodowania mialby zachowac klucz — hipoteza nie bylaby falszywa",
        )
        self.assertIn(
            "comment literal",
            naive_after_pve,
            "literalne %20 w komentarzu mialo zamienic sie na spacje",
        )


if __name__ == "__main__":
    unittest.main()
