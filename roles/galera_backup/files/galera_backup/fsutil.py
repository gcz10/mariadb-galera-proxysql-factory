"""Operacje plikowe: sumy kontrolne, zapis atomowy, usuwanie drzew.

Czego tu NIE ma i dlaczego: `selinux_is_enabled` oraz `restore_default_context`
zostaja w entrypoincie, bo oba sa podmieniane przez `patch.object(self.mod, ...)`,
a drugie wola pierwsze. Przeniesienie ich tutaj sprawiloby, ze wywolanie
rozwiazywaloby sie w przestrzeni TEGO modulu i mock z testu przestalby cokolwiek
przechwytywac — testy dalej zielone, ale nic nie sprawdzaja.
"""

import hashlib
import os
import shutil
from pathlib import Path
from typing import Optional

from .errors import BackupError


def file_sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as payload_file:
        while chunk := payload_file.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def atomic_write(target_path: Path, content: str, mode: int = 0o644) -> None:
    """Zapis przez plik tymczasowy + `os.replace`, zeby czytelnik nigdy nie
    zobaczyl tresci obcietej w polowie."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.parent / f".tmp.{target_path.name}.{os.getpid()}"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, target_path)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def remove_tree_or_raise(
    path: Path,
    error_code: str,
    purpose: str,
) -> None:
    """Usun drzewo albo rzuc. Sprawdzenie po `rmtree` jest celowe: cichy brak
    usuniecia stagingu z danymi to wyciek, nie drobiazg."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise BackupError(
            error_code,
            f"Failed to remove {purpose} '{path}': {exc}",
        ) from exc
    if path.exists():
        raise BackupError(
            error_code,
            f"Failed to remove {purpose} '{path}': path still exists",
        )


def remove_sensitive_work_dir(
    work_dir: Optional[Path],
    error_code: str,
) -> None:
    if work_dir is None:
        return
    remove_tree_or_raise(
        work_dir,
        error_code,
        "sensitive staging directory",
    )
