"""Blokada wylacznosci runnera na jeden klaster.

Kolizja blokady NIE jest bledem backupu — inny przebieg wlasnie trwa. Zerowanie
metryki sukcesu w tym miejscu dawalo falszywy alarm braku swiezej kopii; poprawka
b24a8ff zachowuje `last_success_unixtime` odczytany ze stanu.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Optional

from .errors import BackupError

class LockManager:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(self._fd, 0)
            os.write(self._fd, f"{os.getpid()}\n".encode("utf-8"))
        except (BlockingIOError, OSError) as exc:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            raise BackupError(
                "E_LOCKED",
                f"Lock file {self.lock_path} is locked by another process",
            ) from exc

    def release(self) -> None:
        # Plik blokady CELOWO zostaje na dysku. `flock` chroni inode, nie
        # sciezke, wiec unlink pozwalal dwom przebiegom trzymac wylacznosc
        # jednoczesnie: jeden na starym inode, drugi na pliku utworzonym pod
        # ta sama nazwa juz po skasowaniu. Pusty plik 0600 nic nie kosztuje,
        # a w /run/lock znika i tak przy reboocie.
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

def resolve_lock_path(cluster_name: str, cluster_dir: Path) -> Path:
    runtime_lock_dir = Path("/run/lock")
    if runtime_lock_dir.is_dir() and os.access(runtime_lock_dir, os.W_OK):
        return runtime_lock_dir / f"galera-backup-{cluster_name}.lock"
    return cluster_dir / f"galera-backup-{cluster_name}.lock"
