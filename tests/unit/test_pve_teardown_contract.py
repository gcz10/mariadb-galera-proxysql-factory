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
printf 'ARGV:%s\\n' "$*" >> "$CURL_LOG"
for arg in "$@"; do
  case "$arg" in
    @*)
      f="${arg#@}"
      if [ -f "$f" ]; then
        printf 'FILE_CONTENT:%s:\\n' "$f" >> "$CURL_LOG"
        cat "$f" >> "$CURL_LOG"
        printf '\\n' >> "$CURL_LOG"
      fi
      ;;
  esac
done
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
  *"/qemu/"*"/status/current"*)
    # Stan maszyny pod VMID: 200 = zywa, 500 = PVE nie zna takiej VM (tak
    # odpowiada dla nieistniejacego configu). Testy podnosza VM_STATUS_CODE,
    # gdy chca sprawdzic ochrone ZYWYCH maszyn albo nierozstrzygnieta odpowiedz.
    # Wzorzec celowo wymaga `/status/current`: katalogowy `.../<vmid>/status`
    # zwraca podkatalogi i NIE jest dowodem istnienia maszyny, wiec atrapa nie
    # moze odpowiadac na oba warianty tak samo.
    printf '%s' "${VM_STATUS_CODE:-500}"
    ;;
  *access/ticket*)
    if [ -n "${TICKET_BODY:-}" ]; then
      printf '%s' "$TICKET_BODY"
    else
      cat "$CONTENT_BODY"
    fi
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

    def run(self, body, env_extra=None, confirm=True, nodes=()):
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
            ["bash", str(SCRIPT), str(self.workdir)] + list(nodes),
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

    def test_password_never_enters_process_arguments(self):
        """Haslo PVE, bilet sesyjny i CSRF nie moga trafic do argv/ps w wywolaniu curl."""
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("password@", text)
        self.assertNotIn('password=${PROXMOX_VE_PASSWORD}', text)
        self.assertNotIn('password="${PROXMOX_VE_PASSWORD}"', text)
        self.assertNotIn('PVEAuthCookie=${TICKET}', text)
        self.assertNotIn('CSRFPreventionToken: ${CSRF}', text)

    def test_password_never_leaks_into_curl_argv_behavioral(self):
        """Behawioralny dowod braku wycieku hasla, biletu i CSRF do argv (CURL_LOG)."""
        harness = TeardownHarness()
        self.addCleanup(harness.cleanup)
        secret_pass = "SUPER_SECRET_PVE_PASSWORD_SHOULD_NEVER_LEAK"
        ticket_response = '{"data":{"ticket":"PVEAuthCookie=ticket123","CSRFPreventionToken":"csrf123"}}'
        # Zwroc bilet przy POST access/ticket, a przy listingu pusta liste wolumenow
        result = harness.run(
            '{"data":[]}',
            env_extra={
                "PROXMOX_VE_API_TOKEN": "",
                "PROXMOX_VE_USERNAME": "root@pam",
                "PROXMOX_VE_PASSWORD": secret_pass,
                "TICKET_BODY": ticket_response,
            },
        )
        self.assertEqual(result.returncode, 0, f"Teardown powinien zakonczyc sie sukcesem: {result.stderr}")
        self.assertNotIn(secret_pass, result.stdout)
        self.assertNotIn(secret_pass, result.stderr)
        self.assertNotIn("ticket123", result.stdout)
        self.assertNotIn("ticket123", result.stderr)
        self.assertNotIn("csrf123", result.stdout)
        self.assertNotIn("csrf123", result.stderr)

        logged = harness.requested_urls()
        argv_lines = "\n".join(
            line for line in logged.splitlines() if line.startswith("ARGV:")
        )
        self.assertNotIn(
            secret_pass,
            argv_lines,
            "PROXMOX_VE_PASSWORD pojawilo sie w argv wywolania curl!",
        )
        self.assertNotIn(
            "ticket123",
            argv_lines,
            "PVEAuthCookie ticket pojawil sie w argv wywolania curl!",
        )
        self.assertNotIn(
            "csrf123",
            argv_lines,
            "CSRFPreventionToken pojawil sie w argv wywolania curl!",
        )
        # Dowod pozytywny: bilet i CSRF zostaly przekazane w pliku naglowka
        self.assertIn("Cookie: PVEAuthCookie=ticket123", logged)
        self.assertIn("CSRFPreventionToken: csrf123", logged)

    def test_protected_infra_disk_preserved_during_teardown(self):
        """Ochrona danych: dysk danych maszyny z role:infra / delete_unreferenced_disks_on_destroy:false NIE moze zostac usuniety."""
        harness = TeardownHarness()
        self.addCleanup(harness.cleanup)
        custom_tf = """#!/bin/sh
case "$*" in
  *output*) printf '{"x12mon":{"vmid":10035,"role":"infra","delete_unreferenced_disks_on_destroy":false}}' ;;
esac
exit 0
"""
        _write_executable(harness.bindir / "terraform", custom_tf)
        volumes_json = '{"data":[{"volid":"local-zfs:vm-10035-disk-0"},{"volid":"local-zfs:vm-10035-cloudinit"}]}'
        result = harness.run(volumes_json)
        self.assertEqual(result.returncode, 0, f"Teardown powinien zakonczyc sie sukcesem: {result.stderr}")
        self.assertIn("zachowano chroniony dysk danych: local-zfs:vm-10035-disk-0", result.stderr)
        self.assertIn("usunieto sierote: local-zfs:vm-10035-cloudinit", result.stdout)
        logged = harness.requested_urls()
        self.assertNotIn("local-zfs:vm-10035-disk-0", logged)
        self.assertIn("local-zfs:vm-10035-cloudinit", logged)


TWO_NODE_TERRAFORM = """#!/bin/sh
case "$*" in
  *output*) printf '{"target":{"vmid":991,"role":"galera"},"neighbor":{"vmid":992,"role":"galera"}}' ;;
esac
exit 0
"""

NEIGHBOR_ONLY_TERRAFORM = """#!/bin/sh
case "$*" in
  *output*) printf '{"neighbor":{"vmid":992,"role":"galera"}}' ;;
esac
exit 0
"""

TWO_NODE_VOLUMES = (
    '{"data":[{"volid":"local-zfs:vm-991-cloudinit"},'
    '{"volid":"local-zfs:vm-992-cloudinit"}]}'
)

# Stan terraform juz pusty (po zakonczonym destroy) — lista celow moze przyjsc
# wylacznie z pliku wznowienia.
EMPTY_OUTPUT_TERRAFORM = """#!/bin/sh
case "$*" in
  *output*) printf '{}' ;;
esac
exit 0
"""


class PveTeardownResumeScopeTests(unittest.TestCase):
    """Wznowienie po przerwanym destroy nie moze wyjsc poza wskazany zakres.

    ZMIERZONE 2026-09-12 na atrapach API: skrypt zapisywal liste VMID PRZED
    bramka potwierdzenia, a wznowienie adoptowalo ja w CALOSCI, takze przy
    wywolaniu per-node. Teardown jednej maszyny wysylal wtedy DELETE na
    wolumeny sasiada, ktory nadal zyl i nadal byl w stanie terraform.
    """

    def setUp(self):
        self.harness = TeardownHarness()
        self.addCleanup(self.harness.cleanup)
        _write_executable(self.harness.bindir / "terraform", TWO_NODE_TERRAFORM)
        self.cache = self.harness.workdir / ".teardown-vmids"

    def test_refused_run_leaves_no_resume_file(self):
        result = self.harness.run(TWO_NODE_VOLUMES, confirm=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(CONFIRM_GATE, result.stderr)
        self.assertFalse(
            self.cache.exists(),
            "odmowa potwierdzenia zostawila liste VMID do pozniejszego wznowienia",
        )

    def test_resume_skips_nodes_outside_the_requested_scope(self):
        # Przerwany teardown calego roota: DELETE odrzucony, wiec skrypt konczy
        # sie bledem i zostawia plik wznowienia z OBOMA wezlami.
        interrupted = self.harness.run(
            TWO_NODE_VOLUMES, env_extra={"DELETE_CODE": "403"}
        )
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertTrue(self.cache.exists())

        # Wznowienie dotyczy JEDNEJ maszyny, a sasiad nadal jest w stanie.
        _write_executable(self.harness.bindir / "terraform", NEIGHBOR_ONLY_TERRAFORM)
        self.harness.curl_log.write_text("", encoding="utf-8")
        resumed = self.harness.run(TWO_NODE_VOLUMES, nodes=["target"])

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        logged = self.harness.requested_urls()
        self.assertIn("local-zfs:vm-991-cloudinit", logged)
        self.assertNotIn(
            "local-zfs:vm-992-cloudinit",
            logged,
            "wznowienie skasowalo wolumen maszyny spoza wskazanego zakresu",
        )

    def test_resume_refuses_cache_without_node_names(self):
        """Plik sprzed poprawki nie wie, do ktorej maszyny naleza VMID."""
        self.cache.write_text("991\n992\n", encoding="utf-8")
        _write_executable(self.harness.bindir / "terraform", NEIGHBOR_ONLY_TERRAFORM)
        result = self.harness.run(TWO_NODE_VOLUMES, nodes=["target"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("starym formacie", result.stderr)
        logged = self.harness.requested_urls()
        self.assertNotIn("local-zfs:vm-991-cloudinit", logged)
        self.assertNotIn("local-zfs:vm-992-cloudinit", logged)


PARTIAL_DESTROY_TERRAFORM = """#!/bin/sh
case "$*" in
  *output*)
    # Run 1: w stanie sa OBJIE maszyny. Run 2 (po przerwanym destroy): zostala
    # tylko ocalala — dokladnie tak zachowuje sie terraform po kasowaniu polowy.
    if [ -f "$PARTIAL_PROGRESS" ]; then
      printf '{"neighbor":{"vmid":992,"role":"galera"}}'
    else
      printf '{"target":{"vmid":991,"role":"galera"},"neighbor":{"vmid":992,"role":"galera"}}'
    fi
    ;;
  *destroy*)
    if [ ! -f "$PARTIAL_PROGRESS" ]; then
      # Przerwanie PO skasowaniu pierwszej maszyny, PRZED druga.
      touch "$PARTIAL_PROGRESS"
      exit 1
    fi
    ;;
esac
exit 0
"""


class PveTeardownPartialDestroyTests(unittest.TestCase):
    """Wznowienie po CZESCIOWYM destroy nie moze zapomniec ofiar run-1.

    ZMIERZONE 2026-09-14 na atrapach API: `terraform destroy` usunal pierwsza
    maszyne i padl na drugiej, wiec kolejny przebieg czytal z outputu juz tylko
    ocalala maszyne, nadpisywal nia plik wznowienia, sprzatal tylko ja i
    konczyl "usunietych sierot: 0" z kodem 0 (bez banera bledu). Wolumen
    pierwszej ofiary zostawal na ZFS i wywalal nastepny `apply` na tym VMID.
    """

    def setUp(self):
        self.harness = TeardownHarness()
        self.addCleanup(self.harness.cleanup)
        _write_executable(self.harness.bindir / "terraform", PARTIAL_DESTROY_TERRAFORM)
        self.progress = self.harness.sandbox / "partial-destroy"
        self.cache = self.harness.workdir / ".teardown-vmids"

    def run_teardown(self):
        return self.harness.run(
            TWO_NODE_VOLUMES, env_extra={"PARTIAL_PROGRESS": str(self.progress)}
        )

    def test_retry_still_sweeps_the_victim_of_the_interrupted_run(self):
        interrupted = self.run_teardown()
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertTrue(self.cache.exists())
        self.assertIn("target:991:no", self.cache.read_text(encoding="utf-8"))

        self.harness.curl_log.write_text("", encoding="utf-8")
        resumed = self.run_teardown()

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        logged = self.harness.requested_urls()
        self.assertIn(
            "local-zfs:vm-991-cloudinit",
            logged,
            "retry zapomnial sieroty maszyny skasowanej w przerwanym przebiegu",
        )
        self.assertIn("local-zfs:vm-992-cloudinit", logged)
        self.assertFalse(self.cache.exists(), "plik wznowienia zostal po sprzataniu")


class PveTeardownLiveMachineGuardTests(unittest.TestCase):
    """Sierota to wolumen BEZ maszyny. VMID wraca do obiegu, wiec wolumen
    `vm-<vmid>-*` moze nalezec do maszyny utworzonej po przerwanym przebiegu —
    skasowanie jej dyskow jest nieodwracalne."""

    def setUp(self):
        self.harness = TeardownHarness()
        self.addCleanup(self.harness.cleanup)

    def test_volumes_of_a_live_machine_are_preserved(self):
        result = self.harness.run(ORPHAN_LISTING, env_extra={"VM_STATUS_CODE": "200"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ZACHOWANO", result.stderr)
        self.assertNotIn("usunieto sierote", result.stdout)
        self.assertNotIn("local-zfs:vm-9999-cloudinit", self.harness.requested_urls())
        # Zakotwiczenie ENDPOINTU: `/qemu/<vmid>/status` (katalogowy) zwraca
        # podkatalogi, wiec jego 200 nie dowodzi istnienia maszyny — bramka musi
        # pytac o lisc API `status/current`.
        self.assertIn("/qemu/9999/status/current", self.harness.requested_urls())

    def test_unresolvable_machine_state_refuses_to_delete(self):
        # Odpowiedz, ktora nie rozstrzyga (np. 401 po utracie poswiadczen), nie
        # moze byc czytana jako "maszyny nie ma": nierozstrzygniete kasowanie
        # jest nieodwracalne.
        result = self.harness.run(ORPHAN_LISTING, env_extra={"VM_STATUS_CODE": "401"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Odmawiam kasowania wolumenow", result.stderr)
        self.assertNotIn("usunieto sierote", result.stdout)
        self.assertNotIn("local-zfs:vm-9999-cloudinit", self.harness.requested_urls())
        self.assertIn("/qemu/9999/status/current", self.harness.requested_urls())


class PveTeardownScopePreservedResumeTests(unittest.TestCase):
    """Wywolanie PER-NODE nie jest wlascicielem calej listy wznowienia.

    Plik wznowienia jest jedynym miejscem, ktore pamieta ofiary przerwanego
    teardownu CALEGO roota (`terraform output` ich juz nie zwroci). Gdyby
    przebieg jednego wezla nadpisal go wlasnym zakresem i skasowal po sukcesie,
    wolumeny pozostalych ofiar zostalyby zapomniane bez ostrzezenia.
    """

    def setUp(self):
        self.harness = TeardownHarness()
        self.addCleanup(self.harness.cleanup)
        _write_executable(self.harness.bindir / "terraform", TWO_NODE_TERRAFORM)
        self.cache = self.harness.workdir / ".teardown-vmids"

    def test_scoped_run_keeps_the_other_nodes_unfinished_targets(self):
        # Przerwany teardown calego roota: plik wznowienia zna OBIE maszyny.
        interrupted = self.harness.run(TWO_NODE_VOLUMES, env_extra={"DELETE_CODE": "403"})
        self.assertNotEqual(interrupted.returncode, 0)
        cache_text = self.cache.read_text(encoding="utf-8")
        self.assertIn("target:991:no", cache_text)
        self.assertIn("neighbor:992:no", cache_text)

        # Przebieg per-node dla jednego wezla nie moze skasowac cudzych celow.
        self.harness.curl_log.write_text("", encoding="utf-8")
        scoped = self.harness.run(TWO_NODE_VOLUMES, nodes=["target"])

        self.assertEqual(scoped.returncode, 0, scoped.stderr)
        self.assertIn("local-zfs:vm-991-cloudinit", self.harness.requested_urls())
        self.assertNotIn(
            "local-zfs:vm-992-cloudinit",
            self.harness.requested_urls(),
            "przebieg per-node skasowal wolumen maszyny spoza swojego zakresu",
        )
        self.assertTrue(self.cache.exists(), "plik wznowienia zgubil cele innego wezla")
        kept = self.cache.read_text(encoding="utf-8")
        self.assertIn("neighbor:992:no", kept)
        self.assertNotIn("target:991:no", kept, "cel wlasnego zakresu zostal po sprzataniu")

    def test_out_of_scope_entries_are_not_duplicated_when_output_is_empty(self):
        """Plik jest przepisywany od zera, nie dopisywany.

        Dopisanie wpisow spoza zakresu do NIEOSKROCONEGO pliku zdublowaloby je —
        sa w nim juz te same wpisy, ktore przed chwila czytalismy.
        """
        interrupted = self.harness.run(TWO_NODE_VOLUMES, env_extra={"DELETE_CODE": "403"})
        self.assertNotEqual(interrupted.returncode, 0)

        # Zakres nieobecny w pliku i pusty `terraform output`: nic do sprzatania,
        # ale cudze cele musza przetrwac bez zmian.
        _write_executable(self.harness.bindir / "terraform", EMPTY_OUTPUT_TERRAFORM)
        result = self.harness.run(TWO_NODE_VOLUMES, nodes=["ghost"])

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.cache.read_text(encoding="utf-8").split()
        self.assertEqual(
            sorted(lines),
            ["neighbor:992:no", "target:991:no"],
            f"wpisy wznowienia zostaly zdublowane albo zgubione: {lines}",
        )


if __name__ == "__main__":
    unittest.main()
