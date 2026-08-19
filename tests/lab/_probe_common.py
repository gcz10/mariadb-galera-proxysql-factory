#!/usr/bin/env python3
"""Wspolny protokol sond lab: fail-closed, jeden parser, jeden raport.

POWSTAL PO PRZEGLADZIE THERMO-NUCLEAR (2026-08-20): sondy mialy ~12 wlasnych
parserow wyjscia ansible i kazdy z nich przy awarii hosta OPUSZCZAL go z
wyniku, a asercje typu `.get(host, "")` zamienialy brak odpowiedzi w
"brak dowodow winy" = PASS. Sonda, ktora nie umie sprawdzic, MUSI byc
czerwona (FAIL, exit 1) albo jawnie nierozstrzygnieta (exit 2) - nigdy
zielona.

Zasady protokolu:
 1. Naglowek ansible `FAILED`/`UNREACHABLE` to WPIS z bledem, nie brak wpisu.
 2. Host z inwentarza, ktorego nie ma w wyniku sekcji, to nierozstrzygniecie.
 3. PASS (exit 0) wylacznie przy pustych `failures` i `undetermined`.
 4. Komunikaty maja prefiksy `PASS:` / `FAIL:` / `REFUSED:` / `UNDETERMINED:`,
    zeby automat logowy mial jeden kontrakt.

Ten modul jest jedynym miejscem, ktore zna format wyjscia ansible ad-hoc.
Sondy nie pisza wlasnych parserow - dzieki temu poprawka wzorca propaguje
sie na wszystkie.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Kod wyjscia 2 = "nie da sie rozstrzygnac" (martwa infrastruktura pomiarowa).
# Kod 1 = FAIL (zmierzono i jest zle). Kod 0 = PASS (zmierzono i jest dobrze).
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNDETERMINED = 2

_HEADER_RE = re.compile(
    r"^(?P<host>\S+)\s+\|\s+(?P<status>CHANGED|SUCCESS|FAILED!?|UNREACHABLE!?)(?P<rest>.*)$"
)


class ProbeContext:
    """Minimalny kontekst: nazwa klastra + sciezki konfiguracji."""

    def __init__(self) -> None:
        import yaml

        self.cluster_name = os.environ.get("CLUSTER", "")
        if not self.cluster_name:
            print("FAIL: wymagana zmienna srodowiskowa CLUSTER=<nazwa>", file=sys.stderr)
            sys.exit(EXIT_FAIL)
        cfg_path = REPO_ROOT / os.environ.get(
            "CLUSTER_CONFIG", f"clusters/{self.cluster_name}/cluster.yml"
        )
        inv_path = REPO_ROOT / os.environ.get(
            "CLUSTER_INVENTORY", f"clusters/{self.cluster_name}/inventory.yml"
        )
        for p in (cfg_path, inv_path):
            if not p.is_file():
                print(f"FAIL: plik konfiguracyjny {p} nie istnieje", file=sys.stderr)
                sys.exit(EXIT_FAIL)
        self.config = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        self.inventory = yaml.safe_load(inv_path.read_text(encoding="utf-8")) or {}

    def group_hosts(self, group: str) -> list[str]:
        """Nazwy hostow grupy z inwentarza (kolejnosc stabilna)."""
        children = self.inventory.get("all", {}).get("children", {})
        hosts = children.get(group, {}).get("hosts", {}) or {}
        return list(hosts.keys())

    def host_address(self, name: str, group: str) -> str | None:
        children = self.inventory.get("all", {}).get("children", {})
        entry = (children.get(group, {}).get("hosts", {}) or {}).get(name)
        if isinstance(entry, dict):
            return entry.get("ansible_host") or entry.get(f"{group}_node_address")
        return None

    def env_secret(self, name: str) -> str:
        val = os.environ.get(name, "")
        if val:
            return val
        env_file = REPO_ROOT / "tests" / "lab" / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        return ""


class AnsibleResult:
    """Wynik ad-hoc: ciala odpowiedzi ORAZ jawne bledy per host."""

    def __init__(self) -> None:
        self.bodies: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self.returncode = 0

    def body(self, host: str) -> str:
        return self.bodies.get(host, "")


def run_ansible(ctx: ProbeContext, pattern: str, script: str, timeout: int = 120) -> AnsibleResult:
    """Uruchamia ansible ad-hoc i parsuje wyjście WG PROTOKOLU.

    Host, ktory nie odpowiedzial (UNREACHABLE) albo polecial (FAILED), trafia
    do `errors` z trescia komunikatu - nigdy nie znika z wyniku.
    """
    cmd = [
        "ansible",
        pattern,
        "-i",
        str(REPO_ROOT / os.environ.get(
            "CLUSTER_INVENTORY", f"clusters/{ctx.cluster_name}/inventory.yml"
        )),
        "-m",
        "shell",
        "-a",
        script,
    ]
    res = AnsibleResult()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        res.returncode = 124
        res.errors[pattern] = f"ansible timeout po {timeout}s"
        return res
    except Exception as exc:  # odpalenie ansible w ogole sie nie udalo
        res.returncode = 125
        res.errors[pattern] = f"nie udalo sie uruchomic ansible: {type(exc).__name__}"
        return res

    res.returncode = proc.returncode
    current: str | None = None
    buf: list[str] = []
    for line in (proc.stdout + proc.stderr).splitlines():
        m = _HEADER_RE.match(line.strip())
        if m:
            if current is not None:
                _store(res, current, buf)
            current = m.group("host")
            status = m.group("status").rstrip("!")
            rest = m.group("rest").strip()
            if status in ("FAILED", "UNREACHABLE"):
                res.errors[current] = rest or status
                buf = []
            else:
                res.errors.pop(current, None)
                buf = []
        elif current is not None and current not in res.errors:
            buf.append(line)
    if current is not None:
        _store(res, current, buf)
    return res


def _store(res: AnsibleResult, host: str, buf: list[str]) -> None:
    if host not in res.errors:
        res.bodies[host] = "\n".join(buf).strip()


def require_hosts(
    res: AnsibleResult,
    expected: list[str],
    section: str,
    failures: list[str],
    undetermined: list[str],
) -> None:
    """Konfrontuje wynik z inwentarzem: brak hosta = nierozstrzygniecie.

    To jest serce fail-closed: sonda nie moze przejsc zielono na podzbiorze
    floty ani na pustym zbiorze.
    """
    if not expected:
        undetermined.append(f"{section}: inwentarz nie definiuje hostow - nie ma czego mierzyc")
        return
    for host in expected:
        if host in res.errors:
            undetermined.append(f"{section}: {host} nie odpowiedzial: {res.errors[host][:120]}")
        elif host not in res.bodies:
            undetermined.append(f"{section}: {host} brak w wyniku ansible")


def check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def finish(failures: list[str], undetermined: list[str], ok_summary: str) -> int:
    """Jedyny dozwolony sposob konczenia sondy."""
    if failures:
        print(f"FAIL: {len(failures)} naruszen:")
        for f in failures:
            print(f"  - {f}")
        for u in undetermined:
            print(f"  - (nierozstrzygniete) {u}")
        return EXIT_FAIL
    if undetermined:
        print(f"UNDETERMINED: {len(undetermined)} sekcji bez realnego pomiaru:")
        for u in undetermined:
            print(f"  - {u}")
        return EXIT_UNDETERMINED
    print(f"PASS: {ok_summary}")
    return EXIT_PASS
