#!/usr/bin/env python3
"""Polityka MinIO musi dopuszczac klucz znacznika restore drillu.

POWSTAL PO REALNEJ AWARII KONTRAKTU (n14, 2026-08-19; objaw zlapany na n13):
kod znacznika (PR #52) zapisuje `drill-state/<cluster>.json`, ale polityka
poswiadczenia runnera obejmowala wylacznie prefiks `galera-<cluster>-*` oraz
`galera-backup-owner.json`. Drill dostawal `AccessDenied` i most swiezosci
nigdy nie powstawal - przy czym backup dzialal, wiec nic nie krzyczalo.

Te testy pilnuja obu stron kontraktu jednoczesnie:
  1. polityka dopuszcza GetObject i PutObject na DOKLADNIE tym kluczu, ktory
     buduje kod (`DRILL_MARKER_S3_PREFIX` + nazwa klastra),
  2. uprawnienie jest waskie - nie jest to wildcard na caly bucket,
  3. dotychczasowe statementy (prefiks kopii, plik ownera) pozostaja nietkniete,
     zeby ta zmiana nie rozszerzyla uprawnien po cichu.

Test jest falsyfikowalny: usuniecie statementu znacznika z szablonu albo zmiana
prefiksu w kodzie natychmiast go wywala.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "roles" / "galera_backup" / "files"))

TEMPLATE = REPO / "roles" / "galera_backup" / "templates" / "minio-policy.json.j2"
BUCKET = "n14-galera-backups"
CLUSTER = "newclaude14-r9"


def render_policy():
    """Renderuje szablon polityki dla znanego klastra i zwraca sparsowany JSON."""
    import json

    from jinja2 import Template

    out = Template(TEMPLATE.read_text(encoding="utf-8")).render(
        backup={"s3": {"bucket": BUCKET}},
        cluster={"name": CLUSTER},
    )
    return json.loads(out)


def allowed_actions(policy, resource_arn):
    """Zbiera akcje dozwolone dla dokladnie tego ARN-u (bez dopasowania wildcard)."""
    actions = set()
    for stmt in policy["Statement"]:
        if stmt.get("Effect") != "Allow":
            continue
        if resource_arn in stmt.get("Resource", []):
            actions.update(stmt.get("Action", []))
    return actions


class TestMarkerKeyIsPermitted(unittest.TestCase):
    def test_policy_renders_as_valid_json(self):
        policy = render_policy()
        self.assertEqual(policy["Version"], "2012-10-17")
        self.assertGreaterEqual(len(policy["Statement"]), 4)

    def test_marker_key_allows_read_and_write(self):
        """Klucz bierzemy z METODY backendu, nie sklejamy recznie - inaczej test
        przepisalby blad, zamiast go zlapac (pierwsza wersja tego testu wlasnie
        zgubila ukosnik i zielenila sie na nieistniejacym kluczu)."""
        from galera_backup.storage.s3 import S3Backend

        key = S3Backend._drill_marker_key(
            type("Stub", (), {"cluster_name": CLUSTER})()
        )
        arn = f"arn:aws:s3:::{BUCKET}/{key}"
        actions = allowed_actions(render_policy(), arn)
        self.assertIn("s3:GetObject", actions, f"brak odczytu znacznika dla {arn}")
        self.assertIn("s3:PutObject", actions, f"brak zapisu znacznika dla {arn}")

    def test_marker_permission_is_not_a_bucket_wildcard(self):
        """Waskie uprawnienie: zaden statement nie otwiera calego bucketa na zapis."""
        policy = render_policy()
        for stmt in policy["Statement"]:
            if "s3:PutObject" not in stmt.get("Action", []):
                continue
            for res in stmt.get("Resource", []):
                self.assertNotEqual(
                    res,
                    f"arn:aws:s3:::{BUCKET}/*",
                    "zapis na caly bucket - uprawnienie za szerokie",
                )
                self.assertNotEqual(res, f"arn:aws:s3:::{BUCKET}", "zapis na bucket jako obiekt")


class TestExistingGrantsSurvive(unittest.TestCase):
    def test_backup_prefix_still_writable(self):
        arn = f"arn:aws:s3:::{BUCKET}/galera-{CLUSTER}-*"
        actions = allowed_actions(render_policy(), arn)
        self.assertIn("s3:PutObject", actions)
        # `s3:DeleteObject` CELOWO tu nie ma: prawo kasowania kopii przeszlo do
        # osobnego poswiadczenia retencji, bo ta polityka lezy na kazdym wezle
        # Galery. Kontrakt rozdzialu: tests/unit/test_backup_delete_separation.py.
        self.assertNotIn("s3:DeleteObject", actions)

    def test_owner_file_still_readable(self):
        arn = f"arn:aws:s3:::{BUCKET}/galera-backup-owner.json"
        self.assertIn("s3:GetObject", allowed_actions(render_policy(), arn))

    def test_bucket_listing_still_allowed(self):
        arn = f"arn:aws:s3:::{BUCKET}"
        self.assertIn("s3:ListBucket", allowed_actions(render_policy(), arn))


if __name__ == "__main__":
    unittest.main()
