"""Obserwatorow: metryki textfile, SELinux context, swiezosci drillu.

Wydzielone z pipeline.py (refaktor strukturalny, zachowanie 1:1).
pipeline.py pozostaje facade: re-eksportuje te nazwy, zeby testy
(patch.object(pipeline, ...)) i tests/live dalej rozwiazywaly je w jednym
namespace — to jest kontrakt udokumentowany w docstringu pipeline.py.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .errors import BackupError
from .fsutil import atomic_write
from .textutil import escape_metric_label


def selinux_is_enabled() -> bool:
    return Path("/sys/fs/selinux/enforce").exists()


def restore_default_context(target_path: Path) -> None:
    from . import pipeline  # late binding: testy patchuja pipeline.restore_default_context / pipeline.selinux_is_enabled
    if pipeline.selinux_is_enabled():
        pass
    else:
        return
    restorecon = shutil.which("restorecon")
    if restorecon is None:
        raise BackupError(
            "E_METRICS",
            "SELinux is enabled but restorecon is unavailable",
        )
    result = subprocess.run(
        [restorecon, "-F", str(target_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise BackupError(
            "E_METRICS",
            f"Failed to restore the default security context on {target_path}: {detail}",
        )


class MetricsManager:
    def __init__(
        self,
        metric_path: Path,
        cluster_label: str,
        logical_cluster: str,
        backend_label: str,
        metric_prefix: str = "galera_backup",
    ):
        self.metric_path = metric_path
        self.c_label = escape_metric_label(cluster_label)
        self.lc_label = escape_metric_label(logical_cluster)
        self.b_label = escape_metric_label(backend_label)
        self.prefix = metric_prefix

    def update(
        self,
        last_success_unixtime: int = 0,
        last_failure_unixtime: int = 0,
        last_run_success: int = 0,
        last_size_bytes: int = 0,
        last_duration_seconds: float = 0.0,
    ) -> None:
        labels = f'cluster="{self.c_label}",logical_cluster="{self.lc_label}",backend="{self.b_label}"'
        content = (
            f"{self.prefix}_last_success_unixtime{{{labels}}} {last_success_unixtime}\n"
            f"{self.prefix}_last_failure_unixtime{{{labels}}} {last_failure_unixtime}\n"
            f"{self.prefix}_last_run_success{{{labels}}} {last_run_success}\n"
            f"{self.prefix}_last_size_bytes{{{labels}}} {last_size_bytes}\n"
            f"{self.prefix}_last_duration_seconds{{{labels}}} {last_duration_seconds:.3f}\n"
        )
        atomic_write(self.metric_path, content, mode=0o644)
        from . import pipeline  # kontrakt patchowania: patrz docstring pipeline.py
        pipeline.restore_default_context(self.metric_path)


def publish_drill_freshness(
    metric_path: Path,
    cluster_label: str,
    logical_cluster: str,
    backend_label: str,
    last_success_unixtime: int,
) -> None:
    """Przepisz swiezosc restore drillu ze znacznika backendu do textfile collectora.

    Wolane z hosta SCHEDULERA podczas backupu, bo to on jest scrapowany. Sam drill
    biegnie na izolowanym hoscie `restore`, ktorego nikt nie odpytuje — bez tego
    mostka jego sukces nigdy nie dociera do alertu ISC-47.

    Nazwa metryki jest CELOWO ta sama, ktorej uzywa runner na hoscie restore
    (`galera_restore_last_success_unixtime`): regula alertu bierze `max` po serii,
    wiec obie sciezki uruchomienia — cron i Ansible — trafiaja w ten sam licznik.
    """
    labels = (
        f'cluster="{escape_metric_label(cluster_label)}",'
        f'logical_cluster="{escape_metric_label(logical_cluster)}",'
        f'backend="{escape_metric_label(backend_label)}"'
    )
    content = (
        "# Zrodlo: znacznik restore drill w backendzie kopii (patrz storage/artifacts.py).\n"
        "# Wartosc 0 oznacza brak potwierdzonego drillu dla tego klastra.\n"
        "# HELP galera_restore_last_success_unixtime Unix time of the last successful restore drill.\n"
        "# TYPE galera_restore_last_success_unixtime gauge\n"
        f"galera_restore_last_success_unixtime{{{labels}}} {int(last_success_unixtime)}\n"
    )
    atomic_write(metric_path, content, mode=0o644)
    restore_default_context(metric_path)
