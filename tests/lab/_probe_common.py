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

import base64
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[2]

# Kod wyjscia 2 = "nie da sie rozstrzygnac" (martwa infrastruktura pomiarowa).
# Kod 1 = FAIL (zmierzono i jest zle). Kod 0 = PASS (zmierzono i jest dobrze).
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNDETERMINED = 2

_HEADER_RE = re.compile(
    r"^(?P<host>\S+)\s+\|\s+(?P<status>CHANGED|SUCCESS|FAILED!?|UNREACHABLE!?)(?P<rest>.*)$"
)


def pmm_ssl_context(pmm_config: dict) -> ssl.SSLContext:
    """Use declared PMM trust; environment variables cannot weaken it."""
    validate = pmm_config.get("validate_certs", True)
    if not isinstance(validate, bool):
        raise ValueError("monitoring.pmm.validate_certs must be a boolean")
    ca_reference = pmm_config.get("ca_reference")
    context = ssl.create_default_context(
        cafile=str(REPO_ROOT / ca_reference) if ca_reference else None
    )
    if not validate:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Laczy sie POD JEDEN adres, a certyfikat weryfikuje POD INNA nazwe."""

    def __init__(self, *args, tls_hostname: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._tls_hostname = tls_hostname

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._tls_hostname)


def pmm_effective_url(pmm_config: dict) -> tuple[str, str]:
    """Zwraca (adres do polaczenia, adres zadeklarowany).

    `PMM_SERVER_URL` przestawia PUNKT POLACZENIA sondy — potrzebne, gdy host
    kontrolny jest odciety od LAN (prywatnosc sieci lokalnej macOS, ISA
    2026-09-09) i jedyna droga do API jest tunel na loopback. Zaufania to NIE
    rusza: `pmm_get_json` weryfikuje certyfikat pod nazwa ZADEKLAROWANA.
    Rozniace sie adresy sa sygnalem dla sondy, ze ma o tym powiedziec w wyniku.
    """
    declared = (pmm_config.get("server_url") or "").rstrip("/")
    override = os.environ.get("PMM_SERVER_URL", "").strip().rstrip("/")
    return (override or declared), declared


def pmm_get_json(connect_url: str, declared_url: str, user: str, password: str,
                 path: str, pmm_config: dict):
    """GET do API PMM: polaczenie pod `connect_url`, weryfikacja pod `declared_url`."""
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    context = pmm_ssl_context(pmm_config)
    target = urlsplit(connect_url)
    declared = urlsplit(declared_url or connect_url)
    if target.hostname == declared.hostname:
        request = Request(f"{connect_url}{path}", headers={"Authorization": f"Basic {token}"})
        with urlopen(request, context=context, timeout=10) as response:
            return json.load(response)
    conn = _PinnedHTTPSConnection(
        target.hostname,
        target.port or 443,
        context=context,
        timeout=10,
        tls_hostname=declared.hostname,
    )
    try:
        conn.request("GET", path, headers={"Authorization": f"Basic {token}"})
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError(f"PMM {path} zwrocilo HTTP {response.status}")
        return json.load(response)
    finally:
        conn.close()


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


# === Poświadczenia klienta bez sekretu w argv (ISC-43) =======================

# Haslo aplikacji nie moze trafic do argv procesu `ansible` na kontrolerze
# (widoczne kazdemu przez `ps`), ani do linii polecenia na hoscie zdalnym.
# Wzorzec jest tu wyjatkowy, bo jedna implementacja obsluguje zarowno
# `mariadb`, jak i `mariadb-slap` (oba czytaja `[client]` z pliku opcji,
# potwierdzone --print-defaults). Te same ucieczki cytowania sa testowane
# jednostkowo w test_quorum_evidence.py.


def client_profile_text(user: str, password: str) -> str:
    """Zawartosc pliku opcji klienta [client], wartosci z cytowaniem.

    Zrodlo prawdy dla ucieczek: `_quorum_evidence.option_file_quote` —
    tabela ucieczek z dokumentacji plikow opcji, testowana w
    test_quorum_evidence.py. Duplikat tabeli tutaj oznaczalby dwie
    konwencje cytowania w tym samym katalogu.
    """
    from _quorum_evidence import option_file_quote

    return (
        "[client]\n"
        f"user={option_file_quote(user)}\n"
        f"password={option_file_quote(password)}\n"
    )


def install_client_profile(
    ansible_bin: str,
    inventory: str,
    host: str,
    remote_path: str,
    user: str,
    password: str,
    timeout: int = 120,
) -> None:
    """Wgraj plik opcji klienta na `host` (mode=0600 owner=root).

    Lokalny plik tymczasowy powstaje z 0600 i ginie NATYCHMIAST po
    transferze — nieprzydzielony sekret w /tmp kontrolera to ta sama klasa
    wycieku, ktora ta funkcja zamyka. Rzuca RuntimeError przy porazce, zeby
    wywolujaca sonda zlozyla ja do `failures` zamiast przechodzc zielono.
    """
    fd, local_path = tempfile.mkstemp(prefix="isa-client-profile-", suffix=".cnf")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(client_profile_text(user, password))
        proc = subprocess.run(
            [
                ansible_bin,
                host,
                "-i",
                inventory,
                "-m",
                "ansible.builtin.copy",
                "-a",
                f"src={local_path} dest={remote_path} mode=0600 owner=root",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            detail = (proc.stdout + proc.stderr).strip()[:300] or f"rc={proc.returncode}"
            raise RuntimeError(f"install_client_profile na {host}: {detail}")
    finally:
        if os.path.exists(local_path):
            os.unlink(local_path)


def remove_client_profile(
    ansible_bin: str,
    inventory: str,
    host: str,
    remote_path: str,
    timeout: int = 60,
) -> None:
    """Usun profil klienta z hosta. Rzuca RuntimeError przy porazce.

    Porazka usuniecia MUSI zostac zgloszona: pozostawiony plik z haslem
    aplikacji na hoscie to regresja dokladnie tej wlasnosci, ktora ta
    sekcja pilnuje. `state=absent` jest idempotentny, wiec drugi przebieg
    jest no-op.
    """
    proc = subprocess.run(
        [
            ansible_bin,
            host,
            "-i",
            inventory,
            "-m",
            "ansible.builtin.file",
            "-a",
            f"path={remote_path} state=absent",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip()[:300] or f"rc={proc.returncode}"
        raise RuntimeError(f"remove_client_profile na {host}: {detail}")
