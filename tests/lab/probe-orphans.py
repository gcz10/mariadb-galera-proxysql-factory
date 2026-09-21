#!/usr/bin/env python3
"""Sonda sprawdzajaca brak sierot po deprovisioningu klastra.

Sprawdza:
  1. PMM Inventory (nodes, services, agents)
  2. Grafana Alerting (alert rules, contact points, folders, policy routes)
  3. MinIO (konta serwisowe; bucket nie jest badany)
  4. ProxySQL (hostgroups, users, SSL params)
"""
from __future__ import annotations

import base64
import json
import re
import shlex
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

try:
    import yaml
except ImportError:
    print("FAIL: brak modulu PyYAML", file=sys.stderr)
    sys.exit(2)

from _probe_common import ProbeContext, finish, pmm_ssl_context, require_hosts, run_ansible
from alert_identity import alert_uid_prefixes


REPO_ROOT = Path(__file__).resolve().parents[2]


def api_get(
    path: str,
    server_url: str,
    auth_header: str,
    undetermined: list[str],
    unavailable: set[str],
    pmm_config: dict,
) -> dict | list:
    """Pobierz odpowiedz PMM; blad nigdy nie staje sie pustym pomiarem."""
    url = server_url + path
    req = urllib.request.Request(
        url,
        headers={"Authorization": auth_header, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=pmm_ssl_context(pmm_config), timeout=15) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            if status != 200:
                undetermined.append(f"PMM API {path}: HTTP {status}")
                unavailable.add(path)
                return {}
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        undetermined.append(f"PMM API {path}: HTTP {exc.code}")
        unavailable.add(path)
        return {}
    except UnicodeError as exc:
        undetermined.append(f"PMM API {path}: nieprawidlowe dane ({type(exc).__name__})")
        unavailable.add(path)
        return {}
    except Exception as exc:
        undetermined.append(f"PMM API {path}: blad polaczenia ({type(exc).__name__})")
        unavailable.add(path)
        return {}

    if not raw.strip():
        undetermined.append(f"PMM API {path}: pusta odpowiedz HTTP 200")
        unavailable.add(path)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        undetermined.append(f"PMM API {path}: nieprawidlowy JSON ({type(exc).__name__})")
        unavailable.add(path)
        return {}
    if not isinstance(data, (dict, list)):
        undetermined.append(f"PMM API {path}: JSON ma nieprawidlowa strukture")
        unavailable.add(path)
        return {}
    return data


def _load_mc_image(ctx: ProbeContext, undetermined: list[str]) -> str:
    """Obraz `mc` z lockfile WSKAZANEGO przez definicje, nie z sztywnej sciezki.

    Rola i playbooki laduja `(versions | default({})).lock_file |
    default('versions/versions.lock.yml')` — klaster przypina mc_image do
    WLASNEGO lockfile (np. versions-el10-123.lock.yml). Sonda czytajaca zawsze
    `versions/versions.lock.yml` porownywalaby konta z obrazem, ktory ta
    definicja nie wdrozy.
    """
    lock_reference = (
        (ctx.config.get("versions") or {}).get("lock_file")
        or "versions/versions.lock.yml"
    )
    lock_path = REPO_ROOT / lock_reference
    try:
        lock = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
        if not isinstance(lock, dict):
            raise ValueError("lock nie jest obiektem")
        minio_lock = lock.get("minio")
        if not isinstance(minio_lock, dict):
            raise ValueError("brak sekcji minio")
        image = minio_lock.get("mc_image")
        digest = minio_lock.get("mc_image_digest")
        if not isinstance(image, str) or not image:
            raise ValueError("brak minio.mc_image")
        if not isinstance(digest, str) or not digest:
            raise ValueError("brak minio.mc_image_digest")
        return f"{image}@{digest}"
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        undetermined.append(
            f"MinIO: nie mozna odczytac {lock_reference} ({type(exc).__name__})"
        )
        return ""


def _minio_probe_script(image_ref: str, username: str, password: str) -> str:
    """Zbuduj jeden zdalny skrypt: list, info per klucz i walidacja JSON."""
    image_host = (
        f"http://{quote(username, safe='')}:{quote(password, safe='')}@localhost:9000"
    )
    remote_python = textwrap.dedent(
        """\
        import json
        import os
        import subprocess
        import sys

        image = os.environ["MINIO_MC_IMAGE"]
        mc_host = os.environ["MINIO_MC_HOST"]
        base = [
            "docker",
            "run",
            "--rm",
            "--network",
            "container:minio",
            "-e",
            "MC_HOST_m=" + mc_host,
            image,
        ]

        def run_mc(arguments):
            return subprocess.run(
                base + arguments,
                capture_output=True,
                text=True,
                check=False,
            )

        def fail(message):
            print(message, file=sys.stderr)
            raise SystemExit(1)

        listed = run_mc(["admin", "accesskey", "list", "m", "--all", "--json"])
        if listed.returncode != 0:
            fail("minio accesskey list failed")
        if not listed.stdout.strip():
            fail("minio accesskey list returned empty output")

        keys = []
        for line_number, raw_line in enumerate(listed.stdout.splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except (TypeError, json.JSONDecodeError):
                fail(f"minio accesskey list JSON parse failed at line {line_number}")
            if not isinstance(record, dict) or record.get("status") != "success":
                fail("minio accesskey list returned invalid JSON")
            accounts = record.get("svcaccs")
            if accounts is None:
                continue
            if not isinstance(accounts, list):
                fail("minio accesskey list svcaccs is not a list")
            for account in accounts:
                if not isinstance(account, dict):
                    fail("minio accesskey list contains invalid account")
                access_key = account.get("accessKey")
                if not isinstance(access_key, str) or not access_key:
                    fail("minio accesskey list account has no accessKey")
                if access_key not in keys:
                    keys.append(access_key)

        for access_key in keys:
            info = run_mc(
                ["admin", "accesskey", "info", "m", access_key, "--json"]
            )
            if info.returncode != 0:
                fail("minio accesskey info failed")
            info_lines = [line.strip() for line in info.stdout.splitlines() if line.strip()]
            if not info_lines:
                fail("minio accesskey info returned empty output")
            for raw_info in info_lines:
                try:
                    record = json.loads(raw_info)
                except (TypeError, json.JSONDecodeError):
                    fail("minio accesskey info JSON parse failed")
                if not isinstance(record, dict) or record.get("status") != "success":
                    fail("minio accesskey info returned invalid JSON")
                print("MINIO_INFO\\t" + raw_info)

        print("MINIO_OK")
        """
    )
    return (
        "set -eu\n"
        f"export MINIO_MC_IMAGE={shlex.quote(image_ref)}\n"
        f"export MINIO_MC_HOST={shlex.quote(image_host)}\n"
        "python3 - <<'PY'\n"
        + remote_python
        + "PY\n"
    )


def _managed_minio_address(backup_cfg: dict, infra_address: str | None) -> bool:
    """Czy endpoint S3 wskazuje host infra TEGO inwentarza (zarzadzane MinIO)?

    TEN SAM predykat co `galera_backup_managed_minio` w
    roles/galera_backup/tasks/main.yml i playbooks/cluster_deregister.yml
    (derejestracyjny wariant — bez `backup.enabled` i bez
    `galera_backup_provision_s3`, bo derejestracja sprzata konto tez po
    wylaczeniu kopii). Port odcinamy, bo operator moze wpisac `host:9000`.
    """
    endpoint = (backup_cfg.get("s3") or {}).get("endpoint")
    if not isinstance(endpoint, str) or not endpoint or not infra_address:
        return False
    return re.sub(r":[0-9]+$", "", endpoint) == str(infra_address)


def main() -> int:
    failures: list[str] = []
    undetermined: list[str] = []
    ctx = ProbeContext()

    cluster_name = ctx.config.get("cluster", {}).get("name", ctx.cluster_name)
    pmm_cfg = ctx.config.get("monitoring", {}).get("pmm", {})
    pmm_cluster_name = pmm_cfg.get("cluster_name", cluster_name)
    pmm_server_url = str(pmm_cfg.get("server_url") or "").rstrip("/")
    if not pmm_server_url:
        # Adres PMM jest DANYM ZEWNETRZNYM tej sondy. Do 2026-09-21 stal tu
        # labowy fallback (https://192.168.1.130): definicja bez server_url
        # byla mierzona na CUDZYM PMM, wiec raport o sierotach dotyczyl
        # obcych obiektow. cluster.schema.json wymaga tego pola — brak to
        # wada definicji, wiec odmawiamy, a nie podstawiamy adresu.
        print(
            "FAIL: cluster.yml nie deklaruje monitoring.pmm.server_url — "
            "sonda nie ma gdzie zmierzyc sierot PMM, a adresu zadnej "
            "infrastruktury nie podstawia zamiast deklaracji",
            file=sys.stderr,
        )
        return 1
    proxysql_cfg = ctx.config.get("proxysql", {})
    hostgroup_base = int(proxysql_cfg.get("hostgroup_base", 10))
    app_user = proxysql_cfg.get("app_user", "app_user")

    # ==========================================================================
    # 1. PMM Inventory (Nodes & Services)
    # ==========================================================================
    pmm_password = ctx.env_secret("PMM_ADMIN_PASSWORD")
    pmm_unavailable: set[str] = set()
    if not pmm_password:
        undetermined.append("PMM: brak PMM_ADMIN_PASSWORD w srodowisku lub tests/lab/.env")
    else:
        auth_header = "Basic " + base64.b64encode(
            f"admin:{pmm_password}".encode()
        ).decode()

        nodes_path = "/v1/inventory/nodes"
        nodes_data = api_get(
            nodes_path,
            pmm_server_url,
            auth_header,
            undetermined,
            pmm_unavailable,
            pmm_cfg,
        )
        if isinstance(nodes_data, dict):
            all_nodes = []
            for group_name in ("generic", "container", "remote"):
                node_group = nodes_data.get(group_name, [])
                if not isinstance(node_group, list):
                    if nodes_path not in pmm_unavailable:
                        undetermined.append(
                            f"PMM Inventory nodes: sekcja {group_name} nie jest lista"
                        )
                    continue
                all_nodes.extend(node_group)
            for node in all_nodes:
                if not isinstance(node, dict):
                    undetermined.append("PMM Inventory nodes: wpis nie jest obiektem")
                    continue
                node_name = node.get("node_name", "")
                node_id = node.get("node_id", "")
                if not isinstance(node_name, str):
                    undetermined.append("PMM Inventory nodes: node_name nie jest tekstem")
                    continue
                if node_name.startswith(f"{pmm_cluster_name}-") or node_name == pmm_cluster_name:
                    failures.append(f"PMM Node: id={node_id} name={node_name}")
        elif nodes_path not in pmm_unavailable:
            undetermined.append("PMM Inventory nodes: odpowiedz nie jest obiektem")

        services_path = "/v1/inventory/services"
        services_data = api_get(
            services_path,
            pmm_server_url,
            auth_header,
            undetermined,
            pmm_unavailable,
            pmm_cfg,
        )
        if isinstance(services_data, dict):
            for service_type, service_list in services_data.items():
                if not isinstance(service_list, list):
                    if services_path not in pmm_unavailable:
                        undetermined.append(
                            f"PMM Inventory services: sekcja {service_type} nie jest lista"
                        )
                    continue
                for service in service_list:
                    if not isinstance(service, dict):
                        undetermined.append(
                            f"PMM Inventory services ({service_type}): wpis nie jest obiektem"
                        )
                        continue
                    service_name = service.get("service_name", "")
                    service_id = service.get("service_id", "")
                    if not isinstance(service_name, str):
                        undetermined.append(
                            f"PMM Inventory services ({service_type}): service_name nie jest tekstem"
                        )
                        continue
                    if (
                        service_name.startswith(f"{pmm_cluster_name}-")
                        or service_name.startswith(f"{cluster_name}-")
                    ):
                        failures.append(
                            f"PMM Service ({service_type}): id={service_id} name={service_name}"
                        )
        elif services_path not in pmm_unavailable:
            undetermined.append("PMM Inventory services: odpowiedz nie jest obiektem")

        # ==========================================================================
        # 2. Grafana Alerting (Alert Rules, Contact Points, Folders, Policies)
        # ==========================================================================
        alert_path = "/graph/api/v1/provisioning/alert-rules"
        alert_rules = api_get(
            alert_path,
            pmm_server_url,
            auth_header,
            undetermined,
            pmm_unavailable,
            pmm_cfg,
        )
        owned_alert_prefixes = {
            prefix
            for label in (pmm_cluster_name, cluster_name)
            for prefix in alert_uid_prefixes(label)
        }
        if isinstance(alert_rules, list):
            for rule in alert_rules:
                if not isinstance(rule, dict):
                    undetermined.append("Grafana Alert Rules: wpis nie jest obiektem")
                    continue
                uid = rule.get("uid", "")
                title = rule.get("title", "")
                if not isinstance(uid, str) or not isinstance(title, str):
                    undetermined.append("Grafana Alert Rules: uid/title nie sa tekstami")
                    continue
                if any(
                    uid.startswith(f"{prefix}-")
                    for prefix in owned_alert_prefixes
                ):
                    failures.append(f"Grafana Alert Rule: uid={uid} title={title}")
        elif alert_path not in pmm_unavailable:
            undetermined.append("Grafana Alert Rules: odpowiedz nie jest lista")

        contact_path = "/graph/api/v1/provisioning/contact-points"
        contact_points = api_get(
            contact_path,
            pmm_server_url,
            auth_header,
            undetermined,
            pmm_unavailable,
            pmm_cfg,
        )
        if isinstance(contact_points, list):
            for contact_point in contact_points:
                if not isinstance(contact_point, dict):
                    undetermined.append("Grafana Contact Points: wpis nie jest obiektem")
                    continue
                contact_name = contact_point.get("name", "")
                contact_uid = contact_point.get("uid", "")
                if not isinstance(contact_name, str) or not isinstance(contact_uid, str):
                    undetermined.append("Grafana Contact Points: name/uid nie sa tekstami")
                    continue
                if (
                    f"({pmm_cluster_name})" in contact_name
                    or contact_uid == f"email-isa-{pmm_cluster_name}"
                ):
                    failures.append(
                        f"Grafana Contact Point: uid={contact_uid} name={contact_name}"
                    )
        elif contact_path not in pmm_unavailable:
            undetermined.append("Grafana Contact Points: odpowiedz nie jest lista")

        folders_path = "/graph/api/folders"
        folders = api_get(
            folders_path,
            pmm_server_url,
            auth_header,
            undetermined,
            pmm_unavailable,
            pmm_cfg,
        )
        if isinstance(folders, list):
            for folder in folders:
                if not isinstance(folder, dict):
                    undetermined.append("Grafana Folders: wpis nie jest obiektem")
                    continue
                folder_uid = folder.get("uid", "")
                folder_title = folder.get("title", "")
                if not isinstance(folder_uid, str) or not isinstance(folder_title, str):
                    undetermined.append("Grafana Folders: uid/title nie sa tekstami")
                    continue
                if (
                    folder_uid == f"isa-alerts-{pmm_cluster_name}"
                    or folder_title == f"ISA Alerts ({pmm_cluster_name})"
                ):
                    failures.append(
                        f"Grafana Folder: uid={folder_uid} title={folder_title}"
                    )
        elif folders_path not in pmm_unavailable:
            undetermined.append("Grafana Folders: odpowiedz nie jest lista")

        policies_path = "/graph/api/v1/provisioning/policies"
        policies = api_get(
            policies_path,
            pmm_server_url,
            auth_header,
            undetermined,
            pmm_unavailable,
            pmm_cfg,
        )
        if isinstance(policies, dict):
            routes = policies.get("routes", [])
            if not isinstance(routes, list):
                if policies_path not in pmm_unavailable:
                    undetermined.append("Grafana Policies: routes nie jest lista")
            else:
                for route in routes:
                    if not isinstance(route, dict):
                        undetermined.append("Grafana Policies: route nie jest obiektem")
                        continue
                    receiver = route.get("receiver", "")
                    if not isinstance(receiver, str):
                        undetermined.append("Grafana Policies: receiver nie jest tekstem")
                        continue
                    if (
                        f"({pmm_cluster_name})" in receiver
                        or f"isa-{pmm_cluster_name}" in receiver
                    ):
                        failures.append(f"Grafana Policy Route: receiver={receiver}")
        elif policies_path not in pmm_unavailable:
            undetermined.append("Grafana Policies: odpowiedz nie jest obiektem")

    # ==========================================================================
    # 3. MinIO (service accounts)
    # ==========================================================================
    backup_cfg = ctx.config.get("backup", {})
    backup_enabled = bool(backup_cfg.get("enabled", True))
    minio_measured = False
    infra_hosts = ctx.group_hosts("infra")
    infra_children = (ctx.inventory.get("all", {}).get("children", {}) or {}).get("infra", {})
    infra_entry = (infra_children.get("hosts", {}) or {}).get(infra_hosts[0], {}) if infra_hosts else {}
    # Predykat porownuje DOKLADNIE `hostvars[groups['infra'][0]].ansible_host`.
    # Zmierzono na ansible 2.15: na hoscie bez `ansible_host` playbook NIE
    # renderuje pustego napisu — pada z AnsibleUndefinedVariable, czyli
    # preflight derejestracji wywala sie glosno, zanim cokolwiek zostanie
    # odwolane. Sonda w tym stanie raportuje NIEzarzadzane (SKIP): nie wolno
    # jej zadac poswiadczen MinIO na klastrowie, ktorego fabryka nigdy nie
    # provisionowala. Fallback na `infra_node_address` utworzylby konwencje,
    # ktorej playbook nie zna.
    infra_address = str(infra_entry.get("ansible_host") or "") if infra_entry else ""
    if (
        backup_cfg.get("destination") == "s3"
        and not _managed_minio_address(backup_cfg, infra_address)
    ):
        # Predykat "zarzadzane MinIO" jest TEN SAM co w
        # roles/galera_backup/tasks/main.yml i playbooks/cluster_deregister.yml:
        # endpoint S3 musi wskazywac host grupy `infra` TEGO inwentarza.
        # S3 zewnetrzny (AWS, R2, Ceph, operator) nie ma tu konta serwisowego
        # zakladanego przez fabryke, wiec wymaganie poswiadczen root MinIO albo
        # `mc` na grupie infra opisywaloby jako nierozstrzygniete cos, czego
        # nikt nie zadeklarowal — a derejestracja i tak tam niczego nie odwoluje.
        endpoint = (backup_cfg.get("s3") or {}).get("endpoint")
        print(
            "SKIP: magazyn S3 jest zewnetrzny wobec tego inwentarza "
            f"(endpoint {endpoint!r}, host infra {infra_address!r}) — "
            "konta serwisowe dostawcy nie naleza do tego klastra"
        )
    elif backup_cfg.get("destination") == "s3" and not backup_enabled:
        # Wylaczona kopia nigdy nie zalozyla konta serwisowego, wiec nie ma czego
        # osierocic. Do 2026-08-25 bramkowal tu SAM backend: `nova-r9` mial
        # `enabled: false` obok `destination: s3`, wiec sonda szla do MinIO po
        # konta, ktorych nikt nie tworzyl, i konczyla UNDETERMINED. Werdykt
        # "nie zmierzono" na klastrze, ktory wlasnie kasujesz, jest gorszy niz
        # brak sekcji: wyglada jak ryzyko, a jest artefaktem bramkowania.
        print("SKIP: backup wylaczony w cluster.yml — brak konta MinIO do osierocenia")
    elif backup_cfg.get("destination") == "s3":
        # Sam bucket nie jest sierota; jego retencja to decyzja operatora.
        minio_user = ctx.env_secret("MINIO_ROOT_USER")
        minio_password = ctx.env_secret("MINIO_ROOT_PASSWORD")
        if not minio_user or not minio_password:
            undetermined.append(
                "MinIO: brak MINIO_ROOT_USER/MINIO_ROOT_PASSWORD w srodowisku "
                "lub tests/lab/.env"
            )
        else:
            image_ref = _load_mc_image(ctx, undetermined)
            if not infra_hosts:
                undetermined.append("MinIO: brak grupy infra w inwentarzu")
            elif image_ref:
                minio_result = run_ansible(
                    ctx,
                    "infra",
                    _minio_probe_script(image_ref, minio_user, minio_password),
                )
                require_hosts(
                    minio_result,
                    infra_hosts,
                    "MinIO konta serwisowe",
                    failures,
                    undetermined,
                )
                if minio_result.returncode != 0:
                    undetermined.append(
                        f"MinIO konta serwisowe: ansible rc={minio_result.returncode}"
                    )
                minio_measured = True

                target_name = f"galera-backup-{cluster_name}"
                seen_keys: set[str] = set()
                for host in infra_hosts:
                    if host not in minio_result.bodies:
                        continue
                    saw_ok = False
                    for line_number, line in enumerate(
                        minio_result.body(host).splitlines(), start=1
                    ):
                        if line == "MINIO_OK":
                            if saw_ok:
                                undetermined.append(
                                    f"MinIO konta serwisowe: {host} powtorzony znacznik OK"
                                )
                            saw_ok = True
                            continue
                        if not line.startswith("MINIO_INFO\t"):
                            undetermined.append(
                                f"MinIO konta serwisowe: {host} nieoczekiwany format "
                                f"(wiersz {line_number})"
                            )
                            continue
                        raw_info = line.split("\t", 1)[1]
                        try:
                            info = json.loads(raw_info)
                        except json.JSONDecodeError as exc:
                            undetermined.append(
                                f"MinIO konta serwisowe: {host} nieprawidlowy JSON "
                                f"({type(exc).__name__})"
                            )
                            continue
                        if not isinstance(info, dict) or info.get("status") != "success":
                            undetermined.append(
                                f"MinIO konta serwisowe: {host} nieprawidlowy wynik info"
                            )
                            continue
                        if info.get("name") != target_name:
                            continue
                        access_key = info.get("accessKey")
                        if not isinstance(access_key, str) or not access_key:
                            undetermined.append(
                                f"MinIO konta serwisowe: {host} konto {target_name} "
                                "nie ma accessKey"
                            )
                            continue
                        if access_key not in seen_keys:
                            seen_keys.add(access_key)
                            failures.append(
                                f"MinIO Service Account: accessKey={access_key} "
                                f"name={target_name}"
                            )
                    if not saw_ok:
                        undetermined.append(
                            f"MinIO konta serwisowe: {host} brak znacznika zakonczenia"
                        )

    # ==========================================================================
    # 4. ProxySQL (Hostgroups, Users, SSL Params)
    # ==========================================================================
    proxysql_hosts = ctx.group_hosts("proxysql")
    if proxysql_hosts:
        first_psql_host = proxysql_hosts[0]
        sql_query = f"""
        SELECT 'mysql_servers', hostgroup_id, hostname FROM mysql_servers WHERE hostgroup_id IN ({hostgroup_base}, {hostgroup_base+10}, {hostgroup_base+20}, {hostgroup_base+30});
        SELECT 'mysql_galera_hostgroups', writer_hostgroup, backup_writer_hostgroup FROM mysql_galera_hostgroups WHERE writer_hostgroup={hostgroup_base};
        SELECT 'mysql_users', username, default_hostgroup FROM mysql_users WHERE username='{app_user}';
        SELECT 'mysql_servers_ssl_params', hostname, ssl_ca FROM mysql_servers_ssl_params WHERE ssl_ca LIKE '%{cluster_name}%' OR ssl_ca LIKE '%{pmm_cluster_name}%';
        """
        proxy_script = (
            "mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf "
            "-h127.0.0.1 -P6032 -uadmin -N -B -e "
            + shlex.quote(sql_query)
        )
        proxy_result = run_ansible(ctx, first_psql_host, proxy_script, timeout=15)
        require_hosts(
            proxy_result,
            [first_psql_host],
            "ProxySQL sieroty",
            failures,
            undetermined,
        )
        if proxy_result.returncode != 0:
            undetermined.append(
                f"ProxySQL sieroty: ansible rc={proxy_result.returncode}"
            )
        if first_psql_host in proxy_result.bodies:
            for line in proxy_result.body(first_psql_host).splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0] in (
                    "mysql_servers",
                    "mysql_galera_hostgroups",
                    "mysql_users",
                    "mysql_servers_ssl_params",
                ):
                    failures.append(
                        f"ProxySQL Table ({parts[0]}): {' '.join(parts[1:])}"
                    )
    else:
        undetermined.append("ProxySQL: brak grupy proxysql w inwentarzu")

    # Podsumowanie mowi tylko o TYM, co zostalo zmierzone. Do 2026-09-21
    # klaster z zewnetrznym S3 dostawal "w MinIO" w wierszu PASS, chociaz
    # sonda tam niczego nie zmierzyla.
    components = "PMM, Grafanie i ProxySQL"
    if minio_measured:
        components = "PMM, Grafanie, MinIO i ProxySQL"
    return finish(
        failures,
        undetermined,
        f"zero sierot dla klastra '{cluster_name}' (PMM: '{pmm_cluster_name}') "
        f"w {components}",
    )


if __name__ == "__main__":
    sys.exit(main())
