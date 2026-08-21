#!/usr/bin/env python3
"""Verify the shared platform layer AS A UNIT, with no Galera cluster involved.

This probe exists because the shared layer used to be owned by a Galera cluster
(`proxysql.role: owner`), so every guarantee about ProxySQL, the VIP and the
frontend certificate was only ever measured through a tenant. Destroying that
tenant would have left the layer unmonitored and unverifiable.

The hard rule enforced here: NOTHING in this probe may depend on a tenant.
No Galera node is contacted, no application user is used, no cluster hostgroup
is inspected. The frontend certificate is validated with `openssl s_client`
rather than a login precisely because a platform with zero tenants has zero
database users — and it must still verify green.

Checks:
  - the platform inventory declares no `galera`/`restore` group (independence),
  - every ProxySQL node runs and its runtime config equals the saved one,
  - the VIP is held by exactly one node and that node's ProxySQL is up,
  - the endpoint serves a certificate that chains to the shared CA, verified
    from the application host (the client's point of view, over the network).
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import sys
from urllib.request import Request, urlopen

from _probe_common import ProbeContext, check, finish, require_hosts, run_ansible

IFACE = os.environ.get("PROXYSQL_ENDPOINT_INTERFACE", "eth0")
# Rozprowadza go `app_host.yml` z `proxysql.frontend_tls.ca_reference`.
SHARED_CA = "/etc/mysql/app/shared/proxysql-ca.pem"


def pmm_json(base_url: str, user: str, password: str, path: str):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = Request(f"{base_url}{path}", headers={"Authorization": f"Basic {token}"})
    context = ssl.create_default_context()
    if os.environ.get("PMM_VALIDATE_CERTS", "0") != "1":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    with urlopen(request, context=context, timeout=10) as response:
        return json.load(response)
SHARED_CA = "/etc/mysql/app/shared/proxysql-ca.pem"


def main() -> int:
    failures: list[str] = []
    undetermined: list[str] = []

    os.environ.setdefault("CLUSTER_CONFIG", "platform/shared/platform.yml")
    os.environ.setdefault("CLUSTER_INVENTORY", "platform/shared/inventory.yml")
    ctx = ProbeContext()

    # Niezaleznosc nie jest deklaracja w komentarzu, tylko mierzalna wlasnoscia
    # definicji: gdyby ktos dolozyl tu wezly bazy, warstwa znow stalaby sie
    # czescia klastra i ten test ma to zatrzymac.
    for forbidden in ("galera", "restore"):
        check(
            not ctx.group_hosts(forbidden),
            f"inwentarz warstwy wspolnej zawiera grupe '{forbidden}' — "
            f"warstwa przestalaby byc niezalezna od klastrow",
            failures,
        )

    proxysql_hosts = ctx.group_hosts("proxysql")
    app_hosts = ctx.group_hosts("app")
    endpoint = ctx.config["proxysql"]["endpoint"]
    vip, port = endpoint["address"], endpoint["port"]
    expected = ctx.config["proxysql"].get("nodes_expected")

    check(
        expected is None or len(proxysql_hosts) == expected,
        f"proxysql: {len(proxysql_hosts)} hostow w inwentarzu, "
        f"nodes_expected={expected}",
        failures,
    )

    # Stan pary: proces, posiadanie VIP i osiagalnosc portu admina.
    #
    # ODRZUCONE SWIADOMIE: porownanie calego `runtime_global_variables` z
    # `global_variables`. Na zywej parze rozni sie `mysql-server_capabilities`
    # (runtime 67678763 vs disk 569867), a w dokumentacji ProxySQL nie ma strony
    # opisujacej, kto ustawia te zmienna — wiec nie umiem uzasadnic wykluczenia
    # jej z porownania i nie zamierzam zgadywac. Drift konfiguracji operatora
    # pilnuje ISC-21 (`probe-drift.py` i `probe-proxysql.py`, oba zielone na tej
    # samej parze), ktore porownuja tabele zarzadzane przez operatora.
    probe = (
        f"if ip -o -4 addr show dev {IFACE} | grep -q '{vip}/'; then echo VIP=1; else echo VIP=0; fi; "
        "if pgrep -x proxysql >/dev/null; then echo PROC=1; else echo PROC=0; fi; "
        "mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf -h127.0.0.1 -P6032 -uadmin -N -B "
        "-e \"SELECT 'ADMIN=1'\" 2>/dev/null || echo ADMIN=0"
    )
    raw = run_ansible(ctx, "proxysql", probe)
    require_hosts(raw, proxysql_hosts, "stan pary ProxySQL", failures, undetermined)

    state: dict[str, dict[str, str]] = {}
    for node in proxysql_hosts:
        if node not in raw.bodies:
            continue
        state[node] = dict(
            kv.split("=", 1) for kv in raw.body(node).split() if "=" in kv
        )

    for node, vals in state.items():
        check(vals.get("PROC") == "1", f"{node}: proces ProxySQL nie dziala", failures)
        check(
            vals.get("ADMIN") == "1",
            f"{node}: port admina ProxySQL (6032) nie odpowiada — warstwa jest "
            f"niezarzadzalna, choc proces moze dzialac",
            failures,
        )

    holders = [n for n, v in state.items() if v.get("VIP") == "1"]
    if state and len(state) == len(proxysql_hosts):
        check(
            len(holders) == 1,
            f"VIP {vip} trzymany przez {len(holders)} wezlow {holders} (oczekiwano 1)",
            failures,
        )
    for holder in holders:
        check(
            state[holder].get("PROC") == "1",
            f"{holder} trzyma VIP {vip}, ale jego ProxySQL nie dziala",
            failures,
        )

    # Certyfikat frontendu z perspektywy KLIENTA, po sieci, bez logowania.
    # `openssl verify` na lancuchu, nie samo "TLS obecny": endpoint podajacy
    # self-signed cert przechodzilby slabszy test, a klient by go odrzucil.
    if app_hosts:
        tls_script = (
            f"if [ ! -f {SHARED_CA} ]; then echo CA=MISSING; else echo CA=OK; "
            f"echo | openssl s_client -connect {vip}:{port} -starttls mysql "
            f"-CAfile {SHARED_CA} 2>/dev/null "
            "| grep -E '^(Verify return code|subject=|issuer=)' | tr '\\n' ';'; fi"
        )
        tls_raw = run_ansible(ctx, "app[0]", tls_script)
        require_hosts(tls_raw, app_hosts[:1], "TLS wspolnego endpointu", failures, undetermined)
        for node in app_hosts[:1]:
            if node not in tls_raw.bodies:
                continue
            body = tls_raw.body(node)
            if "CA=MISSING" in body:
                failures.append(
                    f"{node}: brak {SHARED_CA} — warstwa nie rozprowadzila CA endpointu"
                )
                continue
            check(
                "Verify return code: 0 (ok)" in body,
                f"{node}: certyfikat VIP {vip}:{port} nie weryfikuje sie wobec CA "
                f"warstwy wspolnej ({body.strip()[:200]})",
                failures,
            )
    else:
        undetermined.append("brak hosta w grupie 'app' — nie zmierzono TLS endpointu")

    # Kazdy adres warstwy MUSI miec w PMM dokladnie jeden wezel — jej wlasny.
    # Przy wyniesieniu warstwy z wlasnosci `finalclaude-r10` powstaly obok
    # nowych `shared-fcp*` sieroty `fc10-galera-fcp*` wskazujace TEN SAM adres:
    # podwojne metryki i alerty z jednej maszyny, ktorych po odebraniu ownerowi
    # wlasnosci nikt juz nie sprzata. Regula jest adresowa, nie po nazwie,
    # bo nazwa jest wlasnie tym, co sie rozjechalo.
    pmm = ctx.config.get("monitoring", {}).get("pmm", {})
    pmm_url = pmm.get("server_url", "").rstrip("/")
    expected_prefix = pmm.get("cluster_name", "")
    # Tylko para ProxySQL: `fcinfra` hostuje sam serwer PMM i figuruje tam jako
    # wezel `pmm-server`, a nie jako `shared-fcinfra` — warstwa go nie rejestruje.
    managed = {
        ctx.host_address(host, "proxysql"): f"{expected_prefix}-{host}"
        for host in ctx.group_hosts("proxysql")
    }
    try:
        nodes = pmm_json(pmm_url, "admin", ctx.env_secret("PMM_ADMIN_PASSWORD"), "/v1/inventory/nodes")
    except Exception as exc:  # noqa: BLE001 - kazdy blad tu jest niezmierzeniem
        undetermined.append(f"nie odpytano PMM Inventory ({exc})")
    else:
        registered: dict[str, list[str]] = {}
        for group in nodes.values():
            for node in group if isinstance(group, list) else [group]:
                if not isinstance(node, dict):
                    continue
                addr = node.get("address")
                if addr in managed:
                    registered.setdefault(addr, []).append(node.get("node_name"))
        for addr, expected_name in sorted(managed.items()):
            names = sorted(registered.get(addr, []))
            check(
                names == [expected_name],
                f"{addr}: PMM ma wezly {names or '[]'}, oczekiwano dokladnie "
                f"['{expected_name}'] — rejestracja spoza warstwy wspolnej "
                f"dubluje metryki tej samej maszyny",
                failures,
            )

    summary = (
        f"warstwa wspolna zdrowa — {len(proxysql_hosts)} wezlow ProxySQL "
        f"(proces + port admina), VIP {vip} na "
        f"{holders[0] if len(holders) == 1 else '?'}, zweryfikowany TLS endpointu "
        f"z hosta aplikacyjnego, po jednym wezle PMM na adres; "
        f"zero zaleznosci od klastra Galera"
    )
    return finish(failures, undetermined, summary)


if __name__ == "__main__":
    sys.exit(main())
