#!/usr/bin/env python3
"""Klastry dzielace jedna pare ProxySQL musza byc rozlaczne w jej tabelach.

Jedna para ProxySQL w HA obsluguje CALA flote. ProxySQL trzyma jednak
`mysql_servers`, `mysql_galera_hostgroups` i `mysql_users` w tabelach
GLOBALNYCH, wiec rozdzielenie klastrow jest wylacznie kwestia rozlacznych
identyfikatorow:

  * `proxysql.hostgroup_base`  -> writer/backup/reader/offline = base, +10, +20, +30
    f7_proxysql.yml wykonuje `DELETE FROM mysql_servers WHERE hostgroup_id IN (...)`
    dla WLASNYCH czterech ID. Przy wspolnej bazie drugi klaster kasuje backendy
    pierwszego i wypycha go z ProxySQL.

  * `proxysql.app_user`        -> wpis w `mysql_users`
    `default_hostgroup` tego wpisu decyduje, do KTOREGO klastra trafia polaczenie.
    Wspolna nazwa = drugi klaster przejmuje ruch aplikacji pierwszego.

Kolizja nie objawia sie bledem: `f7` konczy sie sukcesem, a pierwszy klaster po
prostu znika z ProxySQL. Dlatego pilnuje tego statyczna sonda, a nie recenzja.

Klastry o ROZNYCH endpointach nie moga na siebie wplynac i nie sa porownywane.

PASS: kazda para klastrow na wspolnym endpoincie ma rozlaczne hostgroupy i app_user.
FAIL: kolizja bazy hostgroup albo nazwy uzytkownika.
"""

import glob
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

DEFAULT_BASE = 10
DEFAULT_APP_USER = "app_user"
DEFAULT_ROLE = "owner"


def main():
    by_endpoint = defaultdict(list)
    for path in sorted(glob.glob("clusters/*/cluster.yml")):
        try:
            with open(path, encoding="utf-8") as handle:
                cfg = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError) as exc:
            print(f"FAIL: {path}: nie da sie wczytac ({exc})")
            return 1
        proxysql = (cfg or {}).get("proxysql") or {}
        endpoint = (proxysql.get("endpoint") or {}).get("address")
        if not endpoint:
            continue
        by_endpoint[endpoint].append(
            {
                "name": (cfg.get("cluster") or {}).get("name", path),
                "path": path,
                "base": int(proxysql.get("hostgroup_base", DEFAULT_BASE)),
                "app_user": proxysql.get("app_user", DEFAULT_APP_USER),
                "role": proxysql.get("role", DEFAULT_ROLE),
                "tls_mode": ((cfg or {}).get("tls") or {}).get("mode", "disabled"),
            }
        )

    violations = []
    for endpoint, clusters in sorted(by_endpoint.items()):
        if len(clusters) < 2:
            continue
        owners = [e["name"] for e in clusters if e["role"] == "owner"]
        if len(owners) != 1:
            violations.append(
                f"{endpoint}: dokladnie JEDEN klaster musi miec proxysql.role=owner, "
                f"jest {len(owners)} ({owners or 'brak'}). Dwoch ownerow nadpisze sobie "
                f"pakiety ProxySQL z roznych lockfile i zdubluje eksportery w PMM; "
                f"zero ownerow oznacza, ze nikt tej warstwy nie instaluje."
            )
        seen_base = {}
        seen_user = {}
        for entry in clusters:
            other = seen_base.get(entry["base"])
            if other:
                violations.append(
                    f"{endpoint}: {entry['name']} i {other} maja te sama "
                    f"proxysql.hostgroup_base={entry['base']} — f7 drugiego skasuje "
                    f"backendy pierwszego ({entry['path']})"
                )
            else:
                seen_base[entry["base"]] = entry["name"]

            other_user = seen_user.get(entry["app_user"])
            if other_user:
                violations.append(
                    f"{endpoint}: {entry['name']} i {other_user} maja ten sam "
                    f"proxysql.app_user={entry['app_user']!r} — wpis w mysql_users "
                    f"jest globalny, wiec ruch trafi do jednego klastra ({entry['path']})"
                )
            else:
                seen_user[entry["app_user"]] = entry["name"]

        # CA backendu tez jest zasobem wspoldzielonym. Do 2026-08-15 f7 zapisywalo
        # je do GLOBALNEJ zmiennej `mysql-ssl_p2s_ca` pod stala sciezka
        # /etc/mysql/tls/ca.pem, wiec drugi klaster z tls.mode=full nadpisywal CA
        # pierwszego — bez zadnego bledu, po prostu przestawal przechodzic
        # weryfikacje. Naprawione przez `mysql_servers_ssl_params` (ssl_ca per
        # hostname, ProxySQL >= 2.6) i sciezke per klaster.
        #
        # Ta sonda NIE zabrania juz dwoch klastrow TLS. Sprawdza, czy mechanizm,
        # ktory to umozliwil, nadal jest w kodzie — cofniecie naprawy ma znowu
        # zapalic czerwone, zamiast po cichu wrocic do nadpisywania CA.
        tls_clusters = [e["name"] for e in clusters if e["tls_mode"] == "full"]
        if len(tls_clusters) > 1:
            f7_src = Path("playbooks/f7_proxysql.yml").read_text(encoding="utf-8")
            if "mysql_servers_ssl_params" not in f7_src:
                violations.append(
                    f"{endpoint}: {len(tls_clusters)} klastry maja tls.mode=full "
                    f"({', '.join(sorted(tls_clusters))}), a f7_proxysql.yml nie uzywa "
                    f"mysql_servers_ssl_params — CA idzie do globalnej zmiennej i drugi "
                    f"klaster nadpisze CA pierwszego."
                )
            if re.search(r"UPDATE\s+global_variables[^;]*mysql-ssl_p2s_ca", f7_src, re.S | re.I):
                violations.append(
                    f"{endpoint}: f7_proxysql.yml nadal zapisuje globalne "
                    f"`mysql-ssl_p2s_ca`. Przy {len(tls_clusters)} klastrach TLS ta zmienna "
                    f"jest pulapka: nadpisuje ja ostatni przebieg."
                )
            if "proxysql_cluster_tls_dir" not in f7_src or "cluster.name" not in f7_src:
                violations.append(
                    f"{endpoint}: CA backendu nie ma sciezki per klaster. Wiersze "
                    f"mysql_servers_ssl_params wskazujace na TEN SAM plik kolidowaly by "
                    f"tak samo jak zmienna globalna."
                )

    if violations:
        print("FAIL: klastry na wspolnym ProxySQL nie sa rozlaczne:")
        for violation in violations:
            print(f"  - {violation}")
        return 1

    shared = {e: len(c) for e, c in by_endpoint.items() if len(c) > 1}
    if shared:
        detail = ", ".join(f"{e} ({n} klastry)" for e, n in sorted(shared.items()))
        print(f"PASS: wspolne endpointy rozlaczne — {detail}")
    else:
        print("PASS: zaden endpoint ProxySQL nie jest wspoldzielony")
    return 0


if __name__ == "__main__":
    sys.exit(main())
