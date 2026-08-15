"""Trwaly stan ostatniego przebiegu i dziennik zdarzen.

`MetricsManager` celowo NIE jest tutaj: wola `restore_default_context`, ktory
testy podmieniaja przez `patch.object(self.mod, ...)` i asertuja samo wywolanie
(`restore_context.assert_called_once_with(metric_path)`). Po przeniesieniu to
wywolanie rozwiazywaloby sie w tym module, poza zasiegiem mocka.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from .errors import BackupError
from .fsutil import atomic_write
from .runner import SecretRedactor

class StateManager:
    def __init__(self, cluster_name: str, state_path: Path):
        self.cluster_name = cluster_name
        self.state_path = state_path

    def read(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "format_version": 1,
                "cluster": self.cluster_name,
                "last_run": None,
                "last_success": None,
                "last_failure": None,
            }
        try:
            with open(self.state_path, "r", encoding="utf-8") as state_file:
                state = json.load(state_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupError(
                "E_STATE",
                f"Cannot read backup state file '{self.state_path}': {exc}",
            ) from exc

        if (
            not isinstance(state, dict)
            or state.get("format_version") != 1
            or state.get("cluster") != self.cluster_name
        ):
            raise BackupError(
                "E_STATE",
                f"Backup state file '{self.state_path}' has an invalid format or cluster identity",
            )
        return state

    def update_success(self, command: str, unixtime: int, artifact: Optional[str] = None) -> None:
        curr = self.read()
        succ = {"command": command, "unixtime": unixtime, "artifact": artifact}
        run = {"command": command, "status": "success", "error_code": None, "unixtime": unixtime}
        curr["last_run"] = run
        curr["last_success"] = succ
        atomic_write(self.state_path, json.dumps(curr, indent=2), mode=0o644)

    def update_failure(self, command: str, unixtime: int, error_code: str, error_message: str) -> None:
        curr = self.read()
        fail = {"command": command, "unixtime": unixtime, "error_code": error_code, "error_message": error_message}
        run = {"command": command, "status": "failed", "error_code": error_code, "unixtime": unixtime}
        curr["last_run"] = run
        curr["last_failure"] = fail
        atomic_write(self.state_path, json.dumps(curr, indent=2), mode=0o644)

    def update_locked(self, command: str, unixtime: int) -> None:
        curr = self.read()
        run = {"command": command, "status": "locked", "error_code": "E_LOCKED", "unixtime": unixtime}
        curr["last_run"] = run
        curr["last_failure"] = {"command": command, "unixtime": unixtime, "error_code": "E_LOCKED", "error_message": "Locked"}
        atomic_write(self.state_path, json.dumps(curr, indent=2), mode=0o644)


class EventManager:
    def __init__(self, events_path: Path, redactor: SecretRedactor):
        self.events_path = events_path
        self.redactor = redactor

    def emit(self, event_type: str, data: dict[str, Any]) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": int(time.time()), "event": event_type}
        payload.update(data)
        line = json.dumps(payload, separators=(",", ":"))
        line_clean = self.redactor.redact(line) + "\n"
        fd = os.open(str(self.events_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line_clean.encode("utf-8"))
        finally:
            os.close(fd)
