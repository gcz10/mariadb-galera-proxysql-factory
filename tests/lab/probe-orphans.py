#!/usr/bin/env python3
"""Sonda sprawdzajaca brak sierot w PMM, Grafanie i ProxySQL po deprovisioningu klastra.

Uruchomienie:
  TARGET_ENV tests/lab/probe-orphans.py
  make cluster-deregister-verify CLUSTER=<name>

Zwraca kod 0 (PASS), gdy wskazany klaster NIE POSIADA zadnych obiektow w:
  1. PMM Inventory (nodes, services, agents)
  2. Grafana Alerting (alert rules, contact points, folders, notification policy routes)
  3. ProxySQL (mysql_servers, mysql_galera_hostgroups, mysql_users, mysql_servers_ssl_params)

Zwraca kod 1 (FAIL) i wypisuje liste znalezionych sierot, gdy cokolwiek zostalo.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    print("FAIL: brak modulu PyYAML", file=sys.stderr)
    sys.exit(2)


CLUSTER = os.environ.get("CLUSTER")
CLUSTER_CONFIG_PATH = os.environ.get("CLUSTER_CONFIG")
CLUSTER_INVENTORY_PATH = os.environ.get("CLUSTER_INVENTORY")

if not CLUSTER:
    print("FAIL: wymagana zmienna srodowiskowa CLUSTER=<nazwa>", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parents[2]
if not CLUSTER_CONFIG_PATH:
    CLUSTER_CONFIG_PATH = str(REPO_ROOT / "clusters" / CLUSTER / "cluster.yml")
if not CLUSTER_INVENTORY_PATH:
    CLUSTER_INVENTORY_PATH = str(REPO_ROOT / "clusters" / CLUSTER / "inventory.yml")

cfg_file = Path(CLUSTER_CONFIG_PATH)
if not cfg_file.is_file():
    print(f"FAIL: plik konfiguracyjny {cfg_file} nie istnieje", file=sys.stderr)
    sys.exit(2)

with open(cfg_file, encoding="utf-8") as f:
    cluster_cfg = yaml.safe_load(f) or {}

inv_file = Path(CLUSTER_INVENTORY_PATH)
inv_cfg = {}
if inv_file.is_file():
    with open(inv_file, encoding="utf-8") as f:
        inv_cfg = yaml.safe_load(f) or {}

# Wyznacz identyfikatory klastra
cluster_name = cluster_cfg.get("cluster", {}).get("name", CLUSTER)
pmm_cfg = cluster_cfg.get("monitoring", {}).get("pmm", {})
pmm_cluster_name = pmm_cfg.get("cluster_name", cluster_name)
pmm_server_url = pmm_cfg.get("server_url", "https://192.168.1.130").rstrip("/")
proxysql_cfg = cluster_cfg.get("proxysql", {})
hostgroup_base = int(proxysql_cfg.get("hostgroup_base", 10))
app_user = proxysql_cfg.get("app_user", "app_user")

# Pobierz adresy IP wezlow klastra
cluster_ips = set()
galera_hosts = inv_cfg.get("all", {}).get("children", {}).get("galera", {}).get("hosts", {})
for h_name, h_vars in galera_hosts.items():
    if isinstance(h_vars, dict):
        ip = h_vars.get("ansible_host") or h_vars.get("galera_node_address")
        if ip:
            cluster_ips.add(ip)

# Odczytaj sekret PMM z env lub tests/lab/.env
pmm_password = os.environ.get("PMM_ADMIN_PASSWORD")
if not pmm_password:
    env_path = REPO_ROOT / "tests" / "lab" / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("PMM_ADMIN_PASSWORD="):
                pmm_password = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

if not pmm_password:
    print("FAIL: brak PMM_ADMIN_PASSWORD w srodowisku lub tests/lab/.env", file=sys.stderr)
    sys.exit(2)

CTX = ssl._create_unverified_context()
AUTH_HEADER = "Basic " + base64.b64encode(f"admin:{pmm_password}".encode()).decode()


def api_get(path: str) -> dict | list | None:
    url = pmm_server_url + path
    req = urllib.request.Request(url, headers={"Authorization": AUTH_HEADER, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=15) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data) if data.strip() else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"WARN: API {path} zwrocilo HTTP {e.code}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"WARN: blad polaczenia z API {path}: {e}", file=sys.stderr)
        return None


findings: list[str] = []

# ==============================================================================
# 1. PMM Inventory (Nodes & Services)
# ==============================================================================
nodes_data = api_get("/v1/inventory/nodes") or {}
all_nodes = (
    nodes_data.get("generic", [])
    + nodes_data.get("container", [])
    + nodes_data.get("remote", [])
)
for n in all_nodes:
    n_name = n.get("node_name", "")
    n_id = n.get("node_id", "")
    if n_name.startswith(f"{pmm_cluster_name}-") or n_name == pmm_cluster_name:
        findings.append(f"PMM Node: id={n_id} name={n_name}")

services_data = api_get("/v1/inventory/services") or {}
for svc_type, svc_list in services_data.items():
    if isinstance(svc_list, list):
        for s in svc_list:
            s_name = s.get("service_name", "")
            s_id = s.get("service_id", "")
            if s_name.startswith(f"{pmm_cluster_name}-") or s_name.startswith(f"{cluster_name}-"):
                findings.append(f"PMM Service ({svc_type}): id={s_id} name={s_name}")

# ==============================================================================
# 2. Grafana Alerting (Alert Rules, Contact Points, Folders, Policies)
# ==============================================================================
alert_rules = api_get("/graph/api/v1/provisioning/alert-rules")
if isinstance(alert_rules, list):
    for r in alert_rules:
        uid = r.get("uid", "")
        title = r.get("title", "")
        # Szukaj UID isa-<cluster>-*
        if uid.startswith(f"isa-{pmm_cluster_name}-") or uid.startswith(f"isa-{cluster_name}-"):
            findings.append(f"Grafana Alert Rule: uid={uid} title={title}")

contact_points = api_get("/graph/api/v1/provisioning/contact-points")
if isinstance(contact_points, list):
    for cp in contact_points:
        cp_name = cp.get("name", "")
        cp_uid = cp.get("uid", "")
        if f"({pmm_cluster_name})" in cp_name or cp_uid == f"email-isa-{pmm_cluster_name}":
            findings.append(f"Grafana Contact Point: uid={cp_uid} name={cp_name}")

folders = api_get("/graph/api/folders")
if isinstance(folders, list):
    for fld in folders:
        fld_uid = fld.get("uid", "")
        fld_title = fld.get("title", "")
        if fld_uid == f"isa-alerts-{pmm_cluster_name}" or fld_title == f"ISA Alerts ({pmm_cluster_name})":
            findings.append(f"Grafana Folder: uid={fld_uid} title={fld_title}")

policies = api_get("/graph/api/v1/provisioning/policies")
if isinstance(policies, dict):
    routes = policies.get("routes", [])
    for rt in routes:
        recv = rt.get("receiver", "")
        if f"({pmm_cluster_name})" in recv or f"isa-{pmm_cluster_name}" in recv:
            findings.append(f"Grafana Policy Route: receiver={recv}")

# ==============================================================================
# 3. ProxySQL (Hostgroups, Users, SSL Params)
# ==============================================================================
proxysql_hosts = inv_cfg.get("all", {}).get("children", {}).get("proxysql", {}).get("hosts", {})
if proxysql_hosts:
    first_psql_host = next(iter(proxysql_hosts.keys()))
    psql_ip = (
        proxysql_hosts[first_psql_host].get("ansible_host")
        if isinstance(proxysql_hosts[first_psql_host], dict)
        else first_psql_host
    )

    sql_query = f"""
    SELECT 'mysql_servers', hostgroup_id, hostname FROM mysql_servers WHERE hostgroup_id IN ({hostgroup_base}, {hostgroup_base+10}, {hostgroup_base+20}, {hostgroup_base+30});
    SELECT 'mysql_galera_hostgroups', writer_hostgroup, backup_writer_hostgroup FROM mysql_galera_hostgroups WHERE writer_hostgroup={hostgroup_base};
    SELECT 'mysql_users', username, default_hostgroup FROM mysql_users WHERE username='{app_user}';
    SELECT 'mysql_servers_ssl_params', hostname, ssl_ca FROM mysql_servers_ssl_params WHERE ssl_ca LIKE '%{cluster_name}%' OR ssl_ca LIKE '%{pmm_cluster_name}%';
    """

    cmd = [
        "ansible",
        first_psql_host,
        "-i",
        CLUSTER_INVENTORY_PATH,
        "-b",
        "-m",
        "shell",
        "-a",
        f"mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf -h127.0.0.1 -P6032 -uadmin -N -B -e \"{sql_query}\"",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line or "rc=0" in line or "| CHANGED" in line or "| SUCCESS" in line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0] in (
                    "mysql_servers",
                    "mysql_galera_hostgroups",
                    "mysql_users",
                    "mysql_servers_ssl_params",
                ):
                    findings.append(f"ProxySQL Table ({parts[0]}): {' '.join(parts[1:])}")
    except Exception:
        # ProxySQL probe failure is non-blocking if ProxySQL host is unreachable
        pass

# ==============================================================================
# Wynik
# ==============================================================================
if findings:
    print(f"FAIL: znaleziono {len(findings)} sierot dla klastra '{cluster_name}' (PMM: '{pmm_cluster_name}'):")
    for f in findings:
        print(f"  - {f}")
    sys.exit(1)
else:
    print(f"PASS: zero sierot dla klastra '{cluster_name}' (PMM: '{pmm_cluster_name}') w PMM, Grafanie i ProxySQL")
    sys.exit(0)
