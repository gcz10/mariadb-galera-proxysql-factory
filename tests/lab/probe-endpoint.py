#!/usr/bin/env python3
"""Verify the redundant ProxySQL endpoint (Keepalived VIP).

Checks: ISC-24 (VIP assigned to exactly one ProxySQL node when healthy),
ISC-26 (the VIP holder's ProxySQL is actually running — VIP never sits on an
instance whose ProxySQL is down). ISC-25 (failover < RTO) is exercised by the
live failover test, not a steady-state probe.

Reads the endpoint address from cluster.yml.
"""

from __future__ import annotations

import os
import sys

from _probe_common import ProbeContext, check, finish, require_hosts, run_ansible

IFACE = os.environ.get("PROXYSQL_ENDPOINT_INTERFACE", "eth0")


def main() -> int:
    failures: list[str] = []
    undetermined: list[str] = []
    ctx = ProbeContext()
    proxysql_hosts = ctx.group_hosts("proxysql")
    galera_hosts = ctx.group_hosts("galera")
    vip = ctx.config["proxysql"]["endpoint"]["address"]

    # Per-node: does it hold the VIP? is ProxySQL running?
    probe = (
        f"if ip -o -4 addr show dev {IFACE} | grep -q '{vip}/'; then echo VIP=1; else echo VIP=0; fi; "
        f"if pgrep -x proxysql >/dev/null; then echo PROXYSQL=1; else echo PROXYSQL=0; fi"
    )
    raw = run_ansible(ctx, "proxysql", probe)
    # Guard VIP: kazdy host grupy musi dostarczyc stan do pomiaru.
    require_hosts(raw, proxysql_hosts, "ISC-24/26 VIP+ProxySQL", failures, undetermined)

    state = {}
    for node in proxysql_hosts:
        if node not in raw.bodies:
            continue
        vals = dict(kv.split("=", 1) for kv in raw.body(node).split() if "=" in kv)
        state[node] = {"vip": vals.get("VIP") == "1", "proxysql": vals.get("PROXYSQL") == "1"}

    vip_holders = [n for n, s in state.items() if s["vip"]]

    # ISC-24: exactly one node holds the VIP
    if state and len(state) == len(proxysql_hosts):
        check(
            len(vip_holders) == 1,
            f"VIP {vip} held by {len(vip_holders)} nodes {vip_holders} (expected exactly 1)",
            failures,
        )

    # ISC-26: the VIP holder's ProxySQL must be running
    for holder in vip_holders:
        check(
            state[holder]["proxysql"],
            f"{holder} holds VIP {vip} but its ProxySQL is DOWN (ISC-26 violation)",
            failures,
        )

    # TLS punktu wejscia aplikacji.
    #
    # Ta sonda pilnowala dotad wylacznie tego, CZY VIP gdzies wisi i czy stoi za
    # nim zywy ProxySQL. Wlasciwosci ostatniego przeskoku — tego, po ktorym
    # faktycznie laczy sie aplikacja — nie sprawdzal nikt: 110 linii bez slowa
    # o TLS, przy `tls.mode: full` zadeklarowanym w cluster.yml.
    #
    # Zmierzone na zywej flocie: ProxySQL serwowal tu certyfikat, ktorego nikt w
    # repo nie provisionowal ani nie rotowal —
    #   subject=CN=ProxySQL_Auto_Generated_Server_Certificate
    #   issuer =CN=ProxySQL_Auto_Generated_CA_Certificate
    # NAPRAWIONE: owner pary ProxySQL wdraza material z `proxysql.frontend_tls`
    # (CA warstwy wspolnej, nie klastra — jeden cert obsluguje cala flote), a
    # sonda ponizej raportuje wystawce, wiec powrot auto-certu bylby widoczny.
    #
    # To nie jest problem tylko dla klientow o zaostrzonej konfiguracji. Release
    # notes MariaDB 11.4 (mariadb-docs, what-is-mariadb-114) mowia wprost:
    # "SSL/TLS is now enabled in the server by default, with self-signed
    # certificates automatically generated if no server certificate is provided.
    # Clients now require SSL and have server certificate verification enabled
    # by default". Nasz lockfile przypina wlasnie 11.4, wiec DOMYSLNIE
    # skonfigurowany klient odbija sie tu z "Certificate verification failure";
    # polaczenie wymaga jawnego --ssl-verify-server-cert=0. Zmierzone:
    # mariadb-slap przez VIP odmawia, ten sam klient z wylaczona weryfikacja laczy sie.
    #
    # Ta sama zmiana w 11.4 tlumaczy, dlaczego `tls.mode: disabled` (finalclaude-r10)
    # NIE oznacza plaintextu na 3306: serwer i tak podnosi TLS z auto-certem.
    # "disabled" w cluster.yml znaczy "nie provisionujemy i nie wymagamy", nie
    # "ruch jest nieszyfrowany".
    #
    # Asercjami sa dwie rzeczy falsyfikowalne bez decyzji projektowej: endpoint
    # MUSI oferowac TLS (inaczej klienci cicho schodza do plaintextu) i cert MUSI
    # byc wazny z zapasem 30 dni. Brak zaufanego lancucha jest RAPORTOWANY w
    # linii wyniku — stan swiadomie przyjety, ale nie przemilczany.
    port = ctx.config["proxysql"]["endpoint"].get("port", 6033)
    tls_script = (
        "command -v openssl >/dev/null || { echo NO_OPENSSL; exit 0; }; "
        f"pem=$(echo | timeout 10 openssl s_client -starttls mysql -connect {vip}:{port} "
        "2>/dev/null | sed -n '/BEGIN CERT/,/END CERT/p'); "
        '[ -n "$pem" ] || { echo NO_TLS; exit 0; }; '
        'echo "$pem" | openssl x509 -noout -subject -issuer | tr "\\n" ";"; echo; '
        'echo "$pem" | openssl x509 -noout -checkend 2592000 >/dev/null '
        "&& echo EXPIRY_OK || echo EXPIRY_SOON"
    )
    tls_raw = run_ansible(ctx, "galera[0]", tls_script)
    # Brak odpowiedzi TLS nie moze zostac cichym "nie zweryfikowano" pod PASS.
    require_hosts(tls_raw, galera_hosts[:1], "TLS endpoint", failures, undetermined)
    tls_note = "nie zweryfikowano"
    for node in galera_hosts[:1]:
        if node not in tls_raw.bodies:
            continue
        body = tls_raw.body(node)
        if "NO_OPENSSL" in body:
            check(
                False,
                f"{node}: brak openssl — nie da sie sprawdzic TLS endpointu {vip}:{port}; "
                f"sonda nie moze potwierdzic wlasciwosci, ktorej pilnuje",
                failures,
            )
            continue
        if "NO_TLS" in body:
            check(
                False,
                f"endpoint {vip}:{port} NIE oferuje TLS — klienci schodza do plaintextu "
                f"(sprawdzone z {node})",
                failures,
            )
            continue
        check(
            "EXPIRY_SOON" not in body,
            f"certyfikat endpointu {vip}:{port} wygasa w ciagu 30 dni",
            failures,
        )
        subject = issuer = "?"
        for part in body.replace("\n", ";").split(";"):
            part = part.strip()
            if part.startswith("subject="):
                subject = part[len("subject="):].strip()
            elif part.startswith("issuer="):
                issuer = part[len("issuer="):].strip()
        if "ProxySQL_Auto_Generated" in issuer:
            tls_note = (
                f"TLS obecny, ale cert self-signed ProxySQL ({subject}) — klient "
                f"weryfikujacy lancuch sie NIE polaczy"
            )
        else:
            tls_note = f"TLS obecny, wystawca: {issuer}"

    holder = vip_holders[0] if vip_holders else "?"
    summary = (
        f"ProxySQL endpoint healthy — VIP {vip} on {holder} "
        f"(ProxySQL running), {len(state)} node(s) evaluated; {tls_note}"
    )
    return finish(failures, undetermined, summary)


if __name__ == "__main__":
    sys.exit(main())
