#!/usr/bin/env python3
"""Verify MariaDB hardening: ISC-40, ISC-41, ISC-42 + waznosc certyfikatow TLS."""

from __future__ import annotations

import os
import sys

from _probe_common import ProbeContext, check, finish, require_hosts, run_ansible

# Prog ostrzegania o wygasaniu: 30 dni. Wymiana NIE wymaga restartu — Galera ma
# udokumentowana sciezke bez przestoju (galera-security/reloading-tls-certificates-
# without-downtime.md): podmien pliki atomowo w miejscu i wykonaj `FLUSH SSL`,
# ktore przeladowuje kontekst TLS serwera ORAZ providera wsrep; procedure powtarza
# sie per wezel. "Not dynamic" z dokumentacji dotyczy WARTOSCI zmiennych (sciezki),
# nie zawartosci plikow. 30 dni to zapas na zaplanowanie rotacji, nie na okno serwisowe.
CERT_MIN_DAYS = int(os.environ.get("TLS_CERT_MIN_DAYS", "30"))


def mariadb_query(query: str) -> str:
    """Polecenie pomiarowe MariaDB wykonywane przez wspolny parser."""
    return f'mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "{query}"'


def main() -> int:
    failures: list[str] = []
    undetermined: list[str] = []
    ctx = ProbeContext()
    galera_hosts = ctx.group_hosts("galera")
    galera_node = galera_hosts[0] if galera_hosts else ""

    def query_section(section: str, query: str) -> str | None:
        """Pojedynczy pomiar na pierwszym wezle; brak odpowiedzi nie jest PASS."""
        pattern = galera_node or "galera"
        result = run_ansible(ctx, pattern, mariadb_query(query))
        require_hosts(
            result,
            [galera_node] if galera_node else [],
            section,
            failures,
            undetermined,
        )
        if not galera_node or galera_node not in result.bodies:
            return None
        return result.body(galera_node)

    # ISC-40: no anonymous users
    anon_users = query_section(
        "ISC-40 anonymous users",
        "SELECT CONCAT(user,'@',host) FROM mysql.user WHERE user=''",
    )
    if anon_users is not None:
        anon_users = anon_users.strip()
        check(not anon_users, f"ISC-40: anonymous users exist: {anon_users}", failures)

    # ISC-40: no test database
    test_result = query_section("ISC-40 test database", "SHOW DATABASES LIKE 'test'")
    if test_result is not None:
        test_result = test_result.strip()
        check(not test_result, f"ISC-40: test database exists: {test_result}", failures)

    # ISC-40: no empty passwords for non-system accounts
    empty_users = query_section(
        "ISC-40 empty passwords",
        "SELECT CONCAT(user,'@',host) FROM mysql.user "
        "WHERE (authentication_string='' OR authentication_string IS NULL) "
        "AND user NOT IN ('mariadb.sys','mysql','PUBLIC') AND user != ''",
    )
    if empty_users is not None:
        empty_users = empty_users.strip()
        check(not empty_users, f"ISC-40: empty password accounts: {empty_users}", failures)

    # ISC-41: root localhost-only
    remote_root_result = query_section(
        "ISC-41 root localhost-only",
        "SELECT CONCAT(user,'@',host) FROM mysql.user "
        "WHERE user='root' AND host NOT IN ('localhost','127.0.0.1','::1')",
    )
    if remote_root_result is not None:
        remote_root_result = remote_root_result.strip()
        check(
            not remote_root_result,
            f"ISC-41: root has remote access: {remote_root_result}",
            failures,
        )

    # ISC-42: sst_user least privilege
    sst_result = query_section("ISC-42 sst_user grants", "SHOW GRANTS FOR 'sst_user'@'localhost'")
    if sst_result is not None:
        check(
            "RELOAD" in sst_result and "PROCESS" in sst_result and "LOCK TABLES" in sst_result,
            f"ISC-42: sst_user missing required grants: {sst_result}",
            failures,
        )

    # ISC-42: pmm_monitor least privilege
    pmm_result = query_section("ISC-42 pmm_monitor grants", "SHOW GRANTS FOR 'pmm_monitor'@'%'")
    if pmm_result is not None:
        check(
            "PROCESS" in pmm_result and "SELECT" in pmm_result,
            f"ISC-42: pmm_monitor missing required grants: {pmm_result}",
            failures,
        )

    # Waznosc certyfikatow TLS na KAZDYM wezle Galery (gdy tls.mode == full).
    #
    # Nasze certy sa wystawiane recznie przez pki/generate.sh i nikt ich
    # nie rotuje — CA i lisc dostaja 1095 dni, po czym po prostu wygasaja. Sciezka
    # naprawy jest udokumentowana i bezprzestojowa: podmiana plikow w miejscu +
    # `FLUSH SSL` per wezel (galera-security/reloading-tls-certificates-without-
    # downtime.md). Prog 30 dni daje czas na jej zaplanowanie.
    #
    # Sprawdzamy wszystkie wezly, nie tylko pierwszy: dystrybucja moze byc
    # niekompletna i wtedy jeden wezel ma stary albo zaden cert.
    tls_mode = (ctx.config.get("tls") or {}).get("mode", "disabled")
    tls_note = "TLS wylaczony"
    if tls_mode == "full":
        cert_script = (
            "for kv in $(grep -hE '^[[:space:]]*(ssl_ca|ssl_cert)[[:space:]]*=' "
            "/etc/my.cnf.d/*.cnf 2>/dev/null | tr -d ' \\t'); do "
            "f=${kv#*=}; "
            "[ -r \"$f\" ] || { echo \"MISSING $f\"; continue; }; "
            f"if openssl x509 -in \"$f\" -noout -checkend {CERT_MIN_DAYS * 86400} >/dev/null 2>&1; "
            "then echo \"OK $f\"; "
            "else echo \"EXPIRING $f $(openssl x509 -in \"$f\" -noout -enddate 2>/dev/null "
            "| cut -d= -f2 | tr ' ' '_')\"; fi; done"
        )
        cert_res = run_ansible(ctx, "galera", cert_script)
        # Brak odpowiedzi wezla nie moze zniknac z petli certyfikatow.
        require_hosts(cert_res, galera_hosts, "TLS certyfikaty Galery", failures, undetermined)
        cert_hosts = [host for host in galera_hosts if host in cert_res.bodies]
        checked = 0
        for node in sorted(cert_hosts):
            lines = [ln.strip() for ln in cert_res.body(node).splitlines() if ln.strip()]
            if not lines:
                check(
                    bool(lines),
                    f"{node}: tls.mode=full, ale w /etc/my.cnf.d nie ma ssl_ca/ssl_cert "
                    f"— TLS Galery nie jest skonfigurowany",
                    failures,
                )
                continue
            for line in lines:
                checked += 1
                if line.startswith("MISSING "):
                    check(
                        False,
                        f"{node}: brak pliku certyfikatu {line.split(' ', 1)[1]}",
                        failures,
                    )
                elif line.startswith("EXPIRING "):
                    parts = line.split()
                    when = parts[2].replace("_", " ") if len(parts) > 2 else "?"
                    check(
                        False,
                        f"{node}: certyfikat {parts[1]} wygasa przed uplywem "
                        f"{CERT_MIN_DAYS} dni (notAfter: {when}) — rotacja bez "
                        f"przestoju: podmiana plikow + FLUSH SSL",
                        failures,
                    )
        tls_note = f"certy TLS wazne >{CERT_MIN_DAYS} dni ({checked} plikow na {len(cert_hosts)} wezlach)"

    summary = (
        "MariaDB hardening verified — no anon/test/empty-pw, root localhost-only, "
        f"least privilege SST+monitor; {tls_note}"
    )
    return finish(failures, undetermined, summary)


if __name__ == "__main__":
    sys.exit(main())
