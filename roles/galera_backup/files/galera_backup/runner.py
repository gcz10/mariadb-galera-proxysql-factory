"""Uruchamianie podprocesow z redakcja sekretow i straznikiem argv.

`CommandRunner` nie jest podmieniany jako atrybut modulu — testy patchuja jego
METODE (`patch.object(mod.CommandRunner, "_exec")`). Dziala to niezaleznie od
tego, ktory modul klase definiuje, bo fasada re-eksportuje TEN SAM obiekt klasy.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Optional, Sequence

from .errors import BackupError

class SecretRedactor:
    def __init__(self, secret_values: Sequence[str] | set[str]):
        self.secret_values = sorted([s for s in secret_values if s], key=len, reverse=True)

    def redact(self, text: str) -> str:
        if not text:
            return text
        res = str(text)
        for secret in self.secret_values:
            if secret:
                res = res.replace(secret, "[REDACTED]")
        return res


class CommandRunner:
    def __init__(
        self,
        secret_values: Sequence[str] | set[str],
        redactor: Optional["SecretRedactor"] = None,
    ):
        self.secret_values = set(s for s in secret_values if s)
        # The argv guard gates on secret_values only. Output redaction may use a
        # wider set (credential halves) passed in explicitly; default to the
        # guard set when no redactor is supplied.
        self.redactor = redactor if redactor is not None else SecretRedactor(self.secret_values)

    def run(
        self,
        cmd: list[str],
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str | Path] = None,
        timeout: Optional[float] = None,
    ) -> tuple[int, str, str]:
        # Pre-execution check: no secret value in argv
        for arg in cmd:
            for secret in self.secret_values:
                if secret and secret in str(arg):
                    raise BackupError(
                        "E_SECRET_IN_ARGV",
                        "Command argv contains a loaded secret value; command rejected before process creation"
                    )

        return self._exec(cmd, env=env, cwd=cwd, timeout=timeout)

    def _exec(
        self,
        cmd: list[str],
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str | Path] = None,
        timeout: Optional[float] = None,
    ) -> tuple[int, str, str]:
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        p: Optional[subprocess.Popen] = None
        try:
            # start_new_session=True JEST WYMAGANE przez os.killpg ponizej: bez
            # wlasnej grupy procesow `os.getpgid(p.pid)` zwraca grupe RODZICA,
            # wiec killpg zabija runnera (a pod cronem cala grupe zadania).
            # Zweryfikowane: usuniecie tej flagi konczy proces testowy rc=137.
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(cwd) if cwd else None,
                env=proc_env,
                start_new_session=True,
            )
            stdout, stderr = p.communicate(timeout=timeout)
            return p.returncode, self.redactor.redact(stdout), self.redactor.redact(stderr)
        except subprocess.TimeoutExpired:
            if p is not None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception:
                    p.kill()
                p.communicate()
            raise BackupError("E_SUBPROCESS", f"Command '{cmd[0]}' timed out after {timeout}s")
        except BaseException as exc:
            if p is not None and p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                    p.wait(timeout=5)
                except Exception:
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    except Exception:
                        p.kill()
                    p.wait()
            if isinstance(exc, BackupError):
                raise
            raise BackupError("E_SUBPROCESS", f"Failed to execute command '{cmd[0]}': {exc}") from exc
