#!/usr/bin/env python3
"""Verify the redundant ProxySQL endpoint (Keepalived VIP).

Checks: ISC-24 (VIP assigned to exactly one ProxySQL node when healthy),
ISC-26 (the VIP holder's ProxySQL is actually running — VIP never sits on an
instance whose ProxySQL is down). ISC-25 (failover < RTO) is exercised by the
live failover test, not a steady-state probe.

Reads the endpoint address from cluster.yml.
"""

import os
import re
import subprocess
import sys
import yaml

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")
IFACE = os.environ.get("PROXYSQL_ENDPOINT_INTERFACE", "eth0")

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER_CONFIG = yaml.safe_load(fh)

VIP = CLUSTER_CONFIG["proxysql"]["endpoint"]["address"]


def run_ansible_query(nodes, script):
    """Run a shell snippet on nodes via ansible, return {node: body}."""
    cmd = [
        ANSIBLE, nodes, "-i", INVENTORY, "-m", "ansible.builtin.shell",
        "-a", script, "--fork", "5",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    data = {}
    current_host = None
    current_body = []
    for line in result.stdout.splitlines():
        header = re.match(r'^(\S+)\s*\|\s*\w+\s*\|\s*rc=\d+\s*>>?\s*$', line)
        if header:
            if current_host:
                data[current_host] = "\n".join(current_body).strip()
            current_host = header.group(1)
            current_body = []
        elif current_host:
            current_body.append(line)
    if current_host:
        data[current_host] = "\n".join(current_body).strip()
    return data


def check(condition, message, failures):
    if not condition:
        failures.append(message)


def main():
    failures = []

    # Per-node: does it hold the VIP? is ProxySQL running?
    probe = (
        f"if ip -o -4 addr show dev {IFACE} | grep -q '{VIP}/'; then echo VIP=1; else echo VIP=0; fi; "
        f"if pgrep -x proxysql >/dev/null; then echo PROXYSQL=1; else echo PROXYSQL=0; fi"
    )
    raw = run_ansible_query("proxysql", probe)
    if not raw:
        print("FAIL: no ProxySQL nodes responded to endpoint probe")
        return 1

    state = {}
    for node, body in raw.items():
        vals = dict(
            kv.split("=", 1) for kv in body.split() if "=" in kv
        )
        state[node] = {"vip": vals.get("VIP") == "1", "proxysql": vals.get("PROXYSQL") == "1"}

    vip_holders = [n for n, s in state.items() if s["vip"]]

    # ISC-24: exactly one node holds the VIP
    check(
        len(vip_holders) == 1,
        f"VIP {VIP} held by {len(vip_holders)} nodes {vip_holders} (expected exactly 1)",
        failures,
    )

    # ISC-26: the VIP holder's ProxySQL must be running
    for holder in vip_holders:
        check(
            state[holder]["proxysql"],
            f"{holder} holds VIP {VIP} but its ProxySQL is DOWN (ISC-26 violation)",
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
    port = CLUSTER_CONFIG["proxysql"]["endpoint"].get("port", 6033)
    tls_script = (
        "command -v openssl >/dev/null || { echo NO_OPENSSL; exit 0; }; "
        f"pem=$(echo | timeout 10 openssl s_client -starttls mysql -connect {VIP}:{port} "
        "2>/dev/null | sed -n '/BEGIN CERT/,/END CERT/p'); "
        '[ -n "$pem" ] || { echo NO_TLS; exit 0; }; '
        'echo "$pem" | openssl x509 -noout -subject -issuer | tr "\\n" ";"; echo; '
        'echo "$pem" | openssl x509 -noout -checkend 2592000 >/dev/null '
        "&& echo EXPIRY_OK || echo EXPIRY_SOON"
    )
    tls_raw = run_ansible_query("galera[0]", tls_script)
    tls_note = "nie zweryfikowano"
    for node, body in tls_raw.items():
        if "NO_OPENSSL" in body:
            failures.append(
                f"{node}: brak openssl — nie da sie sprawdzic TLS endpointu {VIP}:{port}; "
                f"sonda nie moze potwierdzic wlasciwosci, ktorej pilnuje"
            )
            continue
        if "NO_TLS" in body:
            failures.append(
                f"endpoint {VIP}:{port} NIE oferuje TLS — klienci schodza do plaintextu "
                f"(sprawdzone z {node})"
            )
            continue
        check(
            "EXPIRY_SOON" not in body,
            f"certyfikat endpointu {VIP}:{port} wygasa w ciagu 30 dni",
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

    if failures:
        print("FAIL: ProxySQL endpoint checks failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    holder = vip_holders[0]
    print(
        f"PASS: ProxySQL endpoint healthy — VIP {VIP} on {holder} "
        f"(ProxySQL running), {len(state)} node(s) evaluated; {tls_note}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
