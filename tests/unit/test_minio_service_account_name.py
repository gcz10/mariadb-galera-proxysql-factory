"""Contract of the `minio_service_account_name` filter (ISP-04).

MinIO odrzuca nazwy kont serwisowych dluzsze niz 32 znaki. Nazwa musi byc
deterministyczna: ta sama funkcja nadaje ja przy provision i odnajduje ja
przy derejestracji — rozjazd oznaczalby niedobrane klucze.
"""

import importlib.util
import json
import unittest
from pathlib import Path

_FILTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "roles" / "galera_backup" / "filter_plugins" / "minio_access_keys.py"
)
_spec = importlib.util.spec_from_file_location("minio_access_keys", _FILTER_PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class ServiceAccountNameTests(unittest.TestCase):
    def test_short_candidate_passes_through(self):
        candidate = "galera-backup-prune-orionv8-r9"
        self.assertEqual(
            mod.minio_service_account_name(candidate),
            candidate,
        )

    def test_long_candidate_is_bounded_to_32_chars(self):
        candidate = "galera-backup-prune-cassiopeiav8-r9"
        self.assertGreater(len(candidate), 32)
        name = mod.minio_service_account_name(candidate)
        self.assertLessEqual(len(name), 32)

    def test_long_name_is_deterministic(self):
        candidate = "galera-backup-prune-cassiopeiav8-r9"
        self.assertEqual(
            mod.minio_service_account_name(candidate),
            mod.minio_service_account_name(candidate),
        )

    def test_bounded_names_of_distinct_candidates_do_not_collide(self):
        names = {
            mod.minio_service_account_name(f"galera-backup-prune-cassiopeiav8-r{rev}")
            for rev in range(8)
        }
        self.assertEqual(len(names), 8)

    def test_round_trip_with_named_filter(self):
        # The provision path names the account; the deregister path must find
        # it again through `minio_access_keys_named`.
        candidate = "galera-backup-prune-cassiopeiav8-r9"
        name = mod.minio_service_account_name(candidate)
        account = {
            "status": "success",
            "accessKey": "AK123",
            "name": name,
        }
        self.assertEqual(
            mod.minio_access_keys_named([json.dumps(account)], name),
            ["AK123"],
        )

    def test_rejects_empty_or_non_string_candidate(self):
        for bad in ("", None, 123):
            with self.subTest(candidate=bad):
                with self.assertRaises(ValueError):
                    mod.minio_service_account_name(bad)


if __name__ == "__main__":
    unittest.main()
