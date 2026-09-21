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

ZAKRES POMIAROW (v2). Znacznik niesie teraz takze czas i rozmiar drillu, a most
przepisuje je do TEGO SAMEGO pliku textfile co swiezosc. Testy pilnuja roznicy
miedzy BRAKIEM pomiaru a pomiarem zerowym:
  * znacznik v1 zachowuje swiezosc, ale NIE tworzy serii duration/size (most
    musi wtedy POMINAC te metryki — zmyslone zero wygladaloby jak drill
    natychmiastowy i pusty),
  * wartosc obecna, ale niemozliwa (NaN, liczba ujemna, zly typ) jest bledem
    i przerywa publikacje, zamiast zatruc serie,
  * wezel, ktory nie zostal donorem, zabiera ze soba OBA pliki textfile
    (metryke kopii i mostek), bo inaczej zostawia serie sprzeczna ze swieza
    publikacja nowego producenta.
"""
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "roles" / "galera_backup" / "files"))

from galera_backup import backup, pipeline  # noqa: E402
from galera_backup.errors import BackupError  # noqa: E402
from galera_backup.pipeline import (  # noqa: E402
    drill_marker_measurements,
    drill_metric_path,
    publish_drill_freshness,
)
from galera_backup.storage.artifacts import (  # noqa: E402
    build_drill_marker,
    drill_marker_unixtime,
)
from galera_backup.storage.filesystem import FilesystemBackend  # noqa: E402

CLUSTER = "newclaude13-r9"
# Wersja znacznika sprzed pomiarow (v1) — taka lezy juz w backendach.
LEGACY_MARKER = {
    "format_version": 1,
    "cluster_name": CLUSTER,
    "last_success_unixtime": 1787019064,
    "backup_name": "galera-newclaude13-r9-20260818-040000",
    "rows_verified": 3,
}


def _marker(duration=2.5, size=4096):
    """Znacznik v2 z pomiarami — ten sam ksztalt, jaki zapisuje drill."""
    return build_drill_marker(CLUSTER, 1787019064, "galera-x", 3, duration, size)


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
            b.write_drill_marker(_marker())
            self.assertEqual(b.read_drill_marker()["last_success_unixtime"], 1787019064)

    def test_retention_does_not_delete_the_marker(self):
        """Znacznik lezy poza prefiksem `galera-<cluster>-`, wiec prune go nie widzi."""
        with tempfile.TemporaryDirectory() as tmp:
            b = _backend(Path(tmp))
            b.write_drill_marker(_marker())
            # Skan retencji ma biec NA ZYWYM przykladzie: wygasla kopia
            # (stamp sprzed cutoffu) znika, wiec przetrwanie znacznika jest
            # cecha skanu, a nie pustego katalogu. Retencja <= 0 jest jawnie
            # odrzucana przez prune, wiec uzywamy poprawnej, dodatniej.
            expired = Path(tmp) / CLUSTER / "galera-newclaude13-r9-20260701-120000"
            expired.mkdir(parents=True)
            (expired / "metadata.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "cluster_name": CLUSTER,
                        "created_unixtime": 1000,
                    }
                ),
                encoding="utf-8",
            )
            b.prune(datetime.fromtimestamp(1785240000, tz=timezone.utc), 14)
            self.assertFalse(
                expired.exists(),
                "prune nie skasowal wygaslej kopii — skan retencji nie biegl",
            )
            self.assertIsNotNone(
                b.read_drill_marker(),
                "retencja skasowala znacznik drillu — most swiezosci zniknie po pierwszym pruningu",
            )

    def test_marker_is_not_mistaken_for_a_backup(self):
        """`fetch_latest` iteruje po katalogach kopii; plik znacznika ma byc niewidoczny."""
        with tempfile.TemporaryDirectory() as tmp:
            b = _backend(Path(tmp))
            b.write_drill_marker(_marker())
            with self.assertRaises(BackupError) as ctx:
                b.fetch_latest(Path(tmp) / "work")
            self.assertIn("No complete backups found", str(ctx.exception))

    def test_corrupt_marker_is_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _backend(Path(tmp))
            b.write_drill_marker(_marker())
            (Path(tmp) / CLUSTER / "drill-state.json").write_text("{ niepelny", encoding="utf-8")
            with self.assertRaises(BackupError):
                b.read_drill_marker()


class TestDrillMarkerTrust(unittest.TestCase):
    def test_foreign_cluster_marker_is_ignored(self):
        marker = build_drill_marker("INNY-KLASTER", 1787019064, "galera-x", 3, 2.5, 4096)
        self.assertEqual(
            drill_marker_unixtime(marker, CLUSTER, "t"),
            0,
            "znacznik obcego klastra podniosl metryke — alert ISC-47 zamilklby bez powodu",
        )

    def test_unknown_format_version_is_ignored(self):
        marker = {**_marker(), "format_version": 99}
        self.assertEqual(drill_marker_unixtime(marker, CLUSTER, "t"), 0)

    def test_non_integer_unixtime_raises(self):
        marker = {**_marker()}
        marker["last_success_unixtime"] = "wczoraj"
        with self.assertRaises(BackupError):
            drill_marker_unixtime(marker, CLUSTER, "t")

    def test_unhashable_format_version_is_ignored_not_crashed(self):
        """Uszkodzony znacznik musi dac "nie wiem", nie `TypeError`.

        `in` na zbiorze wymaga hashable, wiec lista albo slownik w
        `format_version` przerwalby backup wyjatkiem spoza domeny bledow
        runnera zamiast uczciwie pominac pomiar.
        """
        for version in ([], {"a": 1}, True, None):
            with self.subTest(format_version=version):
                marker = {**_marker(), "format_version": version}
                self.assertEqual(drill_marker_unixtime(marker, CLUSTER, "t"), 0)
                self.assertEqual(
                    drill_marker_measurements(marker, CLUSTER, "t"), (None, None)
                )


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
    vars_file = alerts_path.parent / "vars" / "alert_rules.yml"
    if vars_file.is_file():
        _walk(yaml.safe_load(vars_file.read_text(encoding="utf-8")))
    return [r for r in rules if r.get("uid") == uid]


class TestPublishedMetricMatchesAlert(unittest.TestCase):
    METRIC = "galera_restore_last_success_unixtime"

    def test_metric_name_is_the_one_the_alert_reads(self):
        rules = _alert_rules(
            REPO / "playbooks" / "f15_alerts.yml",
            "{{ f15_uid_prefix }}-restore-drill-stale",
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
            "{{ f15_uid_prefix }}-restore-drill-stale"
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
            "{{ f15_uid_prefix }}-metrics-frozen",
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



class TestLegacyMarkerKeepsFreshnessWithoutMeasurements(unittest.TestCase):
    """Znacznik v1 sprzed pomiarow: swiezosc TAK, zmyslone pomiary NIE.

    Backendy zawieraja juz znaczniki zapisane przed wprowadzeniem v2. Czytnik
    musi je nadal uznawac za dowod drillu (inaczej alert ISC-47 zapala sie bez
    powodu na kazdym klastrze), ale NIE MOZE z braku pomiaru zrobic zera:
    seria `last_size_bytes=0` wygladalaby jak drill, ktory odtworzyl pusty
    artefakt, a `last_duration_seconds=0` — jak drill natychmiastowy.
    """

    def test_freshness_is_decoded_from_a_v1_marker(self):
        self.assertEqual(
            drill_marker_unixtime(LEGACY_MARKER, CLUSTER, "t"),
            1787019064,
            "v1 przestal byc dowodem swiezosci — alert zapali sie mimo udanych drillow",
        )

    def test_v1_marker_yields_no_measurements(self):
        self.assertEqual(
            drill_marker_measurements(LEGACY_MARKER, CLUSTER, "t"),
            (None, None),
            "brak pomiaru w v1 zamienil sie w wartosc, ktora udaje pomiar",
        )

    def test_v2_marker_with_a_null_field_omits_that_measurement(self):
        marker = {**_marker(), "size_bytes": None}
        self.assertEqual(
            drill_marker_measurements(marker, CLUSTER, "t"),
            (2.5, None),
            "jawny brak pomiaru w v2 zamienil sie w zero",
        )

    def test_bridge_omits_measurement_series_for_a_legacy_marker(self):
        """Pelna sciezka mostka na znaczniku v1 — liczby musza ZNIKNAC, nie wyzerowac sie."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = _backend(Path(tmp))
            backend.write_drill_marker(dict(LEGACY_MARKER))
            marker = backend.read_drill_marker()
            metric_path = drill_metric_path(Path(tmp), CLUSTER)
            duration, size = drill_marker_measurements(marker, CLUSTER, "t")
            publish_drill_freshness(
                metric_path,
                "n13-galera",
                CLUSTER,
                "s3",
                drill_marker_unixtime(marker, CLUSTER, "t"),
                duration_seconds=duration,
                size_bytes=size,
            )

            body = metric_path.read_text(encoding="utf-8")
            self.assertIn("galera_restore_last_success_unixtime{", body)
            self.assertNotIn("galera_restore_last_duration_seconds", body)
            self.assertNotIn("galera_restore_last_size_bytes", body)


class TestInvalidMeasurementFailsClosed(unittest.TestCase):
    """Obecna, ale niemozliwa wartosc jest bledem — nigdy cichym zerem.

    Rozroznienie jest istotne: `None` to BRAK pomiaru (pominiecie serii),
    a NaN/liczba ujemna/zly typ to USZKODZONY pomiar. Ten drugi trafia do
    Prometheusa jako seria, ktora psuje agregacje i alarmy, wiec publikacja
    musi zostac przerwana.
    """

    INVALID = (
        ("nan", float("nan")),
        ("infinite", float("inf")),
        ("negative", -1.0),
        ("bool", True),
        ("string", "2.5"),
    )

    def test_invalid_duration_is_rejected_by_the_marker_builder(self):
        for label, value in self.INVALID:
            with self.subTest(duration=label):
                with self.assertRaises(BackupError):
                    _marker(duration=value)

    def test_invalid_size_is_rejected_by_the_marker_builder(self):
        for label, value in (("negative", -1), ("float", 1.5), ("string", "4096"), ("bool", True)):
            with self.subTest(size=label):
                with self.assertRaises(BackupError):
                    _marker(size=value)

    def test_invalid_duration_in_a_stored_marker_raises_on_decode(self):
        marker = {**_marker(), "duration_seconds": float("nan")}
        with self.assertRaises(BackupError):
            drill_marker_measurements(marker, CLUSTER, "t")

    def test_invalid_measurement_aborts_publication_without_touching_the_file(self):
        for label, value in self.INVALID:
            with self.subTest(duration=label):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "m.prom"
                    path.write_text("previous-series 1\n", encoding="utf-8")
                    with self.assertRaises(BackupError):
                        publish_drill_freshness(
                            path, "n13-galera", CLUSTER, "s3", 1787019064,
                            duration_seconds=value,
                        )
                    self.assertEqual(
                        path.read_text(encoding="utf-8"),
                        "previous-series 1\n",
                        "nieudana publikacja nadpisala poprzednia, poprawna serie",
                    )

    def test_invalid_size_aborts_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.prom"
            with self.assertRaises(BackupError):
                publish_drill_freshness(
                    path, "n13-galera", CLUSTER, "s3", 1787019064, size_bytes=-5,
                )
            self.assertFalse(path.exists())


class TestFreshMarkerAgreesAcrossArtifacts(unittest.TestCase):
    """Znacznik, jego dekodowanie i metryka mowia o TYM SAMYM drillu.

    To jest wlasciwy kontrakt mostka: liczba opublikowana w textfile musi byc
    ta, ktora drill zapisal w znaczniku. Test przechodzi cala droge
    (zapis do backendu -> odczyt -> dekodowanie -> publikacja), wiec rozjazd
    nazw pol albo jednostek jest wykrywany, a nie zakladany.
    """

    def test_published_text_carries_the_marker_measurements(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = _backend(Path(tmp))
            backend.write_drill_marker(_marker(duration=12.75, size=52428800))

            marker = backend.read_drill_marker()
            metric_path = drill_metric_path(Path(tmp), CLUSTER)
            publish_drill_freshness(
                metric_path,
                "n13-galera",
                CLUSTER,
                "s3",
                drill_marker_unixtime(marker, CLUSTER, "t"),
                duration_seconds=drill_marker_measurements(marker, CLUSTER, "t")[0],
                size_bytes=drill_marker_measurements(marker, CLUSTER, "t")[1],
            )

            labels = (
                f'cluster="n13-galera",logical_cluster="{CLUSTER}",backend="s3"'
            )
            body = metric_path.read_text(encoding="utf-8")
            self.assertIn(
                f"galera_restore_last_duration_seconds{{{labels}}} 12.750", body
            )
            self.assertIn(
                f"galera_restore_last_size_bytes{{{labels}}} 52428800", body
            )
            self.assertTrue(body.rstrip().splitlines()[-1].endswith(" 52428800"))



class TestNonSelectedDonorRemovesStaleBridgeFile(unittest.TestCase):
    """Wezel, ktory nie zostal donorem, zabiera ze soba TAKZE mostek drillu.

    Mostek niesie te same serie co metryka kopii (`galera_restore_last_*`)
    i ma jednego producenta na przebieg. Porzucony plik zostaje po przeniesieniu
    donora i node_exporter serwuje dwie sprzeczne serie tej samej metryki.
    """

    DONOR = "192.168.1.51"
    THIS_NODE = "192.168.1.52"

    def _config(self, td_path):
        return {
            "format_version": 1,
            "cluster_name": "review-r10",
            "metric_cluster_label": "r10-galera",
            "local_role": "scheduler",
            "scheduler_system_hostname": "different-host",
            "node_system_address": self.THIS_NODE,
            "galera_nodes_expected": 3,
            "proxysql": {
                "admin_host": "192.168.1.44",
                "admin_port": 6032,
                "writer_hostgroup": 10,
                "backup_hostgroup": 20,
            },
            "galera_nodes": [self.DONOR, self.THIS_NODE, "192.168.1.53"],
            "mariadb_version": "11.4.12",
            "retention_days": 14,
            "flow_control_threshold_ns": 1000000000,
            "backend": {
                "type": "s3",
                "endpoint": "192.168.1.47:9000",
                "bucket": "r10-galera-backups",
                "secure": False,
            },
            "paths": {
                "install_root": str(td_path),
                "cluster_dir": str(td_path / "clusters" / "review-r10"),
                "staging_root": str(td_path / "staging"),
                "datadir": str(td_path / "datadir"),
                "socket": str(td_path / "mysql.sock"),
                "metric_file": str(td_path / "metrics.prom"),
            },
        }

    def test_skipped_run_removes_the_bridge_left_by_an_earlier_donor_turn(self):
        secrets = (
            'GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\n'
            'GALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\n'
            'GALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\n'
            'GALERA_BACKUP_PROXYSQL_STATS_USER="admin"\n'
            'GALERA_BACKUP_PROXYSQL_STATS_PASSWORD="proxysql_pass_999"\n'
        )
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            cfg_path = td_path / "config.json"
            env_path = td_path / "secrets.env"
            cfg_path.write_text(json.dumps(self._config(td_path)), encoding="utf-8")
            env_path.write_text(secrets, encoding="utf-8")
            os.chmod(env_path, 0o600)

            backup_metric = td_path / "metrics.prom"
            bridge_metric = drill_metric_path(td_path, "review-r10")
            backup_metric.write_text("galera_backup_last_run_success 1\n", encoding="utf-8")
            bridge_metric.write_text(
                "galera_restore_last_success_unixtime 1787019064\n", encoding="utf-8"
            )

            with patch("socket.gethostname", return_value="current-host"):
                with patch.object(backup, "elect_backup_donor", return_value=self.DONOR):
                    with patch.object(backup, "get_storage_backend"):
                        with patch.object(backup, "run_retention"):
                            pipeline.run_backup(
                                config_path=cfg_path,
                                secrets_path=env_path,
                                cluster_name="review-r10",
                            )

            self.assertFalse(backup_metric.exists())
            self.assertFalse(
                bridge_metric.exists(),
                "porzucony mostek zostaje i serwuje serie sprzeczna ze swieza publikacja",
            )



class TestAbsentMarkerDoesNotEraseAFreshDrill(unittest.TestCase):
    """Brak znacznika NIE MOZE skasowac dowodu zapisanego przez F11.

    Sciezka jest realna, nie teoretyczna: `write_drill_marker` po udanym
    drillu tylko raportuje `drill_marker.failure` i samego drillu nie degraduje,
    wiec swiezy sukces trafia do `state.json` przy NIEOBECNYM znaczniku. Play
    F11 publikuje go wtedy na galera[0] do wspolnego pliku. Gdyby nocny backup
    na tym samym wezle wpisal tam zero, alert ISC-47 "restore drill stale"
    zapalilby sie mimo udanego drillu.
    """

    DONOR = "192.168.1.51"

    def _config(self, td_path):
        return {
            "format_version": 1,
            "cluster_name": "review-r10",
            "metric_cluster_label": "r10-galera",
            "local_role": "scheduler",
            "scheduler_system_hostname": "gnode4",
            "node_system_address": self.DONOR,
            "galera_nodes_expected": 3,
            "proxysql": {
                "admin_host": "192.168.1.44",
                "admin_port": 6032,
                "writer_hostgroup": 10,
                "backup_hostgroup": 20,
            },
            "galera_nodes": [self.DONOR, "192.168.1.52", "192.168.1.53"],
            "mariadb_version": "11.4.12",
            "retention_days": 14,
            "flow_control_threshold_ns": 1000000000,
            "backend": {
                "type": "s3",
                "endpoint": "192.168.1.47:9000",
                "bucket": "r10-galera-backups",
                "secure": False,
            },
            "paths": {
                "install_root": str(td_path),
                "cluster_dir": str(td_path / "clusters" / "review-r10"),
                "staging_root": str(td_path / "staging"),
                "datadir": str(td_path / "datadir"),
                "socket": str(td_path / "mysql.sock"),
                "metric_file": str(td_path / "metrics.prom"),
            },
        }

    def _run_backup_with_marker(self, td_path, marker):
        secrets = (
            'GALERA_BACKUP_ENCRYPTION_KEY="enc_key_999"\n'
            'GALERA_BACKUP_S3_ACCESS_KEY="s3_access_888"\n'
            'GALERA_BACKUP_S3_SECRET_KEY="s3_secret_777"\n'
            'GALERA_BACKUP_PROXYSQL_STATS_USER="admin"\n'
            'GALERA_BACKUP_PROXYSQL_STATS_PASSWORD="proxysql_pass_999"\n'
        )
        cfg_path = td_path / "config.json"
        env_path = td_path / "secrets.env"
        cfg_path.write_text(json.dumps(self._config(td_path)), encoding="utf-8")
        env_path.write_text(secrets, encoding="utf-8")
        os.chmod(env_path, 0o600)

        galera_vars = {
            "wsrep_local_state_comment": "Synced",
            "wsrep_cluster_status": "Primary",
            "wsrep_ready": "ON",
            "wsrep_connected": "ON",
            "wsrep_cluster_size": "3",
            "wsrep_flow_control_paused_ns": "1000",
        }
        fake_backend = MagicMock()
        fake_backend.publish.return_value = pipeline.PublishedArtifact(
            backup_name="galera-review-r10-20260921-040000",
            prefix="p",
            encrypted_sha256="sha",
            encrypted_size=10,
            unixtime=1000,
        )
        fake_backend.read_drill_marker.return_value = marker

        def fake_exec(cmd, env=None, cwd=None, timeout=None):
            for index, argument in enumerate(cmd):
                if argument == "-out" and index + 1 < len(cmd):
                    Path(cmd[index + 1]).write_bytes(b"dummy-encrypted-payload")
            return (0, "", "")

        with patch("socket.gethostname", return_value="gnode4"):
            with patch.object(backup, "elect_backup_donor", return_value=self.DONOR):
                with patch.object(backup, "assert_scheduler_is_not_writer"):
                    with patch.object(backup, "query_galera_vars", return_value=galera_vars):
                        with patch.object(backup, "get_storage_backend", return_value=fake_backend):
                            with patch.object(
                                backup, "perform_physical_backup", return_value=("uuid-1", "100")
                            ):
                                with patch.object(backup, "run_retention"):
                                    with patch.object(
                                        pipeline.CommandRunner, "_exec", side_effect=fake_exec
                                    ):
                                        with patch("subprocess.Popen") as mock_popen:
                                            mock_proc = MagicMock()
                                            mock_proc.stdout.read.side_effect = [b"tar-data", b""]
                                            mock_proc.returncode = 0
                                            mock_proc.communicate.return_value = (b"", b"")
                                            mock_popen.return_value = mock_proc

                                            pipeline.run_backup(
                                                config_path=cfg_path,
                                                secrets_path=env_path,
                                                cluster_name="review-r10",
                                            )
        return td_path / "clusters" / "review-r10" / "events.jsonl"

    def test_absent_marker_leaves_the_existing_bridge_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bridge = drill_metric_path(td_path, "review-r10")
            fresh_line = (
                'galera_restore_last_success_unixtime{cluster="r10-galera",'
                'logical_cluster="review-r10",backend="s3"} 1787019064\n'
            )
            bridge.write_text(fresh_line, encoding="utf-8")

            events_path = self._run_backup_with_marker(td_path, None)

            self.assertEqual(
                bridge.read_text(encoding="utf-8"),
                fresh_line,
                "backup bez znacznika nadpisal swiezy dowod drillu — alert ISC-47 "
                "zapali sie mimo udanego drillu",
            )
            events = events_path.read_text(encoding="utf-8")
            self.assertIn("drill_freshness.skipped", events)
            self.assertNotIn("drill_freshness.failure", events)

    def test_confirmed_marker_still_publishes_over_the_existing_bridge(self):
        """Guard nie moze zamrozic mostka: potwierdzony znacznik nadal wygrywa."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bridge = drill_metric_path(td_path, "review-r10")
            bridge.write_text("galera_restore_last_success_unixtime 1\n", encoding="utf-8")

            self._run_backup_with_marker(
                td_path,
                build_drill_marker(
                    "review-r10", 1787019064, "galera-x", 3, 12.75, 52428800
                ),
            )

            body = bridge.read_text(encoding="utf-8")
            labels = 'cluster="r10-galera",logical_cluster="review-r10",backend="s3"'
            self.assertIn(f"galera_restore_last_duration_seconds{{{labels}}} 12.750", body)
            self.assertIn(f"galera_restore_last_size_bytes{{{labels}}} 52428800", body)

    def test_foreign_marker_does_not_erase_the_bridge_either(self):
        """Znacznik innego klastra daje swiezosc 0 — i musi tez pominac zapis."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bridge = drill_metric_path(td_path, "review-r10")
            bridge.write_text("galera_restore_last_success_unixtime 7\n", encoding="utf-8")

            self._run_backup_with_marker(td_path, _marker(duration=12.75, size=52428800))

            self.assertEqual(
                bridge.read_text(encoding="utf-8"),
                "galera_restore_last_success_unixtime 7\n",
                "znacznik obcego klastra nadpisal mostek tego klastra",
            )


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
