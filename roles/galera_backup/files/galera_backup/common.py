"""Wspolny rdzen backupu i restore: narzedzia GALERA, metryki, backend, sinki.

PODSZCIEP MONOLITU (F1). Zasada wiazania symboli jest niezmienna od czasu
dekompozycji monolitu `galera-backup` (szczegoly w docstringu
`tests/unit/galera_backup_testlib.py`): `patch.object(modul, "nazwa")`
przechwytuje wylacznie wywolania rozwiazywane w globals TEGO modulu.

Dlatego podzial odpowiedzialnosci jest nastepujacy:

* TEN modul definiuje wszystko, co dzielą backup i restore — kwerendy wsrep,
  buildera backendu, menedzer metryk, rejestru redactora oraz oba sinki
  (`_record_pre_lock_failure`, `_finalize_success_cleanup`). Jesli test
  podmienia tu jakis symbol (np. `S3Backend` w `get_storage_backend`), patchuje
  wlasnie ten modul.
* `backup.py` / `restore.py` importuja te nazwy DO SWOICH globals i tam
  rozwiazuja wywolania — testy przebiegow patchuja ich moduly.
* `pipeline.py` jest fasada (main + re-eksporty dla bezposrednich uzyc).
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from .errors import BackupError
from .textutil import escape_metric_label
from .fsutil import atomic_write, remove_sensitive_work_dir
from .storage.filesystem import FilesystemBackend, SMBBackend
from .storage.s3 import S3Backend
from .state import EventManager, StateManager
from .runner import CommandRunner, SecretRedactor
from .config import RunConfig


# Redactor instalowany raz na przebieg (backup albo restore) po zaladowaniu
# sekretow, zeby main() moglo zredagowac nieoczekiwany wyjatek przed trafem do
# journald. None dopoki nic nie zaladowano — main() drukuje wtedy bez zmian.
_module_redactor: Optional[SecretRedactor] = None


def set_module_redactor(redactor: Optional[SecretRedactor]) -> None:
    global _module_redactor
    _module_redactor = redactor


def get_module_redactor() -> Optional[SecretRedactor]:
    return _module_redactor


def selinux_is_enabled() -> bool:
    return Path("/sys/fs/selinux/enforce").exists()


def restore_default_context(target_path: Path) -> None:
    if not selinux_is_enabled():
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
        restore_default_context(self.metric_path)

    def discard(self) -> None:
        """Zabierz plik metryki tego klastra z TEGO wezla.

        Metryka opisuje KLASTER, a jej producentem jest donor wybrany w danym
        przebiegu. Cron stoi na kazdym kandydacie, wiec po przeniesieniu donora
        stary plik zostawal na poprzednim wezle i node_exporter serwowal dwie
        sprzeczne serie tej samej metryki: `min(last_run_success)` widzialo
        porzucone zero i alert palil sie mimo zdrowych kopii (zmierzone
        2026-09-12). Wezel, ktory nie zostal donorem, zabiera swoj plik ze soba.
        """
        try:
            self.metric_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BackupError(
                "E_METRICS",
                f"Failed to remove the stale metric file {self.metric_path}: {exc}",
            ) from exc


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


def get_storage_backend(
    cfg: RunConfig,
    secrets: dict[str, str],
    runner: Optional[CommandRunner] = None,
    purpose: str = "write",
) -> Any:
    """Zbuduj backend kopii; `purpose="retention"` bierze poswiadczenie z delete.

    Rozdzielenie jest bezpieczenstwem, nie kosmetyka. Donora wybiera runner przy
    starcie, wiec poswiadczenie ZAPISU lezy na kazdym wezle Galery i nie moze
    miec prawa kasowania — inaczej kompromitacja dowolnego wezla bazy kasuje
    historie off-cluster. Klucz retencji dostaje wylacznie koordynator
    (`backup.scheduler.host`), patrz roles/galera_backup/templates/minio-policy-prune.json.j2.
    """
    b_type = str(cfg.backend.get("type", cfg.backend.get("destination", ""))).lower()
    if b_type == "s3":
        if purpose == "retention":
            access_key = secrets.get("GALERA_BACKUP_S3_PRUNE_ACCESS_KEY", "")
            secret_key = secrets.get("GALERA_BACKUP_S3_PRUNE_SECRET_KEY", "")
            if not access_key or not secret_key:
                raise BackupError(
                    "E_SECRETS",
                    "Retention credentials are absent: this host is not the retention coordinator",
                )
        else:
            access_key = secrets["GALERA_BACKUP_S3_ACCESS_KEY"]
            secret_key = secrets["GALERA_BACKUP_S3_SECRET_KEY"]
        return S3Backend(
            endpoint=cfg.backend["endpoint"],
            bucket=cfg.backend["bucket"],
            secure=cfg.backend.get("secure", False),
            access_key=access_key,
            secret_key=secret_key,
            cluster_name=cfg.cluster_name,
        )
    elif b_type == "smb":
        return SMBBackend(
            source=cfg.backend["source"],
            mount_point=cfg.backend["mount_point"],
            options=cfg.backend.get("options", []),
            username=secrets["GALERA_BACKUP_SMB_USERNAME"],
            password=secrets["GALERA_BACKUP_SMB_PASSWORD"],
            domain=secrets.get("GALERA_BACKUP_SMB_DOMAIN"),
            cluster_name=cfg.cluster_name,
            runner=runner,
        )
    elif b_type == "filesystem":
        return FilesystemBackend(
            mount_point=cfg.backend["mount_point"],
            expected_fstype=cfg.backend.get("expected_fstype", ""),
            cluster_name=cfg.cluster_name,
        )
    else:
        raise BackupError("E_CONFIG", f"Unknown backend type '{b_type}'")


def last_success_unixtime(state_mgr: StateManager) -> int:
    """Ostatni udany przebieg z state.json; nieczytelny stan to 0, nigdy wyjatek.

    Potrzebne w OBSLUDZE porazki. `E_STATE` znaczy dokladnie "tego pliku nie da
    sie odczytac", wiec niezabezpieczony odczyt w galezi obslugujacej rzuca TEN
    SAM blad po raz drugi: zastepuje oryginalna diagnostyke, a zapis metryki —
    ktory stoi za nim — nigdy nie dochodzi do skutku. Dashboard zostaje wtedy
    z zeszlonocnym `last_run_success=1` mimo nieudanego przebiegu.

    Kazdy inny blad odczytu jest tu traktowany tak samo: to sank, nie pomiar.
    """
    try:
        last_success = state_mgr.read().get("last_success") or {}
    except Exception:
        return 0
    try:
        return int(last_success.get("unixtime", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _guarded_state_write(event_mgr: EventManager, write: Any) -> None:
    """Wykonaj zapis stanu tak, by jego porazka nie zjadla reszty obslugi bledu.

    Zapis stanu jest sankiem, nie pomiarem: gdy plik jest nieczytelny albo
    niemodyfikowalny, jego porazka NIE MOZE (a) zastapic oryginalnej diagnostyki
    ani (b) przerwac obslugi przed zapisem metryki. Dokladnie ten mechanizm
    trzymal `last_run_success` na 1 po nieudanym przebiegu. Porazka zapisu
    dostaje wlasne zdarzenie, zeby nie znikla bez sladu.
    """
    try:
        write()
    except Exception as state_exc:
        try:
            event_mgr.emit(
                "state.write_failure",
                {
                    "error_code": "E_STATE",
                    "message": f"zapis stanu nie powiodl sie: {state_exc}",
                },
            )
        except Exception:
            pass


def record_state_failure(
    state_mgr: StateManager,
    event_mgr: EventManager,
    command: str,
    unixtime: int,
    error_code: str,
    error_message: str,
) -> None:
    """Best-effort zapis PORAZKI do state.json (uses `update_failure`)."""
    _guarded_state_write(
        event_mgr,
        lambda: state_mgr.update_failure(command, unixtime, error_code, error_message),
    )


def record_state_locked(
    state_mgr: StateManager,
    event_mgr: EventManager,
    command: str,
    unixtime: int,
) -> None:
    """Best-effort zapis BLOKADY do state.json.

    Osobna funkcja, bo `status` jest czescia kontraktu stanu: blokada to
    `"locked"`, nie `"failed"` (state.py:71-76, ten sam status zapisuje
    `restore.py`). Zlanie obu zapisow w jeden zmieniloby status, po ktorym
    konsument rozpoznaje blokade — przy zachowaniu identycznego sanku.
    """
    _guarded_state_write(event_mgr, lambda: state_mgr.update_locked(command, unixtime))


def _record_pre_lock_failure(
    cluster_dir: Path,
    cluster_name: str,
    command: str,
    exc: Exception,
    redactor: SecretRedactor,
    metrics_mgr: Optional[MetricsManager] = None,
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
    try:
        StateManager(cluster_name, cluster_dir / "state.json").update_failure(
            command, now_ts, err_code, redactor.redact(err_msg)
        )
    except Exception:
        pass
    try:
        EventManager(cluster_dir / "events.jsonl", redactor).emit(
            "state.failure",
            {"error_code": err_code, "error_message": redactor.redact(err_msg)},
        )
    except Exception:
        pass
    if metrics_mgr is not None:
        try:
            last_succ_time = last_success_unixtime(
                StateManager(cluster_name, cluster_dir / "state.json")
            )
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
        try:
            remove_sensitive_work_dir(work_dir, work_dir_error_code)
        except Exception as cleanup_exc:
            report("workdir.remove", cleanup_exc, work_dir_error_code)
