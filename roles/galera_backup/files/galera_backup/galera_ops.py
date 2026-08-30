"""Operacje na stanie Galera: odczyt wsrep, wsrep_desync, flow control, oczekiwanie na Synced.

Wydzielone z pipeline.py (refaktor strukturalny, zachowanie 1:1).
pipeline.py pozostaje facade: re-eksportuje te nazwy, zeby testy
(patch.object(pipeline, ...)) i tests/live dalej rozwiazywaly je w jednym
namespace — to jest kontrakt udokumentowany w docstringu pipeline.py.
"""

from __future__ import annotations

import time
from pathlib import Path

from .errors import BackupError
from .runner import CommandRunner


def query_galera_vars(socket_path: Path, runner: CommandRunner) -> dict[str, str]:
    cmd = ["mariadb", f"--socket={socket_path}", "-u", "root", "-e", "SHOW GLOBAL STATUS LIKE 'wsrep_%';"]
    code, out, err = runner.run(cmd)
    if code != 0:
        raise BackupError("E_GALERA", f"Failed to query Galera status via socket '{socket_path}': {err or out}")

    vars_dict: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 2:
            vars_dict[parts[0].lower()] = parts[1]
        else:
            parts_space = line.strip().split(maxsplit=1)
            if len(parts_space) == 2:
                vars_dict[parts_space[0].lower()] = parts_space[1]

    return vars_dict


def set_wsrep_desync(socket_path: Path, runner: CommandRunner, enable: bool) -> bool:
    """Przelacz wsrep_desync. Zwraca True TYLKO gdy stan zmienilo TO wywolanie.

    Wezel w stanie innym niz Synced (4) juz jest odsynchronizowany — przez SST
    do innego wezla albo przez operatora. Ustawienie desync=OFF w `finally`
    zdjeloby wtedy CUDZY desync i wciagnelo dawce z powrotem do przesylania
    zapisow w srodku transferu. Dlatego wlaczamy wylacznie ze stanu Synced,
    a wylaczamy wylacznie to, co sami wlaczylismy.
    """
    if enable:
        from . import pipeline  # late binding: testy patchuja pipeline.query_galera_vars (kontrakt w docstringu pipeline)
        state = pipeline.query_galera_vars(socket_path, runner).get("wsrep_local_state", "")
        if state != "4":
            return False
    value = "ON" if enable else "OFF"
    code, out, err = runner.run(
        ["mariadb", f"--socket={socket_path}", "-u", "root", "-e", f"SET GLOBAL wsrep_desync = {value};"]
    )
    if code != 0:
        raise BackupError("E_GALERA", f"SET GLOBAL wsrep_desync={value} failed: {err or out}")
    return True


def _flow_control_paused_ns(gal_vars: dict[str, str]) -> int:
    """Odczytaj wsrep_flow_control_paused_ns; brak zmiennej to jawny E_GALERA.

    Domysl "0" przy braku zmiennej wylaczal zabezpieczenie flow control po
    cichu: SHOW GLOBAL STATUS bez tej pozycji znaczy "nie wiemy, czy backup
    nie zatrzymal klastra", a nie "klastra na pewno nie zatrzymal".
    """
    raw = gal_vars.get("wsrep_flow_control_paused_ns")
    if raw is None or str(raw).strip() == "":
        raise BackupError(
            "E_GALERA",
            "Brak wsrep_flow_control_paused_ns w SHOW GLOBAL STATUS; odmawiam backupu bez straznika flow control",
        )
    return int(raw)


def wait_until_synced(socket_path: Path, runner: CommandRunner, timeout_s: int = 900) -> None:
    """Czekaj az wezel wroci do Synced po wsrep_desync=OFF.

    Powrot nie jest natychmiastowy: wezel musi nadrobic kolejke zapisow
    zgromadzona w czasie backupu. Bez tego oczekiwania runner konczy sie
    sukcesem, zostawiajac wezel w Donor/Desynced — ProxySQL trzyma go poza
    ruchem, a nastepna brama zdrowia odrzuca caly klaster.
    """
    from . import pipeline  # late binding: patrz wyzej
    deadline = time.monotonic() + timeout_s
    while True:
        state = pipeline.query_galera_vars(socket_path, runner).get("wsrep_local_state_comment", "")
        if state == "Synced":
            return
        if time.monotonic() >= deadline:
            raise BackupError("E_GALERA", f"wezel utknal w stanie '{state}' po wsrep_desync=OFF (limit {timeout_s}s)")
        time.sleep(5)

