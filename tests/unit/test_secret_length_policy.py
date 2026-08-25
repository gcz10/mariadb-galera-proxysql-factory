#!/usr/bin/env python3
"""Prog dlugosci sekretow ma JEDNO zrodlo i jeden udokumentowany wyjatek.

POWSTAL Z ROZJAZDU. Czesc asercji wymagala `length > 0`, czyli niepustosci —
haslo "x" przechodzilo bramke i padalo dopiero na API PMM albo przy logowaniu
do bazy, komunikatem bez zwiazku z przyczyna. Inne miejsca mialy wpisane `12`
na sztywno, wiec zmiana progu wymagalaby znalezienia wszystkich.

Wyjatek dotyczy VRRP: keepalived uzywa WYLACZNIE osmiu pierwszych znakow
`auth_pass` (dokumentacja upstreamu), wiec dluzsze haslo nie jest mocniejsze,
tylko mylace. Tam gorna granica jest bramka.

Kontrakt: zaden plik nie wpisuje progu liczba, a wszystkie asercje sekretow
odwoluja sie do polityki.
"""
import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
POLICY = REPO / "playbooks" / "vars" / "secret_policy.yml"
SEARCH_ROOTS = (REPO / "playbooks", REPO / "roles")

# Sekrety objete progiem. VRRP celowo poza lista — ma wlasna, gorna granice.
GUARDED = (
    "pmm_admin_password",
    "pmm_monitor_password",
    "minio_root_password",
    "proxysql_admin_password",
)


def yaml_files():
    for root in SEARCH_ROOTS:
        for path in root.rglob("*.yml"):
            yield path


class SecretLengthPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))

    def test_policy_declares_both_values(self):
        self.assertGreaterEqual(self.policy["isa_min_secret_length"], 12)
        self.assertEqual(
            self.policy["isa_vrrp_auth_length"],
            8,
            "VRRP zuzywa dokladnie 8 znakow — zmiana tej liczby wymaga zrodla w dokumentacji",
        )

    def test_no_secret_assertion_accepts_merely_non_empty(self):
        """`length > 0` na sekrecie to bramka, ktora przepuszcza haslo 'x'."""
        offenders = []
        for path in yaml_files():
            text = path.read_text(encoding="utf-8")
            for secret in GUARDED:
                for match in re.finditer(rf"{secret}\s*\|\s*length\s*>\s*0", text):
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(REPO)}:{line} {secret}")
        self.assertEqual(offenders, [], f"asercje bez progu dlugosci: {offenders}")

    def test_no_file_hardcodes_the_threshold(self):
        """Prog ma pochodzic z polityki, nie z liczby wpisanej w asercji."""
        offenders = []
        for path in yaml_files():
            if path == POLICY:
                continue
            text = path.read_text(encoding="utf-8")
            for secret in GUARDED:
                for match in re.finditer(rf"{secret}\s*\|\s*length\s*>=\s*(\d+)", text):
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(REPO)}:{line} = {match.group(1)}")
        self.assertEqual(offenders, [], f"prog wpisany liczba zamiast z polityki: {offenders}")

    def test_vrrp_password_has_an_upper_bound(self):
        text = (REPO / "playbooks" / "f8_keepalived.yml").read_text(encoding="utf-8")
        self.assertIn("keepalived_auth_pass | length <= isa_vrrp_auth_length", text)

    def test_policy_is_loaded_where_it_is_used(self):
        """Asercja odwolujaca sie do polityki bez jej zaladowania to cicha domyslka."""
        missing = []
        for path in yaml_files():
            text = path.read_text(encoding="utf-8")
            uses = "isa_min_secret_length" in text or "isa_vrrp_auth_length" in text
            if not uses or path == POLICY:
                continue
            if path.parts[-3:-1] == ("roles", "tasks"):
                continue  # rola dostaje zmienna od playbooka, ma wlasna domyslke
            if "vars/secret_policy.yml" not in text and "roles/" not in str(path):
                missing.append(str(path.relative_to(REPO)))
        self.assertEqual(missing, [], f"uzywaja polityki, ale jej nie ladują: {missing}")


if __name__ == "__main__":
    unittest.main()
