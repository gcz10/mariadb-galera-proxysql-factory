#!/usr/bin/env python3
"""Most swiezosci restore drillu przez backend kopii.

POWSTAL PO REALNEJ AWARII KONTRAKTU (n13, 2026-08-18).
Restore drill biegnie na IZOLOWANYM hoscie `restore`, ktorego nikt nie scrapuje.
Jego sukces nie mial jak dotrzec do metryki czytanej przez alert ISC-47
"Restore drill stale", wiec alert mierzyl date ostatniego uruchomienia Ansible.

Naprawa: drill zostawia znacznik w backendzie kopii (jedyny kanal, do ktorego
uwierzytelniaja sie OBA hosty), a nocny backup — biegnacy na scrapowanym hoscie
schedulera — przepisuje go do textfile collectora.

Testy pilnuja czterech wlasnosci, bez ktorych most jest teatrem:
  1. znacznik przezywa retencje (`prune`) i nie jest brany za kopie (`fetch_latest`),
  2. cudzy albo niezgodny formatem znacznik NIE podnosi metryki,
  3. publikacja emituje dokladnie te nazwe metryki, ktorej szuka regula ISC-47,
  4. brak znacznika daje uczciwe 0, a nie awarie backupu.
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "roles" / "galera_backup" / "files"))

from galera_backup.errors import BackupError  # noqa: E402
from galera_backup.pipeline import publish_drill_freshness  # noqa: E402
from galera_backup.storage.artifacts import (  # noqa: E402
    build_drill_marker,
    drill_marker_unixtime,
)
from galera_backup.storage.filesystem import FilesystemBackend  # noqa: E402

CLUSTER = "newclaude13-r9"


def _backend(root: Path) -> FilesystemBackend:
    backend = FilesystemBackend(root, "", CLUSTER)
    # Kontrola tozsamosci montowania nie dotyczy katalogu tymczasowego w tescie.
    backend._verify_mount_identity = lambda: None  # type: ignore[method-assign]
    return backend


class TestDrillMarkerSurvivesStorage(unittest.TestCase):
    def test_marker_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _backend(Path(tmp))
            self.assertIsNone(b.read_drill_marker(), "pusty backend musi dac None, nie wyjatek")
            b.write_drill_marker(build_drill_marker(CLUSTER, 1787019064, "galera-x", 3))
            self.assertEqual(b.read_drill_marker()["last_success_unixtime"], 1787019064)

    def test_retention_does_not_delete_the_marker(self):
        """Znacznik lezy poza prefiksem `galera-<cluster>-`, wiec prune go nie widzi."""
        with tempfile.TemporaryDirectory() as tmp:
            b = _backend(Path(tmp))
            b.write_drill_marker(build_drill_marker(CLUSTER, 1787019064, "galera-x", 3))
            # Retencja 0 dni skasowalaby KAZDA kopie; znacznik ma przezyc.
            b.prune(datetime.now(timezone.utc), 0)
            self.assertIsNotNone(
                b.read_drill_marker(),
                "retencja skasowala znacznik drillu — most swiezosci zniknie po pierwszym pruningu",
            )

    def test_marker_is_not_mistaken_for_a_backup(self):
        """`fetch_latest` iteruje po katalogach kopii; plik znacznika ma byc niewidoczny."""
        with tempfile.TemporaryDirectory() as tmp:
            b = _backend(Path(tmp))
            b.write_drill_marker(build_drill_marker(CLUSTER, 1787019064, "galera-x", 3))
            with self.assertRaises(BackupError) as ctx:
                b.fetch_latest(Path(tmp) / "work")
            self.assertIn("No complete backups found", str(ctx.exception))

    def test_corrupt_marker_is_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _backend(Path(tmp))
            b.write_drill_marker(build_drill_marker(CLUSTER, 1787019064, "galera-x", 3))
            (Path(tmp) / CLUSTER / "drill-state.json").write_text("{ niepelny", encoding="utf-8")
            with self.assertRaises(BackupError):
                b.read_drill_marker()


class TestDrillMarkerTrust(unittest.TestCase):
    def test_foreign_cluster_marker_is_ignored(self):
        marker = build_drill_marker("INNY-KLASTER", 1787019064, "galera-x", 3)
        self.assertEqual(
            drill_marker_unixtime(marker, CLUSTER, "t"),
            0,
            "znacznik obcego klastra podniosl metryke — alert ISC-47 zamilklby bez powodu",
        )

    def test_unknown_format_version_is_ignored(self):
        marker = {**build_drill_marker(CLUSTER, 1787019064, "galera-x", 3), "format_version": 99}
        self.assertEqual(drill_marker_unixtime(marker, CLUSTER, "t"), 0)

    def test_non_integer_unixtime_raises(self):
        marker = {**build_drill_marker(CLUSTER, 1787019064, "galera-x", 3)}
        marker["last_success_unixtime"] = "wczoraj"
        with self.assertRaises(BackupError):
            drill_marker_unixtime(marker, CLUSTER, "t")


class TestPublishedMetricMatchesAlert(unittest.TestCase):
    METRIC = "galera_restore_last_success_unixtime"

    def test_metric_name_is_the_one_the_alert_reads(self):
        alerts = (REPO / "playbooks" / "f15_alerts.yml").read_text(encoding="utf-8")
        self.assertIn(
            self.METRIC,
            alerts,
            "regula ISC-47 nie czyta metryki, ktora publikuje most — most byłby teatrem",
        )

    def test_publish_writes_expected_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"galera_restore_drill-{CLUSTER}.prom"
            publish_drill_freshness(path, "n13-galera", CLUSTER, "s3", 1787019064)
            body = path.read_text(encoding="utf-8")
            self.assertIn(f"{self.METRIC}{{", body)
            self.assertIn('cluster="n13-galera"', body)
            self.assertIn(f'logical_cluster="{CLUSTER}"', body)
            self.assertIn('backend="s3"', body)
            self.assertTrue(body.rstrip().endswith("1787019064"))
            self.assertIn(f"# TYPE {self.METRIC} gauge", body)

    def test_missing_marker_publishes_honest_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.prom"
            publish_drill_freshness(path, "n13-galera", CLUSTER, "s3", 0)
            self.assertTrue(path.read_text(encoding="utf-8").rstrip().endswith(" 0"))


class TestS3MarkerKeyIsOutsideBackupPrefix(unittest.TestCase):
    def test_key_cannot_collide_with_backup_prefix(self):
        from galera_backup.storage.s3 import S3Backend

        b = S3Backend("h:9000", "bucket", False, "a", "s", CLUSTER, client=object())
        key = b._drill_marker_key()
        self.assertFalse(
            key.startswith(f"galera-{CLUSTER}-"),
            "klucz znacznika wpadl w prefiks skanowany przez fetch_latest/prune",
        )
        self.assertEqual(key, f"drill-state/{CLUSTER}.json")


if __name__ == "__main__":
    unittest.main()
