"""Cykl zycia przebiegu: rejestrowanie porazek przed lockiem, epilog sukcesu, retencja.

Wydzielone z pipeline.py (refaktor strukturalny, zachowanie 1:1).
pipeline.py pozostaje facade: re-eksportuje te nazwy, zeby testy
(patch.object(pipeline, ...)) i tests/live dalej rozwiazywaly je w jednym
namespace — to jest kontrakt udokumentowany w docstringu pipeline.py.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .errors import BackupError, combine_failures
from .state import EventManager, StateManager
from .fsutil import remove_sensitive_work_dir
from .secrets import redactable_secret_values, sensitive_secret_values
from .runner import CommandRunner, SecretRedactor
from .config import RunConfig
from .observers import MetricsManager
from .storage_factory import get_storage_backend


def _record_pre_lock_failure(
    cluster_dir: Path,
    cluster_name: str,
    command: str,
    exc: Exception,
    redactor: SecretRedactor,
    metrics_mgr: Optional["MetricsManager"] = None,
) -> None:
    """Best-effort state/event/metric sink for failures raised before the lock.

    Without this, an E_CONFIG/E_SECRETS/E_SECRETS_PERM abort leaves the previous
    night's last_run_success=1 in the textfile and every dashboard stays green.
    Sink nigdy nie moze zastapic oryginalnego bledu: kazdy sink jest chroniony
    przed dowolnym zwyklym wyjatkiem (nie tylko OSError), a wywolujacy ponownie
    rzuca oryginalny wyjatek.
    """
    err_code = exc.code if isinstance(exc, BackupError) else "E_STORAGE"
    err_msg = exc.public_message if isinstance(exc, BackupError) else str(exc)
    now_ts = int(time.time())
    from . import pipeline  # late binding: testy patchuja pipeline.StateManager/EventManager
    try:
        pipeline.StateManager(cluster_name, cluster_dir / "state.json").update_failure(
            command, now_ts, err_code, redactor.redact(err_msg)
        )
    except Exception:
        pass
    try:
        pipeline.EventManager(cluster_dir / "events.jsonl", redactor).emit(
            "state.failure",
            {"error_code": err_code, "error_message": redactor.redact(err_msg)},
        )
    except Exception:
        pass
    if metrics_mgr is not None:
        try:
            last_succ_time = 0
            try:
                last_succ = StateManager(cluster_name, cluster_dir / "state.json").read().get("last_success")
                last_succ_time = last_succ.get("unixtime", 0) if last_succ else 0
            except Exception:
                pass
            metrics_mgr.update(
                last_success_unixtime=last_succ_time,
                last_failure_unixtime=now_ts,
                last_run_success=0,
            )
        except Exception:
            pass


def _finalize_success_cleanup(
    event_mgr: EventManager,
    backend: Any,
    work_dir: Optional[Path],
    work_dir_error_code: str,
) -> None:
    """Epilog po zapisanym sukcesie backupu albo restore, wykonywany najlepszym wysilkiem.

    Zamkniecie backendu i usuniecie workdir sa porzadkowaniem po zweryfikowanym
    sukcesie. Ich porazka nie moze nadpisac state.success ani metryki sukcesu:
    zapisujemy osobne cleanup.failure z faza, a sam epilog nie rzuca wyjatku.
    Sink zdarzen tez jest najlepszym wysilkiem, zeby jego awaria nie reaktywowala
    problemu, ktory ten helper ma usunac.
    """

    def report(phase: str, cleanup_exc: Exception, default_code: str) -> None:
        error_code = cleanup_exc.code if isinstance(cleanup_exc, BackupError) else default_code
        message = cleanup_exc.public_message if isinstance(cleanup_exc, BackupError) else str(cleanup_exc)
        try:
            event_mgr.emit(
                "cleanup.failure",
                {"error_code": error_code, "message": message, "phase": phase},
            )
        except Exception:
            # Awaria sinka zdarzen tez nie moze zdegradowac zapisanego sukcesu.
            pass

    if backend is not None:
        try:
            backend.close()
        except Exception as cleanup_exc:
            report("backend.close", cleanup_exc, "E_STORAGE")
    if work_dir is not None:
        from . import pipeline  # late binding: testy patchuja pipeline.remove_sensitive_work_dir
        try:
            pipeline.remove_sensitive_work_dir(work_dir, work_dir_error_code)
        except Exception as cleanup_exc:
            report("workdir.remove", cleanup_exc, work_dir_error_code)


def has_retention_credential(secrets: dict[str, str]) -> bool:
    """Czy TEN host jest koordynatorem retencji (ma klucz z prawem delete)."""
    return bool(secrets.get("GALERA_BACKUP_S3_PRUNE_ACCESS_KEY")) and bool(
        secrets.get("GALERA_BACKUP_S3_PRUNE_SECRET_KEY")
    )


def run_retention(
    cfg: RunConfig,
    secrets: dict[str, str],
    runner: Optional["CommandRunner"],
    event_mgr: Any,
    backend_type: str,
    backup_backend: Any = None,
) -> None:
    """Skasuj wygasle kopie. JEDYNA sciezka runnera z prawem kasowania.

    Dla S3 buduje WLASNY backend na poswiadczeniu retencji — backend uzyty do
    publikacji nigdy nie dostaje prawa delete, wiec nie da sie przypadkiem
    skasowac historii kluczem lezacym na kazdym wezle Galery.

    Wezel bez poswiadczenia retencji konczy bez zdarzenia: dokladnie jeden host
    w klastrze jest koordynatorem, a dwa pozostale nie maja tu nic do roboty.

    Nigdy nie podnosi wyjatku. Kopia jest w tym momencie opublikowana i
    zweryfikowana; awaria retencji jest raportowana zdarzeniem i NIE degraduje
    udanego backupu (zachowanie sprzed rozdzielenia poswiadczen).
    """
    backend = backup_backend
    dedicated = None

    if backend_type == "s3":
        if not has_retention_credential(secrets):
            return
        from . import pipeline  # late binding: testy patchuja pipeline.get_storage_backend
        try:
            dedicated = pipeline.get_storage_backend(cfg, secrets, runner, purpose="retention")
            dedicated.preflight()
            backend = dedicated
        except BackupError as exc:
            event_mgr.emit(
                "retention.failure",
                {"error_code": exc.code, "message": exc.public_message},
            )
            return
        except Exception as exc:
            event_mgr.emit(
                "retention.failure", {"error_code": "E_STORAGE", "message": str(exc)}
            )
            return

    if backend is None:
        return

    try:
        deleted = backend.prune(datetime.now(timezone.utc), cfg.retention_days)
        event_mgr.emit("retention.success", {"deleted": int(deleted or 0)})
    except BackupError as exc:
        event_mgr.emit(
            "retention.failure",
            {"error_code": exc.code, "message": exc.public_message},
        )
    except Exception as exc:
        event_mgr.emit(
            "retention.failure", {"error_code": "E_STORAGE", "message": str(exc)}
        )
    finally:
        if dedicated is not None:
            try:
                dedicated.close()
            except Exception:
                pass

