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
import sys
from collections import defaultdict

import yaml

DEFAULT_BASE = 10
DEFAULT_APP_USER = "app_user"


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
            }
        )

    violations = []
    for endpoint, clusters in sorted(by_endpoint.items()):
        if len(clusters) < 2:
            continue
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
