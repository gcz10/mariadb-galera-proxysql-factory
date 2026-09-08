#!/usr/bin/env python3
"""Kontrakt gcache: JEDNA formula i default, ktory nie siedzi na granicy.

POWSTAL Z DWOCH DEFEKTOW (orion-r9, 2026-08-25):

1. `calc-gcache.py` byl wymieniony w ISA.md i w komentarzu szablonu, ale pliku
   NIE BYLO w repozytorium. Formula zyla wylacznie wewnatrz `probe-gcache.py`,
   czyli poznac wymagana wartosc mozna bylo dopiero po zbudowaniu klastra.

2. Szablon wysylal `gcache_size: "128M"`, czyli DOKLADNIE podloge formuly.
   Kazdy zmierzony write rate powyzej ~74 kB/s wywracal brame po budowie:
   `sigma-r9` zmierzyla 74222 B/s i przeszla, `orion-r9` 83500 B/s i padla -
   ta sama definicja, ten sam sprzet, wynik zalezny od obciazenia hypervisora.

Testy pilnuja obu rzeczy naraz: kalkulator i sonda licza TAK SAMO (inaczej
operator dostaje inna liczbe niz brama), a domyslna wartosc szablonu ma zapas
wzgledem realnych pomiarow tego laboratorium.
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CALC = REPO / "tests" / "validation" / "calc-gcache.py"
PROBE = REPO / "tests" / "lab" / "probe-gcache.py"
TEMPLATE = REPO / "clusters" / "example-cluster" / "cluster.yml"

sys.path.insert(0, str(CALC.parent))
_spec_src = CALC.read_text(encoding="utf-8")


def _calc_cli(rate, window=None):
    cmd = [sys.executable, str(CALC), "--write-rate", str(rate)]
    if window is not None:
        cmd += ["--window", str(window)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip()


class GcacheFormulaTests(unittest.TestCase):
    def test_calculator_exists_and_is_executable(self):
        """ISA.md i szablon obiecuja ten plik - ma istniec, nie byc martwym linkiem."""
        self.assertTrue(CALC.exists(), f"brak {CALC}")

    def test_measured_case_that_broke_the_gate(self):
        """83500 B/s x 30 min = 143.4 MB -> 144M. Ta liczba wywrocila orion-r9."""
        self.assertEqual(_calc_cli(83500), "144M")

    def test_floor_holds_for_idle_cluster(self):
        """Klaster bez ruchu nie dostaje mikroskopijnego bufora."""
        self.assertEqual(_calc_cli(0), "128M")
        self.assertEqual(_calc_cli(1000), "128M")

    def test_window_scales_the_result(self):
        """Dwukrotnie dluzsze okno IST to dwukrotnie wiekszy wymog."""
        self.assertEqual(_calc_cli(200000, 30), "344M")
        self.assertEqual(_calc_cli(200000, 60), "687M")

    def test_calculator_matches_the_probe_formula(self):
        """Kalkulator i brama MUSZA dawac te sama liczbe.

        Rozjazd oznaczalby, ze operator wpisuje wartosc policzona jedna droga,
        a brama odrzuca ja druga - dokladnie ten rodzaj sprzecznosci, ktory
        kosztowal dzis pietnascie minut budowy.
        """
        probe = PROBE.read_text(encoding="utf-8")
        self.assertIn("rate * IST_WINDOW_MIN * 60", probe)
        self.assertIn("max(math.ceil(gcache_bytes / (1024 * 1024)), 128)", probe)
        self.assertIn("write_rate_bytes_s * ist_window_min * 60", _spec_src)
        self.assertIn("max(math.ceil(needed / (1024 * 1024)), floor_mb)", _spec_src)

    def test_template_default_covers_reference_traffic_not_saturation(self):
        """Default szablonu nie moze siedziec na podlodze formuly.

        ZMIENIONE 2026-09-08. Poprzednia wersja nazywala 83500 B/s "najwyzszym
        pomiarem w tym laboratorium" i zadala od defaultu dwukrotnego zapasu nad
        nim. To zdanie przestalo byc prawdziwe: sonda mierzyla wtedy predkosc
        LOGOWANIA (klient wolany osobno dla kazdego zapisu), a po naprawie ten
        sam lab daje 2,19 MB/s. Test przechodzil na 26-krotnie zanizonej
        przeslance, czyli nie mierzyl tego, co obiecywal w nazwie.

        Kontrakt po korekcie: default pokrywa z zapasem RUCH REFERENCYJNY
        (100 kB/s - rzad wielkosci malego najemcy), a NIE przepustowosc
        nasycenia. Nasycenia default swiadomie nie pokrywa - to jest wlasnie
        czerwone ISC-68 z decyzji operatora (ISA.md, 2026-09-08), a nie luka w
        tym tescie. Sizing pod realny ruch: `calc-gcache.py --write-rate` z
        produkcyjnego `wsrep_replicated_bytes`.
        """
        match = re.search(r'gcache_size:\s*"(\d+)([MG])"', TEMPLATE.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "szablon nie deklaruje gcache_size")
        mb = int(match.group(1)) * (1024 if match.group(2) == "G" else 1)
        reference_rate = 100_000
        required = int(_calc_cli(reference_rate).rstrip("M"))
        self.assertGreaterEqual(
            mb, required * 2,
            f"default {mb}M ma zbyt maly zapas wobec ruchu referencyjnego "
            f"{reference_rate} B/s (wymog {required}M) - brama bedzie loteria",
        )

    def test_saturation_measurement_is_not_covered_by_the_default(self):
        """Jawny zapis stanu, ktory bramka pokazuje na czerwono.

        2_192_400 to POJEDYNCZA PROBKA nasycenia zmierzona 2026-09-08 na
        orionv15-r10, nie stala fizyczna - rozrzut miedzy przebiegami siega 3%.
        Ten test pilnuje ZGODNOSCI Z WPISEM W ISA, nie stanu floty: padnie, gdy
        default urosnie na tyle, ze pokryje te probke, i kaze wtedy zamknac
        ISC-68 zamiast trzymac wpis o "znanym braku". Nie wykryje sytuacji
        odwrotnej - realny ruch najemcy rosnacy ponad ta probke przy nietknietym
        defaulcie zostawi wpis nieaktualny, a test zielony.
        """
        match = re.search(r'gcache_size:\s*"(\d+)([MG])"', TEMPLATE.read_text(encoding="utf-8"))
        mb = int(match.group(1)) * (1024 if match.group(2) == "G" else 1)
        saturation_required = int(_calc_cli(2_192_400).rstrip("M"))
        self.assertLess(
            mb, saturation_required,
            "default pokrywa juz przepustowosc nasycenia - ISC-68 przestal byc "
            "znanym brakiem, zaktualizuj ISA.md",
        )


if __name__ == "__main__":
    unittest.main()
