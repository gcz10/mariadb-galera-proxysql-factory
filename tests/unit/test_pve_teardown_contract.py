#!/usr/bin/env python3
"""Kontrakt `terraform/pve-teardown.sh` wobec PVE API.

POWSTAL PO REALNEJ AWARII (teardown n15, 2026-08-20). Trzy defekty w jednym
przebiegu, wszystkie na sciezce sprzatania sierot ZFS:

  1. `make infra-teardown` przechodzil `pve_auth_guard` z Makefile (token API
     ALBO haslo), po czym skrypt padal na twardym `PROXMOX_VE_USERNAME:?`.
     Operator uwierzytelniajacy sie tokenem — czyli tak, jak provider
     bpg/proxmox — nie mial jak wykonac skodyfikowanej sciezki.
  2. `PROXMOX_VE_ENDPOINT` konczy sie ukosnikiem, wiec skrypt sklejal
     `//api2/json/...`; PVE odpowiadalo HTTP 500 "no such file". Terraform ten
     sam endpoint toleruje, wiec defekt byl niewidoczny az do sprzatania.
  3. Odpowiedz bez JSON-a byla parsowana per VMID z `2>/dev/null`, a lista
     wolumenow nie miala ZADNEJ kontroli bledu. Skutek: cztery tracebacki,
     komunikat "teardown zakonczony (usunietych sierot: 0)" i kod wyjscia 0.
     Cicha porazka sprzatania ujawnia sie dopiero przy nastepnym `apply` na tym
     samym VMID: `zfs error: dataset already exists`.

Dokumentacja Proxmox VE API, sekcja "API Tokens": naglowek ma postac
`Authorization: PVEAPIToken=USER@REALM!TOKENID=UUID`, tokeny NIE wymagaja CSRF
przy POST/PUT/DELETE, a naglowek nalezy podawac plikiem (`-H @plik`), bo argv
widzi kazdy uzytkownik systemu.

Testy uruchamiaja skrypt NAPRAWDE. Cast zatrzymuje sie na bramce potwierdzenia,
reszta podstawia atrapy `curl` i `terraform` na PATH — zaden `terraform destroy`
ani zadne zadanie HTTP nie opuszcza maszyny.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "terraform" / "pve-teardown.sh"

CONFIRM_GATE = "brak potwierdzenia"
SUCCESS_BANNER = "teardown zakonczony"
ENDPOINT_WITH_SLASH = "https://192.0.2.1:8006/"

FAKE_TERRAFORM = """#!/bin/sh
case "$*" in
  *output*) printf '{"zzfake1":{"vmid":9999}}' ;;
esac
exit 0
"""

# Atrapa zapisuje argv do CURL_LOG, a tresc bierze z CONTENT_BODY. Obsluguje
# oba ksztalty wywolania (przechwycenie stdout oraz `-o plik -w %{http_code}`),
# zeby test nie zakladal implementacji, tylko obserwowalne zachowanie.
FAKE_CURL = """#!/bin/sh
printf '%s\\n' "$*" >> "$CURL_LOG"
OUT=""
prev=""
for a in "$@"; do
  [ "$prev" = "-o" ] && OUT="$a"
  prev="$a"
done
case "$*" in
  *"-X DELETE"*)
    [ -n "$OUT" ] && : > "$OUT"
    printf '%s' "${DELETE_CODE:-200}"
    ;;
  *)
    if [ -n "$OUT" ]; then
      cp "$CONTENT_BODY" "$OUT"
      printf '%s' "${CONTENT_CODE:-200}"
    else
      cat "$CONTENT_BODY"
    fi
    ;;
esac
exit 0
"""

ORPHAN_LISTING = '{"data":[{"volid":"local-zfs:vm-9999-cloudinit"}]}'


def _write_executable(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class TeardownHarness:
    """Uruchamia skrypt na atrapie katalogu terraform z podstawionym PATH."""

    def __init__(self):
        self.workdir = Path(
            tempfile.mkdtemp(dir=str(REPO / "terraform"), prefix=".contracttest-")
        )
        self.sandbox = Path(tempfile.mkdtemp(prefix="pve-teardown-sandbox-"))
        Path(self.workdir, "main.tf").write_text("# atrapa\n", encoding="utf-8")
        self.bindir = self.sandbox / "bin"
        self.bindir.mkdir()
        _write_executable(self.bindir / "terraform", FAKE_TERRAFORM)
        _write_executable(self.bindir / "curl", FAKE_CURL)
        self.curl_log = self.sandbox / "curl.log"
        self.curl_log.write_text("", encoding="utf-8")
        self.content_body = self.sandbox / "content.json"

    def run(self, body, env_extra=None, confirm=True):
        self.content_body.write_text(body, encoding="utf-8")
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("PROXMOX_VE_")
        }
        env.update(
            {
                "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
                "PROXMOX_VE_ENDPOINT": ENDPOINT_WITH_SLASH,
                "PROXMOX_VE_API_TOKEN": "root@pam!isa=00000000-0000-0000-0000-000000000000",
                "CURL_LOG": str(self.curl_log),
                "CONTENT_BODY": str(self.content_body),
            }
        )
        if confirm:
            env["CONFIRM_DESTROY"] = str(self.workdir)
        env.update(env_extra or {})
        return subprocess.run(
            ["bash", str(SCRIPT), str(self.workdir)],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=str(REPO),
        )

    def requested_urls(self):
        return self.curl_log.read_text(encoding="utf-8")

    def cleanup(self):
        shutil.rmtree(self.workdir, ignore_errors=True)
        shutil.rmtree(self.sandbox, ignore_errors=True)


class PveTeardownApiUrlTests(unittest.TestCase):
    def setUp(self):
        self.harness = TeardownHarness()
        self.addCleanup(self.harness.cleanup)

    def test_endpoint_trailing_slash_never_doubles_api_path(self):
        result = self.harness.run(ORPHAN_LISTING)
        self.assertNotIn(
            "//api2",
            self.harness.requested_urls(),
            "ukosnik na koncu PROXMOX_VE_ENDPOINT daje //api2 i HTTP 500 z PVE",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usunieto sierote", result.stdout)


class PveTeardownCleanupFailsLoudTests(unittest.TestCase):
    def setUp(self):
        self.harness = TeardownHarness()
        self.addCleanup(self.harness.cleanup)

    def test_non_json_volume_listing_is_fatal(self):
        result = self.harness.run(
            "<html>500 Internal Server Error</html>",
            env_extra={"CONTENT_CODE": "500"},
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "odpowiedz bez JSON-a nie moze konczyc sie sukcesem",
        )
        self.assertNotIn(
            SUCCESS_BANNER,
            result.stdout,
            "skrypt nie moze oglaszac zakonczenia sprzatania, ktore sie nie odbylo",
        )

    def test_failed_volume_delete_is_fatal(self):
        result = self.harness.run(
            ORPHAN_LISTING,
            env_extra={"DELETE_CODE": "403"},
        )
        self.assertNotEqual(
            result.returncode,
            0,
            "nieusunieta sierota wywali nastepny apply — musi byc bledem teraz",
        )


class PveTeardownCredentialContractTests(unittest.TestCase):
    """Poswiadczenia: token ALBO haslo, jak `pve_auth_guard` w Makefile."""

    def run_guard(self, env_extra):
        workdir = tempfile.mkdtemp(dir=str(REPO / "terraform"), prefix=".authtest-")
        try:
            Path(workdir, "main.tf").write_text("# atrapa\n", encoding="utf-8")
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("PROXMOX_VE_")
            }
            env.update(env_extra)
            return subprocess.run(
                ["bash", str(SCRIPT), workdir],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
                cwd=str(REPO),
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_api_token_alone_reaches_confirmation_gate(self):
        result = self.run_guard(
            {
                "PROXMOX_VE_ENDPOINT": ENDPOINT_WITH_SLASH,
                "PROXMOX_VE_API_TOKEN": "root@pam!isa=00000000-0000-0000-0000-000000000000",
            }
        )
        self.assertIn(CONFIRM_GATE, result.stderr)
        self.assertNotIn("PROXMOX_VE_USERNAME", result.stderr)

    def test_username_and_password_remain_supported(self):
        result = self.run_guard(
            {
                "PROXMOX_VE_ENDPOINT": ENDPOINT_WITH_SLASH,
                "PROXMOX_VE_USERNAME": "root@pam",
                "PROXMOX_VE_PASSWORD": "irrelevant",
            }
        )
        self.assertIn(CONFIRM_GATE, result.stderr)

    def test_no_credentials_fails_closed_before_confirmation_gate(self):
        result = self.run_guard({"PROXMOX_VE_ENDPOINT": ENDPOINT_WITH_SLASH})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(CONFIRM_GATE, result.stderr)

    def test_token_never_enters_process_arguments(self):
        """Token jest dlugowieczny; w argv widzi go kazdy przez `ps`."""
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("-H @", text)
        self.assertNotIn('-H "Authorization: PVEAPIToken=${PROXMOX_VE_API_TOKEN}"', text)


if __name__ == "__main__":
    unittest.main()
