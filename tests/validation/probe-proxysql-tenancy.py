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

Od 2026-08-21 warstwa wspolna (platform/<name>/platform.yml) jest jednostka
niezalezna od klastrow — KAZDY klaster jest najemca. Druga pola sondy pilnuje,
zeby sprzezenie wlasnosciowe nie wrocilo:

  * ZADEN cluster.yml nie zawiera `proxysql.role` — pole przestalo istniec razem
    z pojeciem wlasciciela pary; jego powrot oznacza, ze ktos znow uwaza warstwe
    wspolna za wlasnosc klastra, i instaluje pakiety cudzym lockfilem.
  * Zaden klaster nie deklaruje certificate_reference/private_key_reference
    we `proxysql.frontend_tls` — najemca deklaruje TYLKO komu ufa (ca_reference).
    Cert bedacy wlasnoscia najemcy wiazalby zywotnosc endpointu calej floty
    z zyciem jednego klastra.
  * Dla adresu endpointu, ktoremu klaster ufa (deklaruje frontend_tls), istnieje
    DOKLADNIE JEDNA definicja platformy — i to ona dostarcza pelny material
    frontendu. Zero dostawcow = cert, ktoremu nikt nie ufa, nigdy nie wstanie;
    dwie = dwa przebiegi waluja o ten sam VIP i cert.

Klastry o ROZLYNYCH endpointach nie moga na siebie wplynac i nie sa porownywane.

PASS: najemcy wspolnego endpointu sa rozlaczni, a material frontendu ma dokladnie
      jednego dostawce (platforme); pole proxysql.role nie istnieje.
FAIL: kazde naruszenie powyzszych.

`--self-test` falsyfikuje wlasne reguly na kopii definicji w katalogu
tymczasowym (wstrzykniecie `role: owner` MUSI zapalic FAIL) — nigdy na plikach
repozytorium.
"""

import re
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml

DEFAULT_BASE = 10
DEFAULT_APP_USER = "app_user"

# Odwzorowanie z playbooks/vars/proxysql_hostgroups.yml: jedna baza rezerwuje
# cztery kolejne hostgroupy co 10, nie jeden identyfikator.
HOSTGROUP_ROLES = (("writer", 0), ("backup_writer", 10), ("reader", 20), ("offline", 30))
# Material, ktory najemca wolno sobie deklarowac — reszta nalezy do platformy.
TENANT_TLS_REFS = ("ca_reference",)
PLATFORM_TLS_REFS = ("ca_reference", "certificate_reference", "private_key_reference")


def load_definitions(root):
    """Zwraca (clusters, platforms, errors) — definicje pod danym rootem repo."""
    clusters = []
    errors = []
    clusters_dir = root / "clusters"
    for path in sorted(clusters_dir.glob("*/cluster.yml")) if clusters_dir.is_dir() else []:
        try:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{path.relative_to(root).as_posix()}: nie da sie wczytac ({exc})")
            continue
        proxysql = cfg.get("proxysql") or {}
        clusters.append(
            {
                "name": (cfg.get("cluster") or {}).get("name") or path.parent.name,
                "path": path.relative_to(root).as_posix(),
                "endpoint": (proxysql.get("endpoint") or {}).get("address"),
                "frontend_tls": proxysql.get("frontend_tls") or {},
                "role": proxysql.get("role"),
                "base": int(proxysql.get("hostgroup_base", DEFAULT_BASE)),
                "app_user": proxysql.get("app_user", DEFAULT_APP_USER),
                "tls_mode": (cfg.get("tls") or {}).get("mode", "disabled"),
            }
        )

    platforms = []
    platform_dir = root / "platform"
    if platform_dir.is_dir():
        for path in sorted(platform_dir.glob("*/platform.yml")):
            try:
                cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                errors.append(f"{path.relative_to(root).as_posix()}: nie da sie wczytac ({exc})")
                continue
            proxysql = cfg.get("proxysql") or {}
            platforms.append(
                {
                    "name": (cfg.get("platform") or {}).get("name") or path.parent.name,
                    "path": path.relative_to(root).as_posix(),
                    "endpoint": (proxysql.get("endpoint") or {}).get("address"),
                    "frontend_tls": proxysql.get("frontend_tls") or {},
                }
            )
    return clusters, platforms, errors


def check_platform_ownership(clusters, platforms, violations):
    """Sprzezenie wlasnosciowe: proxysql.role, material frontendu, dostawca per endpoint."""
    for cluster in clusters:
        if cluster["role"] is not None:
            violations.append(
                f"{cluster['path']}: zawiera proxysql.role={cluster['role']!r} — pole "
                f"przestalo istniec, bo warstwa wspolna nie jest wlasnoscia zadnego "
                f"klastra. Instalacja pary ProxySQL/VIP/materialu frontendu nalezy do "
                f"platform/*/platform.yml (make platform-*)."
            )
        owned = [
            ref
            for ref in PLATFORM_TLS_REFS
            if ref not in TENANT_TLS_REFS and cluster["frontend_tls"].get(ref)
        ]
        if owned:
            violations.append(
                f"{cluster['path']}: proxysql.frontend_tls deklaruje {', '.join(owned)} — "
                f"najemca moze deklarowac wylacznie {', '.join(TENANT_TLS_REFS)} (komu ufa). "
                f"Material frontendu wdraza definicja platformy; cert we wlasnosci najemcy "
                f"wiaze zywotnosc endpointu calej floty z jednym klastrem."
            )

    platforms_by_endpoint = defaultdict(list)
    for platform in platforms:
        if platform["endpoint"]:
            platforms_by_endpoint[platform["endpoint"]].append(platform)
    for endpoint, defs in sorted(platforms_by_endpoint.items()):
        if len(defs) > 1:
            violations.append(
                f"{endpoint}: {len(defs)} definicje platformy obsluguja ten sam endpoint "
                f"({', '.join(d['path'] for d in defs)}) — material frontendu musi miec "
                f"dokladnie jednego dostawce, inaczej przebiegi platform nadpisuja sobie "
                f"cert i VIP."
            )

    for cluster in clusters:
        if not cluster["frontend_tls"]:
            continue  # klaster nie deklaruje zaufania wspolnemu endpointowi
        defs = platforms_by_endpoint.get(cluster["endpoint"] or "", [])
        if len(defs) != 1:
            violations.append(
                f"{cluster['path']}: deklaruje zaufanie endpointowi {cluster['endpoint']!r}, "
                f"a istnieje dokladnie {len(defs)} definicji platformy o tym adresie — "
                f"bez DOKLADNIE jednej nie ma kto wdrazac certu, ktoremu ten klaster ufa."
            )
            continue
        platform = defs[0]
        missing = [ref for ref in PLATFORM_TLS_REFS if not platform["frontend_tls"].get(ref)]
        if missing:
            violations.append(
                f"{platform['path']}: jest jedynym dostawca materialu frontendu dla "
                f"{cluster['path']}, a nie deklaruje {', '.join(missing)} — endpoint "
                f"pozostanie bez tozsamosci albo bez CA do weryfikacji."
            )


def check_tenant_disjointness(clusters, root, violations):
    """Rozlacznosc najemcow na wspolnym endpoincie (kontrola sprzed wyodrebnienia platformy)."""
    by_endpoint = defaultdict(list)
    for cluster in clusters:
        if cluster["endpoint"]:
            by_endpoint[cluster["endpoint"]].append(cluster)

    for endpoint, tenants in sorted(by_endpoint.items()):
        if len(tenants) < 2:
            continue
        # Baza NIE jest pojedynczym identyfikatorem: zajmuje cztery hostgroupy
        # (base, +10, +20, +30). Porownywanie samych baz przepuszcza sasiadow —
        # 890 i 900 sa "rozne", a jednak 900 to backup_writer najemcy z baza 890,
        # wiec jego failover pisalby do cudzych wezlow. Porownujemy caly zakres.
        claimed = {}
        seen_user = {}
        for entry in tenants:
            collision = False
            for role, offset in HOSTGROUP_ROLES:
                hg = entry["base"] + offset
                owner = claimed.get(hg)
                if owner:
                    violations.append(
                        f"{endpoint}: {entry['name']} ({role} hostgroup {hg}) zajmuje "
                        f"hostgroupe najemcy {owner[0]} ({owner[1]}) — bazy "
                        f"{entry['base']} i {owner[2]} zachodza na siebie, wiec ruch "
                        f"jednego klastra trafi do wezlow drugiego ({entry['path']})"
                    )
                    collision = True
            if not collision:
                for role, offset in HOSTGROUP_ROLES:
                    claimed[entry["base"] + offset] = (entry["name"], role, entry["base"])

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
        tls_clusters = [e["name"] for e in tenants if e["tls_mode"] == "full"]
        if len(tls_clusters) > 1:
            f7_path = root / "playbooks" / "f7_proxysql.yml"
            if not f7_path.exists():
                violations.append(
                    f"{endpoint}: nie moge odczytac playbooks/f7_proxysql.yml, zeby "
                    f"sprawdzic izolacje CA przy {len(tls_clusters)} klastach TLS"
                )
                continue
            f7_src = f7_path.read_text(encoding="utf-8")
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
    return by_endpoint


def check(root):
    """Pelny przebieg sondy; zwraca (violations, najemcy per endpoint)."""
    clusters, platforms, errors = load_definitions(root)
    violations = list(errors)
    check_platform_ownership(clusters, platforms, violations)
    by_endpoint = check_tenant_disjointness(clusters, root, violations)
    return violations, by_endpoint


def self_test(root):
    """Falsyfikacja wlasnych regul na kopii definicji — nigdy na plikach repo."""

    def fresh_clusters(work):
        shutil.rmtree(work / "clusters")
        shutil.copytree(root / "clusters", work / "clusters")

    def inject_role(work):
        target = work / "clusters" / "finalclaude-r10" / "cluster.yml"
        if not target.exists():
            target = next((work / "clusters").glob("*/cluster.yml"), None)
        if target is None:
            return False
        text = target.read_text(encoding="utf-8")
        anchor = "\nproxysql:\n"
        if anchor not in text:
            return False
        target.write_text(
            text.replace(anchor, '\nproxysql:\n  role: "owner"\n', 1), encoding="utf-8"
        )
        return True

    def inject_material(work):
        target = work / "clusters" / "newclaude17-r9" / "cluster.yml"
        if not target.exists():
            candidates = [
                path
                for path in sorted((work / "clusters").glob("*/cluster.yml"))
                if "  frontend_tls:\n" in path.read_text(encoding="utf-8")
            ]
            target = candidates[0] if candidates else None
        if target is None:
            return False
        text = target.read_text(encoding="utf-8")
        marker = "  frontend_tls:\n"
        if marker not in text:
            return False
        target.write_text(
            text.replace(marker, marker + '    certificate_reference: "injected.pem"\n', 1),
            encoding="utf-8",
        )
        return True

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copytree(root / "clusters", work / "clusters")
        if (root / "platform").is_dir():
            shutil.copytree(root / "platform", work / "platform")

        violations, _ = check(work)
        results.append(("czysta kopia definicji przechodzi", not violations))

        injected = inject_role(work)
        violations, _ = check(work)
        results.append(
            (
                "wstrzykniecie proxysql.role=owner do cluster.yml zapala FAIL",
                injected and any("proxysql.role" in v for v in violations),
            )
        )

        fresh_clusters(work)
        injected = inject_material(work)
        violations, _ = check(work)
        results.append(
            (
                "certificate_reference w frontend_tls najemcy zapala FAIL",
                injected and any("certificate_reference" in v for v in violations),
            )
        )

        fresh_clusters(work)
        if (work / "platform").is_dir():
            shutil.rmtree(work / "platform")
        violations, _ = check(work)
        results.append(
            (
                "brak definicji platformy dla zaufanego endpointu zapala FAIL",
                any("definicji platformy" in v for v in violations),
            )
        )

    passed = all(ok for _, ok in results)
    for description, ok in results:
        print(f"  {'OK ' if ok else 'ZONK'} {description}")
    print(f"{'PASS' if passed else 'FAIL'}: samo-test sondy najemstwa ProxySQL")
    return 0 if passed else 1


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--self-test":
        return self_test(Path.cwd())
    if argv:
        print("usage: probe-proxysql-tenancy.py [--self-test]", file=sys.stderr)
        return 2

    violations, by_endpoint = check(Path.cwd())
    if violations:
        print("FAIL: najemcy wspolnego ProxySQL nie sa rozlaczni albo warstwa wspolna nie ma jednoznacznego wlasciciela:")
        for violation in violations:
            print(f"  - {violation}")
        return 1

    shared = {e: len(c) for e, c in by_endpoint.items() if len(c) > 1}
    if shared:
        detail = ", ".join(f"{e} ({n} najemcow)" for e, n in sorted(shared.items()))
        print(f"PASS: wspolne endpointy rozlaczne — {detail}")
    else:
        print("PASS: zaden endpoint ProxySQL nie jest wspoldzielony")
    return 0


if __name__ == "__main__":
    sys.exit(main())
