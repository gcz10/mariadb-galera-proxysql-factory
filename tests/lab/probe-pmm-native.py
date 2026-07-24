#!/usr/bin/env python3
"""Verify that the lab cluster is registered in PMM's native inventory."""

import base64
import json
import os
import ssl
import sys
import time
import yaml
from urllib.parse import quote
from urllib.request import Request, urlopen

PMM_URL = os.environ.get("PMM_SERVER_URL", "https://127.0.0.1:8443").rstrip("/")
PMM_USER = os.environ.get("PMM_ADMIN_USER", "admin")
PMM_PASSWORD = os.environ.get("PMM_ADMIN_PASSWORD")
CONFIG_PATH = os.environ.get(
    "CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml"
)
with open(CONFIG_PATH, encoding="utf-8") as config_file:
    CLUSTER_CONFIG = yaml.safe_load(config_file)
PMM_CONFIG = CLUSTER_CONFIG["monitoring"]["pmm"]
with open(CLUSTER_CONFIG["versions"]["lock_file"], encoding="utf-8") as lock_file:
    VERSION_LOCK = yaml.safe_load(lock_file)


CLUSTER = PMM_CONFIG["cluster_name"]
EXPECTED_NODES = {
    f"{CLUSTER}-gnode1": "172.28.0.11",
    f"{CLUSTER}-gnode2": "172.28.0.12",
    f"{CLUSTER}-gnode3": "172.28.0.13",
    f"{CLUSTER}-pnode1": "172.28.0.21",
    f"{CLUSTER}-pnode2": "172.28.0.22",
}
EXPECTED_MYSQL = {
    f"{CLUSTER}-gnode{index}-mysql": f"{CLUSTER}-gnode{index}"
    for index in range(1, 4)
}
EXPECTED_NODE_EXPORTERS = {f"{name}-node-exporter" for name in EXPECTED_NODES}
EXPECTED_PROXYSQL = {f"{name}-proxysql" for name in EXPECTED_NODES if name.endswith("pnode1") or name.endswith("pnode2")}
EXPECTED_CREDENTIALS_REVISION = str(PMM_CONFIG["credentials_revision"])
EXPECTED_NODE_EXPORTER_VERSION = str(VERSION_LOCK["node_exporter"]["version"])
EXPECTED_PMM_VERSION = str(VERSION_LOCK["pmm"]["version"])
# Lifecycle config gauges — exact value match expected (baseline from F11).
EXPECTED_CONFIG_METRICS = {
    "isa_backup_monitoring_enabled": 1,
    "isa_restore_test_monitoring_enabled": 1,
    "isa_tls_monitoring_enabled": 0,
}
# ISC-49 freshness unixtimes: non-zero + within an age window after F10 runs.
BACKUP_RETENTION_DAYS = int(CLUSTER_CONFIG.get("backup", {}).get("retention_days", 14))
EXPECTED_FRESHNESS_METRICS = {
    # metric name → max acceptable age in days
    "isa_backup_last_success_unixtime": BACKUP_RETENTION_DAYS,
    "isa_restore_test_last_success_unixtime": 8,  # weekly restore_test_schedule + 1d grace
}
# TLS cert expiry: 0 when tls.mode != full; future epoch when full.
TLS_MODE = CLUSTER_CONFIG.get("tls", {}).get("mode", "disabled")
TLS_EXPIRY_DISABLED_EXPECTED = TLS_MODE != "full"
ALL_STATE_METRICS = (
    set(EXPECTED_CONFIG_METRICS)
    | set(EXPECTED_FRESHNESS_METRICS)
    | {"isa_tls_cert_expiry_unixtime"}
)


def get_json(path):
    token = base64.b64encode(f"{PMM_USER}:{PMM_PASSWORD}".encode()).decode()
    request = Request(f"{PMM_URL}{path}", headers={"Authorization": f"Basic {token}"})
    context = ssl.create_default_context()
    if os.environ.get("PMM_VALIDATE_CERTS", "0") != "1":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    with urlopen(request, context=context, timeout=10) as response:
        return json.load(response)


def check(condition, message, failures):
    if not condition:
        failures.append(message)

def wait_for_fresh_metrics(queries, started_at, timeout=90):
    """Poll instant queries until every expected series was scraped after this probe."""
    deadline = time.monotonic() + timeout
    latest = {}
    while True:
        for name, (query, expected_count) in queries.items():
            try:
                response = get_json(f"/prometheus/api/v1/query?query={query}")
                results = response.get("data", {}).get("result", [])
            except (OSError, TypeError, ValueError):
                results = []
            latest[name] = results
            if len(results) != expected_count or not all(
                float(result["value"][0]) >= started_at for result in results
            ):
                break
        else:
            return latest
        if time.monotonic() >= deadline:
            return latest
        time.sleep(2)





def main():
    if not PMM_PASSWORD:
        print("PMM_ADMIN_PASSWORD is required", file=sys.stderr)
        return 2
    probe_started = time.time()

    version = get_json("/v1/version")
    nodes = get_json("/v1/inventory/nodes")
    services = get_json("/v1/inventory/services")
    agents = get_json("/v1/inventory/agents")
    alert_rules = get_json("/graph/api/v1/provisioning/alert-rules")
    failures = []
    check(
        version.get("version") == EXPECTED_PMM_VERSION
        and version.get("distribution_method") == "DISTRIBUTION_METHOD_DOCKER",
        f"PMM runtime differs from Docker lock {EXPECTED_PMM_VERSION}: {version}",
        failures,
    )
    # ISC-47: F15 managed alert rules present (quorum/writer/node loss + freshness).
    # Delivery (contact point) deferred to BLK-5; rules detect the conditions.
    EXPECTED_ALERT_RULES = {
        "isa-galera-node-loss", "isa-galera-quorum-loss",
        "isa-galera-not-synced", "isa-backup-stale",
    }
    managed_alert_rules = [
        rule
        for rule in alert_rules
        if rule.get("labels", {}).get("managed_by") == "ansible"
        and rule.get("labels", {}).get("cluster") == CLUSTER
    ]
    managed_uids = {rule.get("uid") for rule in managed_alert_rules}
    check(
        managed_uids == EXPECTED_ALERT_RULES,
        f"ISC-47 managed alert rules differ: got {sorted(managed_uids)}, "
        f"expected {sorted(EXPECTED_ALERT_RULES)}",
        failures,
    )


    managed_nodes = [
        node
        for node in nodes.get("generic", [])
        if node.get("custom_labels", {}).get("cluster") == CLUSTER
    ]
    managed_node_names = [node["node_name"] for node in managed_nodes]
    check(
        sorted(managed_node_names) == sorted(EXPECTED_NODES),
        f"managed node set differs: {sorted(managed_node_names)}",
        failures,
    )
    generic_nodes = {node["node_name"]: node for node in managed_nodes}
    remote_names = {node["node_name"] for node in nodes.get("remote", [])}
    legacy_names = {f"gnode{index}" for index in range(1, 4)} | {
        f"pnode{index}" for index in range(1, 3)
    }

    for name, address in EXPECTED_NODES.items():
        node = generic_nodes.get(name)
        check(node is not None, f"missing generic node: {name}", failures)
        if node:
            check(node.get("address") == address, f"wrong address for {name}", failures)
        check(name not in remote_names, f"obsolete remote node remains: {name}", failures)
    check(
        remote_names.isdisjoint(legacy_names),
        f"obsolete unnamespaced remote nodes remain: {sorted(remote_names & legacy_names)}",
        failures,
    )

    managed_external_services = [
        service
        for service in services.get("external", [])
        if service.get("cluster") == CLUSTER and service.get("group") == "node-exporter"
    ]
    check(
        sorted(service["service_name"] for service in managed_external_services)
        == sorted(EXPECTED_NODE_EXPORTERS),
        "managed node-exporter service set differs",
        failures,
    )
    external_services = {
        service["service_name"]: service for service in managed_external_services
    }
    external_agents = {}
    for agent in agents.get("external_exporter", []):
        external_agents.setdefault(agent.get("service_id"), []).append(agent)

    for node_name in EXPECTED_NODES:
        service_name = f"{node_name}-node-exporter"
        service = external_services.get(service_name)
        check(
            service is not None,
            f"missing native node-exporter service: {service_name}",
            failures,
        )
        if not service:
            continue
        node = generic_nodes.get(node_name)
        if node:
            check(
                service.get("node_id") == node.get("node_id"),
                f"{service_name} attached to wrong node",
                failures,
            )
        matches = external_agents.get(service.get("service_id"), [])
        check(
            len(matches) == 1,
            f"expected one external-exporter agent for {service_name}, got {len(matches)}",
            failures,
        )
        if len(matches) == 1:
            check(
                matches[0].get("status") == "AGENT_STATUS_RUNNING",
                f"external-exporter not running: {service_name}",
                failures,
            )
            agent = matches[0]
            if node:
                check(
                    agent.get("runs_on_node_id") == node.get("node_id"),
                    f"external-exporter runs on wrong node: {service_name}",
                    failures,
                )
            check(
                agent.get("scheme") == "http"
                and agent.get("metrics_path") == "/metrics"
                and agent.get("listen_port") == 9100,
                f"external-exporter target differs: {service_name}",
                failures,
            )

    # ISC-46: ProxySQL metrics — external services (group=proxysql) + agents (port 6070).
    managed_proxysql_services = [
        service
        for service in services.get("external", [])
        if service.get("cluster") == CLUSTER and service.get("group") == "proxysql"
    ]
    check(
        sorted(service["service_name"] for service in managed_proxysql_services)
        == sorted(EXPECTED_PROXYSQL),
        "managed proxysql service set differs",
        failures,
    )
    proxysql_services = {
        service["service_name"]: service for service in managed_proxysql_services
    }
    for node_name in EXPECTED_PROXYSQL:
        service = proxysql_services.get(node_name)
        check(
            service is not None,
            f"missing proxysql metrics service: {node_name}",
            failures,
        )
        if not service:
            continue
        node = generic_nodes.get(node_name[: -len("-proxysql")])
        if node:
            check(
                service.get("node_id") == node.get("node_id"),
                f"{node_name} attached to wrong node",
                failures,
            )
        agent_matches = [
            a for a in external_agents.get(service.get("service_id"), [])
            if a.get("listen_port") == 6070
        ]
        check(
            len(agent_matches) == 1,
            f"expected one external-exporter agent (port 6070) for {node_name}, "
            f"got {len(agent_matches)}",
            failures,
        )
        if len(agent_matches) == 1:
            check(
                agent_matches[0].get("status") == "AGENT_STATUS_RUNNING",
                f"proxysql external-exporter not running: {node_name}",
                failures,
            )
            check(
                agent_matches[0].get("scheme") == "http"
                and agent_matches[0].get("metrics_path") == "/metrics",
                f"proxysql external-exporter target differs: {node_name}",
                failures,
            )

    managed_mysql_services = [
        service
        for service in services.get("mysql", [])
        if service.get("cluster") == CLUSTER
    ]
    check(
        sorted(service["service_name"] for service in managed_mysql_services)
        == sorted(EXPECTED_MYSQL),
        "managed MySQL service set differs",
        failures,
    )
    mysql_services = {
        service["service_name"]: service for service in managed_mysql_services
    }
    mysql_agents = {}
    for agent in agents.get("mysqld_exporter", []):
        mysql_agents.setdefault(agent.get("service_id"), []).append(agent)
    qan_agents = {}
    for agent in agents.get("qan_mysql_perfschema_agent", []):
        qan_agents.setdefault(agent.get("service_id"), []).append(agent)

    for service_name, node_name in EXPECTED_MYSQL.items():
        service = mysql_services.get(service_name)
        check(service is not None, f"missing MySQL service: {service_name}", failures)
        if not service:
            continue
        node = generic_nodes.get(node_name)
        if node:
            check(
                service.get("node_id") == node.get("node_id"),
                f"{service_name} attached to wrong node",
                failures,
            )
        check(
            service.get("address") == EXPECTED_NODES[node_name]
            and service.get("port") == 3306,
            f"{service_name} targets wrong address or port",
            failures,
        )

        exporters = mysql_agents.get(service.get("service_id"), [])
        check(
            len(exporters) == 1,
            f"expected one mysqld_exporter for {service_name}, got {len(exporters)}",
            failures,
        )
        if len(exporters) == 1:
            exporter = exporters[0]
            check(
                exporter.get("status") == "AGENT_STATUS_RUNNING",
                f"mysqld_exporter not running: {service_name}",
                failures,
            )
            check(
                exporter.get("username") == "pmm_monitor",
                f"wrong mysqld_exporter user: {service_name}",
                failures,
            )
            check(
                exporter.get("custom_labels", {}).get("credentials_revision")
                == EXPECTED_CREDENTIALS_REVISION,
                f"wrong mysqld_exporter credential revision: {service_name}",
                failures,
            )

        qan_matches = qan_agents.get(service.get("service_id"), [])
        check(
            len(qan_matches) == 1,
            f"expected one QAN perfschema agent for {service_name}, got {len(qan_matches)}",
            failures,
        )
        if len(qan_matches) == 1:
            qan = qan_matches[0]
            allowed_qan_statuses = {
                "AGENT_STATUS_RUNNING",
                "AGENT_STATUS_WAITING",
            }
            check(
                qan.get("status") in allowed_qan_statuses,
                f"QAN agent has invalid status for {service_name}: {qan.get('status')}",
                failures,
            )
            check(
                qan.get("username") == "pmm_monitor",
                f"wrong QAN user: {service_name}",
                failures,
            )
            check(
                qan.get("custom_labels", {}).get("credentials_revision")
                == EXPECTED_CREDENTIALS_REVISION,
                f"wrong QAN credential revision: {service_name}",
                failures,
            )

    node_query = quote(
        f'node_load1{{node_name=~"{CLUSTER}-(gnode[1-3]|pnode[1-2])"}}'
    )
    node_build_query = quote(
        f'node_exporter_build_info{{node_name=~"{CLUSTER}-(gnode[1-3]|pnode[1-2])"}}'
    )
    mysql_query = quote(
        f'mysql_up{{service_name=~"{CLUSTER}-gnode[1-3]-mysql",job=~".*_hr"}}'
    )
    galera_query = quote(
        f'mysql_global_status_wsrep_cluster_size{{service_name="{CLUSTER}-gnode1-mysql"}}'
    )
    state_queries = {
        name: quote(f'{name}{{cluster="{CLUSTER}"}}')
        for name in ALL_STATE_METRICS
    }
    proxysql_query = quote(
        f'proxysql_servers_table_version_total{{cluster="{CLUSTER}",external_group="proxysql"}}'
    )
    metric_queries = {
        "nodes": (node_query, len(EXPECTED_NODES)),
        "node_build": (node_build_query, len(EXPECTED_NODES)),
        "mysql": (mysql_query, len(EXPECTED_MYSQL)),
        "galera": (galera_query, 1),
        "proxysql": (proxysql_query, len(EXPECTED_PROXYSQL)),
    }
    metric_queries.update(
        {f"state:{name}": (query, 1) for name, query in state_queries.items()}
    )
    metric_results = wait_for_fresh_metrics(metric_queries, probe_started)

    node_results = metric_results.get("nodes", [])
    measured_nodes = [result["metric"].get("node_name") for result in node_results]
    check(
        sorted(measured_nodes) == sorted(EXPECTED_NODES),
        f"native node metric set differs: {sorted(measured_nodes)}",
        failures,
    )
    check(
        len(node_results) == len(EXPECTED_NODES)
        and all(float(result["value"][0]) >= probe_started for result in node_results),
        "no complete post-probe node scrape within 90 seconds",
        failures,
    )
    node_build_results = metric_results.get("node_build", [])
    measured_node_versions = {
        result["metric"].get("node_name"): result["metric"].get("version")
        for result in node_build_results
    }
    check(
        measured_node_versions
        == {name: EXPECTED_NODE_EXPORTER_VERSION for name in EXPECTED_NODES},
        f"node_exporter version set differs: {measured_node_versions}",
        failures,
    )
    check(
        len(node_build_results) == len(EXPECTED_NODES)
        and all(
            float(result["value"][0]) >= probe_started
            for result in node_build_results
        ),
        "no complete post-probe node_exporter build scrape within 90 seconds",
        failures,
    )
    def state_value(metric_name):
        results = metric_results.get(f"state:{metric_name}", [])
        return results, (
            float(results[0]["value"][1]) if len(results) == 1 else None
        )

    # Config gauges (enabled flags) — exact value match.
    for metric_name, expected_value in EXPECTED_CONFIG_METRICS.items():
        results, value = state_value(metric_name)
        check(
            len(results) == 1
            and results[0]["metric"].get("node_name") == f"{CLUSTER}-gnode1"
            and value == expected_value
            and float(results[0]["value"][0]) >= probe_started,
            f"invalid lifecycle config metric {metric_name}: {results}",
            failures,
        )

    # ISC-49 freshness unixtimes — non-zero and within an age window.
    now = time.time()
    for metric_name, max_age_days in EXPECTED_FRESHNESS_METRICS.items():
        results, value = state_value(metric_name)
        fresh = value and value > 0 and (now - value) <= max_age_days * 86400
        check(
            len(results) == 1
            and results[0]["metric"].get("node_name") == f"{CLUSTER}-gnode1"
            and fresh
            and float(results[0]["value"][0]) >= probe_started,
            f"stale or zero freshness metric {metric_name} "
            f"(value={value}, max_age_days={max_age_days}): {results}",
            failures,
        )

    # TLS cert expiry — 0 when disabled, future epoch when full.
    results, value = state_value("isa_tls_cert_expiry_unixtime")
    if TLS_EXPIRY_DISABLED_EXPECTED:
        check(
            len(results) == 1
            and results[0]["metric"].get("node_name") == f"{CLUSTER}-gnode1"
            and value == 0
            and float(results[0]["value"][0]) >= probe_started,
            f"unexpected non-zero TLS expiry in disabled mode: {results}",
            failures,
        )
    else:
        check(
            len(results) == 1 and value and value > now,
            f"TLS cert expiry not a future epoch in full mode: {results}",
            failures,
        )




    mysql_results = metric_results.get("mysql", [])
    measured_mysql_names = [
        result["metric"].get("service_name") for result in mysql_results
    ]
    measured_mysql = {
        result["metric"].get("service_name"): result["value"][1]
        for result in mysql_results
    }
    check(
        sorted(measured_mysql_names) == sorted(EXPECTED_MYSQL),
        f"native MySQL metric set differs: {sorted(measured_mysql_names)}",
        failures,
    )
    check(
        len(mysql_results) == len(EXPECTED_MYSQL)
        and all(float(result["value"][0]) >= probe_started for result in mysql_results),
        "no complete post-probe MySQL scrape within 90 seconds",
        failures,
    )
    check(
        measured_mysql.get(f"{CLUSTER}-gnode1-mysql") == "1",
        f"{CLUSTER}-gnode1-mysql is not reachable by PMM",
        failures,
    )

    galera_results = metric_results.get("galera", [])
    try:
        galera_cluster_size = float(galera_results[0]["value"][1])
    except (IndexError, KeyError, TypeError, ValueError):
        galera_cluster_size = 0
    check(
        len(galera_results) == 1 and galera_cluster_size > 0,
        "gnode1 Galera cluster-size metric is missing or invalid",
        failures,
    )
    check(
        len(galera_results) == 1
        and all(float(result["value"][0]) >= probe_started for result in galera_results),
        "no post-probe Galera scrape within 90 seconds",
        failures,
    )

    # ISC-46: ProxySQL metrics scraped by PMM (real proxysql_* series).
    proxysql_results = metric_results.get("proxysql", [])
    measured_proxysql_nodes = [
        result["metric"].get("node_name") for result in proxysql_results
    ]
    check(
        len(proxysql_results) == len(EXPECTED_PROXYSQL)
        and sorted(measured_proxysql_nodes) == sorted(
            name[: -len("-proxysql")] for name in EXPECTED_PROXYSQL
        ),
        f"proxysql metric set differs: {measured_proxysql_nodes}",
        failures,
    )
    check(
        len(proxysql_results) == len(EXPECTED_PROXYSQL)
        and all(
            float(result["value"][0]) >= probe_started for result in proxysql_results
        ),
        "no complete post-probe ProxySQL scrape within 90 seconds",
        failures,
    )



    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print(
        f"PASS: PMM {EXPECTED_PMM_VERSION}, 5 namespaced nodes, "
        f"5 node exporters {EXPECTED_NODE_EXPORTER_VERSION}, 3 MySQL services, "
        f"{len(EXPECTED_PROXYSQL)} ProxySQL metric exporters, "
        "QAN, live Galera/freshness/lifecycle metrics + ISC-47 alert rules verified (delivery pending BLK-5)"
    )


if __name__ == "__main__":
    sys.exit(main())
