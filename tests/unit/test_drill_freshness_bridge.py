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
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

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


def _alert_rules(alerts_path, uid):
    """Reguly alertow (dict z uid+expr) o dokladnym `uid`.

    Szukanie rekursywne po calej strukturze sprawia, ze przeniesienie listy
    `f15_rules` w inne miejsce playbooka nie oslabia testu, a yaml.safe_load
    gubi komentarze — nazwa metryki wpisana tylko w komentarzu nie zaspokoi
    asercji.
    """
    rules = []

    def _walk(node):
        if isinstance(node, dict):
            if "uid" in node and "expr" in node:
                rules.append(node)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(yaml.safe_load(alerts_path.read_text(encoding="utf-8")))
    return [r for r in rules if r.get("uid") == uid]


class TestPublishedMetricMatchesAlert(unittest.TestCase):
    METRIC = "galera_restore_last_success_unixtime"

    def test_metric_name_is_the_one_the_alert_reads(self):
        rules = _alert_rules(
            REPO / "playbooks" / "f15_alerts.yml",
            "isa-{{ cluster_label }}-restore-drill-stale",
        )
        self.assertEqual(
            len(rules),
            1,
            "regula ISC-47 (dokladny uid restore-drill-stale) zniknela lub "
            "jest zduplikowana w f15_alerts.yml",
        )
        self.assertIn(
            self.METRIC,
            rules[0]["expr"],
            "regula ISC-47 nie czyta w expr metryki publikowanej przez most — "
            "most bylby teatrem",
        )

    def test_restore_sources_are_aggregated_before_missing_metric_fallback(self):
        rules = _alert_rules(
            REPO / "playbooks" / "f15_alerts.yml",
            "isa-{{ cluster_label }}-restore-drill-stale",
        )
        self.assertEqual(len(rules), 1)
        expr = rules[0]["expr"]
        selector = (
            '{__name__=~"isa_restore_test_last_success_unixtime'
            '|galera_restore_last_success_unixtime",'
            'cluster="{{ cluster_label }}"}'
        )
        self.assertIn(
            selector,
            expr,
            "dwa zrodla swiezosci nie sa jednym zbiorem przed max()",
        )
        self.assertEqual(
            expr.count("or vector(0)"),
            1,
            "fallback przed scaleniem zrodel usuwa RHS przy zgodnym label set",
        )

    def test_backup_metrics_mtime_is_scoped_to_this_tenant_file(self):
        rules = _alert_rules(
            REPO / "playbooks" / "f15_alerts.yml",
            "isa-{{ cluster_label }}-metrics-frozen",
        )
        self.assertEqual(len(rules), 1)
        expr = rules[0]["expr"]
        config_template = (
            REPO / "roles" / "galera_backup" / "templates" / "config.json.j2"
        ).read_text(encoding="utf-8")
        metric_file_match = re.search(
            r'"metric_file":\s*"([^"]+)"',
            config_template,
        )
        self.assertIsNotNone(metric_file_match)
        metric_basename = Path(metric_file_match.group(1)).name
        expected_file = (
            'file=~".*/'
            f"{metric_basename.removesuffix('.prom')}[.]prom"
            '"'
        )
        self.assertIn('cluster="{{ cluster_label }}"', expr)
        self.assertIn(expected_file, expr)
        self.assertNotIn(
            "galera_backup-.*",
            expr,
            "szeroki regex pozwala swiezemu tenantowi maskowac zamrozonego",
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
