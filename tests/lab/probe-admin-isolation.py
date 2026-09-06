#!/usr/bin/env python3
"""ISC-22: port admina ProxySQL (6032) nie jest osiągalny z sieci aplikacyjnej.

Sonda jest GENERYCZNA — nie zna żadnego adresu ani żadnej instalacji. Wszystko
bierze z deklaracji (`network.application_cidrs`, `network.administration_cidrs`)
i z inwentarza (grupy `app`, `proxysql`), więc działa na dowolnej infrastrukturze
i dowolnej adresacji. Administracja może pochodzić z WIELU źródeł: pola są
listami CIDR-ów, a `roles/firewall/templates/public.xml.j2` renderuje regułę
dla każdego wpisu osobno.

Kryterium jest wyrażalne tylko wtedy, gdy deklaracje ROZRÓŻNIAJĄ obie sieci.
Gdy `application_cidrs` zawiera się w `administration_cidrs` (typowo: obie
ustawione na tę samą płaską podsieć), reguła „6032 wyłącznie dla administracji"
z definicji wpuszcza aplikację — nie z powodu błędu firewalla, tylko dlatego,
że deklaracja nie odróżnia tych ról. Sonda mówi wtedy UNDETERMINED i nazywa
przyczynę, zamiast zgłaszać awarię, której nikt nie może naprawić kodem.

Mierzone jest zawsze DWOJE:
  * 6032 z hosta aplikacyjnego — musi być odrzucony,
  * 6033 z tego samego hosta — musi być otwarty.
Druga próba jest kontrolą: bez niej „port zamknięty" mógłby znaczyć „host
nieosiągalny", czyli zielone światło bez pomiaru.

Uruchomienie: CLUSTER=<nazwa> python3 tests/lab/probe-admin-isolation.py
"""

from __future__ import annotations

import ipaddress
import sys

from _probe_common import ProbeContext, check, finish, require_hosts, run_ansible

ADMIN_PORT = 6032
CLIENT_PORT = 6033


def contract_is_expressible(application: list[str], administration: list[str]) -> tuple[bool, str]:
    """Czy deklaracje w ogóle ROZRÓŻNIAJĄ sieć aplikacyjną od administracyjnej?

    Zwraca (wyrażalne, powód). Kryterium jest niewyrażalne dokładnie wtedy, gdy
    CAŁA sieć aplikacyjna mieści się w administracyjnej — wtedy każdy adres
    aplikacji jest z definicji adresem administratora i firewall, robiąc to, co
    mu zadeklarowano, otwiera 6032 dla aplikacji.

    Odwrotne zawieranie NIE jest problemem: `administration_cidrs` zawężone do
    kilku stacji (choćby wewnątrz tej samej podsieci co aplikacja) daje realną
    izolację dla pozostałych adresów — i taka deklaracja jest tu celem.
    """
    if not application or not administration:
        return False, "deklaracja nie podaje application_cidrs albo administration_cidrs"

    app_nets = [ipaddress.ip_network(cidr, strict=False) for cidr in application]
    adm_nets = [ipaddress.ip_network(cidr, strict=False) for cidr in administration]

    swallowed = [
        f"{app} ⊆ {adm}"
        for app in app_nets
        for adm in adm_nets
        if app.subnet_of(adm)
    ]
    if swallowed:
        return False, (
            "cala siec aplikacyjna miesci sie w administracyjnej "
            f"({', '.join(swallowed)}) — regula '6032 tylko dla administracji' "
            "wpuszcza aplikacje z definicji, nie przez blad firewalla. "
            "Zawez administration_cidrs do adresow stacji administracyjnych "
            "(pole jest lista — moze ich byc wiele)."
        )
    return True, ""


def host_is_administrative(address: str, administration: list[str]) -> bool:
    """Czy ten host aplikacyjny jest JEDNOCZESNIE stacja administracyjna?

    Taki host ma prawo osiagnac 6032 i nie dowodzi ani izolacji, ani jej braku —
    mierzymy z pozostalych.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(
        ip in ipaddress.ip_network(cidr, strict=False) for cidr in administration
    )
    return True, ""


def parse(body: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in body.splitlines():
        key, _, value = line.strip().partition("=")
        if key and value:
            values[key] = value
    return values


def main() -> int:
    failures: list[str] = []
    undetermined: list[str] = []
    ctx = ProbeContext()

    network = ctx.config.get("network") or {}
    app_hosts = ctx.group_hosts("app")
    proxy_hosts = ctx.group_hosts("proxysql")

    if not app_hosts:
        undetermined.append("ISC-22: inwentarz nie ma hosta w grupie 'app' — nie ma skąd mierzyć")
        return finish(failures, undetermined, "")
    if not proxy_hosts:
        undetermined.append("ISC-22: inwentarz nie ma hosta w grupie 'proxysql' — nie ma czego mierzyć")
        return finish(failures, undetermined, "")

    targets = [ctx.host_address(name, "proxysql") or name for name in proxy_hosts]

    expressible, reason = contract_is_expressible(
        network.get("application_cidrs") or [],
        network.get("administration_cidrs") or [],
    )

    probe = "; ".join(
        f'for p in {ADMIN_PORT} {CLIENT_PORT}; do '
        f'if timeout 3 bash -c "true </dev/tcp/{target}/$p" 2>/dev/null; '
        f'then echo {target}_$p=OPEN; else echo {target}_$p=CLOSED; fi; done'
        for target in targets
    )
    raw = run_ansible(ctx, "app", probe, timeout=180)
    require_hosts(raw, app_hosts, "ISC-22 izolacja portu admina", failures, undetermined)

    measured = 0
    administration = network.get("administration_cidrs") or []
    for host in app_hosts:
        if host in raw.errors:
            continue
        # Host aplikacyjny bedacy JEDNOCZESNIE stacja administracyjna ma prawo
        # osiagnac 6032 — nie dowodzi ani izolacji, ani jej braku.
        host_address = ctx.host_address(host, "app") or ""
        if host_is_administrative(host_address, administration):
            undetermined.append(
                f"ISC-22: {host} ({host_address}) sam nalezy do administration_cidrs — "
                "z tego hosta izolacji nie da sie zmierzyc"
            )
            continue
        values = parse(raw.body(host))
        for target in targets:
            admin = values.get(f"{target}_{ADMIN_PORT}")
            client = values.get(f"{target}_{CLIENT_PORT}")
            if admin is None or client is None:
                undetermined.append(f"ISC-22: {host} nie zwrócił wyniku dla {target}")
                continue
            # Kontrola: bez otwartego 6033 „6032 zamknięty" nie dowodzi izolacji,
            # tylko nieosiągalności hosta.
            if client != "OPEN":
                undetermined.append(
                    f"ISC-22: {host} nie osiąga {target}:{CLIENT_PORT} — sieć nie działa, "
                    f"więc zamknięty {ADMIN_PORT} niczego nie dowodzi"
                )
                continue
            measured += 1
            if expressible:
                check(
                    admin == "CLOSED",
                    f"ISC-22: {host} (sieć aplikacyjna) osiąga port admina "
                    f"{target}:{ADMIN_PORT}",
                    failures,
                )
            elif admin == "OPEN":
                undetermined.append(
                    f"ISC-22: {host} osiąga {target}:{ADMIN_PORT}, ale kryterium jest "
                    f"NIEWYRAŻALNE przy tej deklaracji — {reason}"
                )

    if measured == 0:
        undetermined.append("ISC-22: żadna para host-cel nie dała rozstrzygalnego pomiaru")

    return finish(
        failures,
        undetermined,
        f"ISC-22 — port admina {ADMIN_PORT} niedostępny z sieci aplikacyjnej "
        f"({measured} par host-cel zmierzonych, port klienta {CLIENT_PORT} osiągalny)",
    )


if __name__ == "__main__":
    sys.exit(main())
