#!/usr/bin/env python3
"""Verify that the lab cluster is registered in PMM's native inventory."""

import base64
import json
import os
import re
import ssl
import sys
import time
import yaml
from urllib.parse import quote
from urllib.request import Request, urlopen

PMM_USER = os.environ.get("PMM_ADMIN_USER", "admin")
PMM_PASSWORD = os.environ.get("PMM_ADMIN_PASSWORD")
CONFIG_PATH = os.environ.get(
    "CLUSTER_CONFIG", "clusters/example-cluster/cluster.yml"
)
with open(CONFIG_PATH, encoding="utf-8") as config_file:
    CLUSTER_CONFIG = yaml.safe_load(config_file)
INVENTORY_PATH = os.environ.get(
    "CLUSTER_INVENTORY",
    os.path.join(os.path.dirname(CONFIG_PATH), "inventory.yml"),
)
with open(INVENTORY_PATH, encoding="utf-8") as inventory_file:
    INVENTORY = yaml.safe_load(inventory_file)
PMM_CONFIG = CLUSTER_CONFIG["monitoring"]["pmm"]
PMM_URL = os.environ.get("PMM_SERVER_URL", PMM_CONFIG["server_url"]).rstrip("/")
with open(CLUSTER_CONFIG["versions"]["lock_file"], encoding="utf-8") as lock_file:
    VERSION_LOCK = yaml.safe_load(lock_file)


CLUSTER = PMM_CONFIG["cluster_name"]
INVENTORY_GROUPS = INVENTORY["all"]["children"]
GALERA_HOSTS = INVENTORY_GROUPS["galera"]["hosts"]
PROXYSQL_HOSTS = INVENTORY_GROUPS["proxysql"]["hosts"]
# Wezly ProxySQL sa WSPOLNE dla floty i rejestruje je w PMM wylacznie warstwa
# wspolna (`make platform-monitoring`). Do 2026-08-21 robil to klaster
# z `proxysql.role: owner` i ta sonda miala galaz ownera z domyslna wartoscia
# `owner` — po usunieciu pola KAZDY najemca bylby uznany za ownera i sonda
# wymagalaby od niego wezlow `<cluster>-fcp1/2`, ktorych f11 slusznie nie
# tworzy. Najemca nie ma tam wlasnych eksporterow: pmm-agent ma jedna tozsamosc
# node per host, wiec rejestracja pod etykieta najemcy dublowalaby metryki
# tej samej maszyny (pilnuje tego `tests/lab/probe-platform.py`).
#
# Hosty z LOKALNYM pmm-agentem (monitoring.agent_groups) sa w PMM wezlami
# NATYWNYMI: node_exporter pochodzi z paczki pmm-client, nie z tarballa, i nie
# ma dla nich uslugi `external`. Kontrakt jest wiec dwutrybowy — mieszanie go
# dawaloby falszywe FAIL na kazdym zmigrowanym hoscie.
AGENT_GROUPS = set((CLUSTER_CONFIG.get("monitoring") or {}).get("agent_groups") or [])
_GROUPS = {"galera": GALERA_HOSTS}
AGENT_HOSTS = {
    host
    for group in AGENT_GROUPS
    for host in _GROUPS.get(group, {})
}
_ALL_MONITORED = dict(GALERA_HOSTS)
# Zbior WEZLOW jest wspolny dla obu trybow: pmm-agent rejestruje wezel generic
# tak samo jak sciezka agentless, wiec asercja zbioru wezlow i zapytania o metryki
# (filtrowane po node_name) MUSZA obejmowac wszystkie monitorowane hosty. Zawezenie
# go do agentless bylo bledem: dla klastra w calosci na pmm-client zbior byl PUSTY,
# przez co filtry metryk nie trafialy w nic i sonda raportowala brak backupu, ktory
# w rzeczywistosci dzialal.
MONITORED_HOSTS = dict(_ALL_MONITORED)
# Tylko te hosty maja usluge `external` node-exporter i binarke z tarballa —
# na hostach z pmm-agentem node_exporter pochodzi z paczki i nie ma tam uslugi
# external ani wersji z lockfile.
AGENTLESS_HOSTS = {h: v for h, v in _ALL_MONITORED.items() if h not in AGENT_HOSTS}
EXPECTED_NODES = {}
for host_name, host_vars in MONITORED_HOSTS.items():
    address = host_vars.get(
        "galera_node_address",
        host_vars.get("proxysql_node_address", host_vars["ansible_host"]),
    )
    EXPECTED_NODES[f"{CLUSTER}-{host_name}"] = address
# Nazwa uslugi MySQL zalezy od trybu: sciezka agentless rejestruje
# "<cluster>-<host>-mysql" (zdalny exporter pod agentem serwera), a host z lokalnym
# pmm-agentem "<cluster>-<host>-mysql-agent" (f11_pmm_agent.yml) — inaczej obie
# nazwy kolidowalyby na tym samym wezle.
EXPECTED_MYSQL = {
    (f"{CLUSTER}-{host_name}-mysql-agent" if host_name in AGENT_HOSTS
     else f"{CLUSTER}-{host_name}-mysql"): f"{CLUSTER}-{host_name}"
    for host_name in GALERA_HOSTS
}
# Typ agenta QAN wynika z monitoring.qan_source (slowlog wymaga lokalnego agenta).
QAN_AGENT_TYPE = (
    f"qan_mysql_{(CLUSTER_CONFIG.get('monitoring') or {}).get('qan_source', 'perfschema')}_agent"
)
# Usluga external node-exporter istnieje TYLKO dla hostow agentless.
EXPECTED_NODE_EXPORTERS = {
    f"{CLUSTER}-{host}-node-exporter" for host in AGENTLESS_HOSTS
}
# Podzial wezlow wg trybu. Asercje SZCZEGOLOWE musza sie rozejsc, bo na hoscie z
# lokalnym pmm-agentem node_exporter jest AGENTEM pod pmm_agent_id, a nie usluga
# `external` — zadanie uslugi dawaloby falszywy FAIL na zdrowej instalacji.
AGENT_NODES = {f"{CLUSTER}-{host}" for host in _ALL_MONITORED if host in AGENT_HOSTS}
AGENTLESS_NODES = {f"{CLUSTER}-{host}" for host in AGENTLESS_HOSTS}
# Pierwszy wezel Galera wg inventory — NIE zaszyte "gnode1".
FIRST_GALERA_NODE = f"{CLUSTER}-{next(iter(GALERA_HOSTS))}"
# Eksportery metryk ProxySQL naleza do warstwy wspolnej i sonda NAJEMCY nigdy
# ich nie oczekuje. Weryfikuje je `probe-platform.py` z `make platform-verify`.
EXPECTED_PROXYSQL: set[str] = set()
EXPECTED_CREDENTIALS_REVISION = str(PMM_CONFIG["credentials_revision"])
EXPECTED_NODE_EXPORTER_VERSION = str(VERSION_LOCK["node_exporter"]["version"])
EXPECTED_PMM_VERSION = str(VERSION_LOCK["pmm"]["version"])
# Lifecycle config gauges. WYPROWADZANE z cluster.yml, nie zaszyte: playbook
# liczy je z tej samej konfiguracji (f11_freshness.yml), wiec zaszyta wartosc
# opisywala tylko klaster, na ktorym sonde pisano. `isa_tls_monitoring_enabled`
# bylo na sztywno 0 i kazdy klaster z tls.mode=full oblewal sonde, mimo ze
# playbook publikowal poprawna 1.
TLS_MODE = CLUSTER_CONFIG.get("tls", {}).get("mode", "disabled")
EXPECTED_CONFIG_METRICS = {
    "isa_restore_test_monitoring_enabled": (
        1 if str(CLUSTER_CONFIG["backup"].get("restore_test_schedule", "")) else 0
    ),
    "isa_tls_monitoring_enabled": 1 if TLS_MODE == "full" else 0,
}
# ISC-49 freshness unixtimes: non-zero + within an age window after F10 runs.
BACKUP_FRESHNESS_SLA_HOURS = int(CLUSTER_CONFIG["backup"]["freshness_sla_hours"])
EXPECTED_FRESHNESS_METRICS = {
    # metric name → max acceptable age in hours
    "isa_restore_test_last_success_unixtime": 8 * 24,  # weekly schedule + 1d grace
}
EXPECTED_GALERA_BACKUP_METRICS = [
    "galera_backup_last_success_unixtime",
    "galera_backup_last_failure_unixtime",
    "galera_backup_last_run_success",
    "galera_backup_last_size_bytes",
    "galera_backup_last_duration_seconds",
]
# TLS cert expiry: 0 when tls.mode != full; future epoch when full.
TLS_EXPIRY_DISABLED_EXPECTED = TLS_MODE != "full"
ALL_STATE_METRICS = (
    set(EXPECTED_CONFIG_METRICS)
    | set(EXPECTED_FRESHNESS_METRICS)
    | set(EXPECTED_GALERA_BACKUP_METRICS)
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
    """Poll instant queries until every expected series was scraped after this probe.

    Kazda runda odpytuje KOMPLET zapytan. Wczesniej petla przerywala sie na pierwszym
    niespelnionym warunku, wiec wszystkie dalsze zapytania nigdy nie trafialy do
    wyniku i raportowaly sie jako `[]` — jedna realna usterka produkowala kilkanascie
    widmowych "brak metryki" dla danych, ktore w rzeczywistosci plynely.
    """
    deadline = time.monotonic() + timeout
    latest = {}
    while True:
        pending = False
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
                pending = True
        if not pending or time.monotonic() >= deadline:
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
    alert_contact_points = get_json(
        "/graph/api/v1/provisioning/contact-points"
    )
    alert_policy = get_json("/graph/api/v1/provisioning/policies")
    failures = []
    check(
        version.get("version") == EXPECTED_PMM_VERSION
        and version.get("distribution_method") == "DISTRIBUTION_METHOD_DOCKER",
        f"PMM runtime differs from Docker lock {EXPECTED_PMM_VERSION}: {version}",
        failures,
    )
    # ISC-47: managed alert rules plus SMTP delivery route.
    # UID-y sa namespace'owane etykieta klastra (f15_alerts.yml), inaczej drugi klaster
    # nadpisalby reguly pierwszego we wspolnym PMM.
    _cl = CLUSTER_CONFIG["monitoring"]["pmm"]["cluster_name"]
    # Zbior oczekiwanych regul wyprowadzamy z JEDYNEGO zrodla — playbooks/f15_alerts.yml —
    # zamiast go tu przepisywac. Wczesniej byla tu lista 6 UID-ow; prace nad backupem
    # dolozyly `backup-metrics-frozen` i `restore-drill-stale`, sonda nie nadgonila
    # i zaczela oblewac poprawnie zaprowizjonowany klaster. Parsowanie zrodla sprawia,
    # ze kazda kolejna regula jest uwzgledniona automatycznie.
    #
    # ZALOZENIE: liczymy wylacznie reguly SCOPE'OWANE klastrem, czyli takie, ktorych
    # uid zawiera `{{ cluster_label }}`. Regula floty (uid bez tej zmiennej) zostanie
    # tu celowo pominieta — tak samo jak legacy UID-y z listy sprzatajacej f15.
    alerts_playbook = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "playbooks", "f15_alerts.yml",
    )
    with open(alerts_playbook, encoding="utf-8") as alerts_file:
        alerts_source = alerts_file.read()
    # Reguly czytamy BLOKAMI, nie pojedyncza regexpa po UID: kazdy wpis moze
    # nosic marker (`shared`, `requires_tls`), ktory decyduje, czy regula w ogole
    # powstaje dla TEGO klastra. Sama lista UID-ow tego nie widzi i sonda
    # zadalaby od klastra regul, ktorych f15 u niego swiadomie nie tworzy.
    rule_blocks = re.split(r'^\s*-\s*uid:\s*', alerts_source, flags=re.M)[1:]
    rule_suffixes, shared_suffixes, tls_suffixes = [], [], []
    for block in rule_blocks:
        m = re.match(r'"isa-(\{\{\s*cluster_label\s*\}\}|shared)-([a-z0-9-]+)"', block)
        if not m:
            continue
        # Marker musi pochodzic z TEGO wpisu, a nie z nastepnego — tniemy blok
        # na pierwszym `- uid:`, wiec `body` konczy sie przed kolejna regula.
        body = block
        suffix = m.group(2)
        if m.group(1) == "shared":
            shared_suffixes.append(suffix)
        elif re.search(r'^\s+requires_tls:\s*true', body, re.M):
            tls_suffixes.append(suffix)
        else:
            rule_suffixes.append(suffix)
    if not rule_suffixes:
        raise SystemExit(f"FAIL: nie odczytano zadnej reguly z {alerts_playbook}")
    tls_full = (CLUSTER_CONFIG.get("tls", {}).get("mode", "disabled") == "full")
    expected_alert_rules = {f"isa-{_cl}-{suffix}" for suffix in rule_suffixes}
    # Reguly `isa-shared-*` opisuja wspolna pare ProxySQL i naleza do warstwy
    # wspolnej (`make platform-alerts`). Do 2026-08-21 wdrazal je klaster
    # z `proxysql.role: owner`, a ta sonda domyslnie zakladala ownera. Po
    # wyniesieniu warstwy takie zalozenie kazaloby jej wymagac tych regul od
    # KAZDEGO najemcy — czyli swiecic na czerwono na poprawnej konfiguracji.
    if tls_full:
        # Bez TLS metryka wygasania ma wartosc 0 i regula nie ma sensu.
        expected_alert_rules |= {f"isa-{_cl}-{suffix}" for suffix in tls_suffixes}
    managed_alert_rules = [
        rule
        for rule in alert_rules
        if rule.get("labels", {}).get("managed_by") == "ansible"
        and rule.get("labels", {}).get("cluster") == CLUSTER
    ]
    managed_uids = {rule.get("uid") for rule in managed_alert_rules}
    check(
        managed_uids == expected_alert_rules,
        f"ISC-47 managed alert rules differ: got {sorted(managed_uids)}, "
        f"expected {sorted(expected_alert_rules)}",
        failures,
    )
    # Falsyfikowalny straznik odwrotnego kierunku: gdyby najemca nazwal sie
    # `shared` albo ktos przywrocil galaz ownera, jego reguly wchlonelyby
    # przestrzen warstwy wspolnej — i teardown tego najemcy zabralby alerty
    # calej flocie. Ta sama klasa bledu, ktora repo naprawialo przy koncie MinIO.
    leaked = managed_uids & {f"isa-shared-{suffix}" for suffix in shared_suffixes}
    check(
        not leaked,
        f"najemca {CLUSTER} zarzadza regulami warstwy wspolnej {sorted(leaked)} — "
        f"naleza do `make platform-alerts`",
        failures,
    )

    # Sonda sprawdzala DOTAD tylko, czy reguly ISTNIEJA — nie czy sa spelnione.
    # Przez te luke przeszedl falszywie dodatni `no-writer` na klastrze-konsumencie:
    # regula filtrowala metryki ProxySQL po etykiecie `cluster`, a te nosi wylacznie
    # owner wspolnej pary, wiec zapytanie nie trafialo w nic i `or vector(0)` palilo
    # alert na stale przy w pelni sprawnym writerze. Po udanym buildzie ZADNA
    # zarzadzana regula nie ma prawa sie palic — inaczej albo klaster jest chory,
    # albo regula jest zla. Oba przypadki musza oblewac.
    rule_states = get_json("/graph/api/prometheus/grafana/api/v1/rules")
    state_groups = (rule_states or {}).get("data", {}).get("groups", [])
    evaluated = [
        rule
        for group in state_groups
        for rule in group.get("rules", [])
        if rule.get("labels", {}).get("managed_by") == "ansible"
        and rule.get("labels", {}).get("cluster") == CLUSTER
    ]
    # FAIL-CLOSED: pusta odpowiedz (zly endpoint, 404, HTML zamiast JSON) dalaby
    # `firing == []` i cichy PASS przy ZEROWYM pokryciu. Reguly musza sie znalezc.
    check(
        len(evaluated) == len(expected_alert_rules),
        f"ISC-47 stan regul nieodczytany: API zwrocilo {len(evaluated)} "
        f"zarzadzanych regul dla {CLUSTER}, oczekiwano {len(expected_alert_rules)}",
        failures,
    )
    firing = sorted(
        rule.get("name", rule.get("uid", "?"))
        for rule in evaluated
        if rule.get("state") == "firing"
    )
    check(
        not firing,
        f"ISC-47 zarzadzane reguly alertowe sie pala: {firing}",
        failures,
    )
    backup_failure_rule = next(
        (
            rule
            for rule in managed_alert_rules
            if rule.get("uid") == f"isa-{_cl}-backup-failed"
        ),
        {},
    )
    check(
        backup_failure_rule.get("for") == "0s",
        "Backup failure alert must evaluate immediately (for=0s)",
        failures,
    )
    # Reguly zwracaja 0 przy zdrowym klastrze (nie pusty wektor), wiec NoData oznacza
    # realna utrate zbierania metryk. Reguly KRYTYCZNE musza wtedy alarmowac (fail-closed).
    # Jedyny wyjatek to warning-level "not Synced": jego fallback (`or vector(4)`) celowo
    # zwraca wartosc zdrowa, bo utrata metryk jest juz pokryta przez dwie reguly krytyczne
    # i trzeci alarm o tej samej przyczynie bylby szumem.
    critical_rules = [
        rule
        for rule in managed_alert_rules
        if rule.get("labels", {}).get("severity") == "critical"
    ]
    bad_nodata = sorted(
        rule.get("uid")
        for rule in critical_rules
        if rule.get("noDataState") != "Alerting"
    )
    check(
        critical_rules and not bad_nodata,
        "ISC-47 critical alert rules must fail closed on NoData "
        f"(noDataState=Alerting); naruszaja: {bad_nodata}",
        failures,
    )
    expected_alert_email = CLUSTER_CONFIG["monitoring"]["alerts"]["email"]
    # UID i nazwa contact pointu sa namespace'owane etykieta klastra (tak samo jak
    # w f15_alerts.yml), zeby drugi klaster nie nadpisal punktu pierwszego.
    cluster_label = CLUSTER_CONFIG["monitoring"]["pmm"]["cluster_name"]
    expected_contact_uid = f"email-isa-{cluster_label}"
    expected_contact_name = f"ISA Email Alerts ({cluster_label})"
    email_contact = next(
        (
            point
            for point in alert_contact_points
            if point.get("uid") == expected_contact_uid
        ),
        None,
    )
    check(
        email_contact is not None
        and email_contact.get("type") == "email"
        and email_contact.get("settings", {}).get("addresses")
        == expected_alert_email,
        f"ISC-47 email contact point '{expected_contact_uid}' missing or wrong address",
        failures,
    )
    check(
        any(
            route.get("receiver") == expected_contact_name
            and ["managed_by", "=", "ansible"]
            in route.get("object_matchers", [])
            and ["cluster", "=", cluster_label]
            in route.get("object_matchers", [])
            for route in alert_policy.get("routes", [])
        ),
        "ISC-47 notification policy does not route managed alerts to email",
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

    for node_name in sorted(AGENTLESS_NODES):
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

    # Tryb agentowy: node_exporter to agent uruchomiony przez lokalnego pmm-agenta.
    # Pominiecie tych hostow daloby falszywy PASS przy martwym eksporterze, wiec
    # sprawdzamy ten sam fakt w ksztalcie, w ktorym on tu wystepuje.
    local_agent_by_node = {
        agent.get("runs_on_node_id"): agent for agent in agents.get("pmm_agent", [])
    }
    for node_name in sorted(AGENT_NODES):
        node = generic_nodes.get(node_name)
        if not node:
            continue
        local = local_agent_by_node.get(node.get("node_id"))
        check(
            local is not None,
            f"missing local pmm-agent on node: {node_name}",
            failures,
        )
        if not local:
            continue
        matches = [
            agent
            for agent in agents.get("node_exporter", [])
            if agent.get("pmm_agent_id") == local.get("agent_id")
        ]
        check(
            len(matches) == 1,
            f"expected one node_exporter agent under local pmm-agent for "
            f"{node_name}, got {len(matches)}",
            failures,
        )
        if len(matches) == 1:
            check(
                matches[0].get("status") == "AGENT_STATUS_RUNNING",
                f"node_exporter agent not running: {node_name}",
                failures,
            )

    # ISC-46: ProxySQL metrics — external services (group=proxysql) + agents (port 6070).
    # Tryb per host: wezel z lokalnym agentem ma usluge NATYWNA typu `proxysql`,
    # agentless — usluge `external` (grupa proxysql, restapi na 6070).
    managed_proxysql_services = [
        service
        for service in services.get("external", [])
        if service.get("cluster") == CLUSTER and service.get("group") == "proxysql"
    ] + [
        service
        for service in services.get("proxysql", [])
        if service.get("cluster") == CLUSTER
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
        # Ksztalt zalezy od trybu hosta: agentless ma agenta `external_exporter`
        # odpytujacego restapi ProxySQL na 6070, host z lokalnym agentem —
        # natywnego `proxysql_exporter` pod tym agentem. Sprawdzamy ten, ktory
        # dla danego hosta jest poprawny, zeby martwy eksporter nadal oblewal.
        host_node = node_name[: -len("-proxysql")]
        if host_node in AGENT_NODES:
            agent_matches = [
                agent
                for agent in agents.get("proxysql_exporter", [])
                if agent.get("service_id") == service.get("service_id")
            ]
            kind = "native proxysql_exporter"
        else:
            agent_matches = [
                agent
                for agent in external_agents.get(service.get("service_id"), [])
                if agent.get("listen_port") == 6070
            ]
            kind = "external-exporter agent (port 6070)"
        check(
            len(agent_matches) == 1,
            f"expected one {kind} for {node_name}, got {len(agent_matches)}",
            failures,
        )
        if len(agent_matches) == 1:
            check(
                agent_matches[0].get("status") == "AGENT_STATUS_RUNNING",
                f"proxysql exporter not running: {node_name}",
                failures,
            )
            if host_node in AGENT_NODES:
                # Bez push metryki cicho nie doplywaja: PMM wybralby tryb pull,
                # a minimalna polityka firewalld nie przepuszcza portow 42000+.
                # API PRZYJMUJE `push_metrics`, a ZWRACA `push_metrics_enabled` —
                # pytanie o nazwe zapisu zawsze dawalo brak pola i falszywy FAIL.
                check(
                    bool(agent_matches[0].get("push_metrics_enabled")),
                    f"proxysql exporter not in push mode: {node_name}",
                    failures,
                )
            else:
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
    for agent in agents.get(QAN_AGENT_TYPE, []):
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
        # Lokalny agent laczy sie z baza przez petle zwrotna wezla, na ktorym stoi;
        # adres wezla widzialby tylko exporter zdalny. Oczekiwanie jednego adresu
        # dla obu trybow oblewalo poprawna instalacje pmm-client.
        expected_address = (
            "127.0.0.1" if node_name in AGENT_NODES else EXPECTED_NODES[node_name]
        )
        check(
            service.get("address") == expected_address
            and service.get("port") == 3306,
            f"{service_name} targets wrong address or port "
            f"(oczekiwano {expected_address}:3306, jest "
            f"{service.get('address')}:{service.get('port')})",
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

    # Nazwy wezlow biora sie z inventory, NIE z zaszytego wzorca. Wczesniej bylo tu
    # sztywne "gnode[1-3]" — klaster z wezlami gnode4-6 przechodzil poprawnie caly
    # deploy, a sonda i tak raportowala brak metryk, bo regex do nich nie pasowal.
    # RE2 (PromQL) odrzuca "\-" jako nieznana sekwencje ucieczki, a re.escape() w
    # starszych Pythonach wlasnie tak escapuje myslnik. Escapujemy tylko kropke,
    # jedyny metaznak wystepujacy w nazwach wezlow/serwisow.
    def _alt(names):
        return "|".join(name.replace(".", r"\.") for name in sorted(names))

    node_alt = _alt(EXPECTED_NODES)
    mysql_alt = _alt(EXPECTED_MYSQL)
    galera_service = sorted(EXPECTED_MYSQL)[0]
    # Agregacja po node_name, NIE filtr po rozdzielczosci: lokalny agent publikuje
    # kazda metryke w hr/mr/lr, eksporter zewnetrzny tylko w jednej, a ktora — zalezy
    # od metryki (node_load1 trafia gdzie indziej niz build_info). Kazdy filtr `job`
    # gubil wiec jeden z trybow; `max by` daje dokladnie jedna serie na wezel w obu.
    node_query = quote(
        f'max by (node_name) (node_load1{{node_name=~"{node_alt}"}})'
    )
    node_build_query = quote(
        f'max by (node_name, version) '
        f'(node_exporter_build_info{{node_name=~"{node_alt}"}})'
    )
    mysql_query = quote(f'mysql_up{{service_name=~"{mysql_alt}",job=~".*_hr"}}')
    galera_query = quote(
        f'mysql_global_status_wsrep_cluster_size{{service_name="{galera_service}"}}'
    )
    state_queries = {
        name: quote(f'{name}{{cluster="{CLUSTER}"}}')
        for name in ALL_STATE_METRICS
    }
    # Nazwa metryki zalezy od zrodla: eksporter zewnetrzny odpytuje restapi ProxySQL
    # i emituje `proxysql_servers_table_version_total` z etykieta `external_group`,
    # natywny proxysql_exporter Percony — `proxysql_up` bez tej etykiety.
    if PROXYSQL_HOSTS and all(host in AGENT_HOSTS for host in PROXYSQL_HOSTS):
        proxysql_query = quote(
            f'max by (service_name, node_name) '
            f'(proxysql_up{{cluster="{CLUSTER}"}})'
        )
    else:
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
    # Wersje niosa DWA rozne kontrakty. Agentless pobiera tarball wskazany w
    # lockfile, wiec musi zgadzac sie co do znaku. Host z pmm-clientem dostaje
    # eksporter z paczki — jego wersja wynika z PRZYPIETEJ wersji pmm-client i
    # jest sprawdzana osobno; tutaj wymagamy, by w ogole sie raportowala, bo
    # brak wpisu oznacza martwy eksporter, a nie inny model wersjonowania.
    expected_node_versions = {
        name: EXPECTED_NODE_EXPORTER_VERSION for name in AGENTLESS_NODES
    }
    check(
        {k: v for k, v in measured_node_versions.items() if k in AGENTLESS_NODES}
        == expected_node_versions,
        f"node_exporter version set differs (agentless): "
        f"{ {k: v for k, v in measured_node_versions.items() if k in AGENTLESS_NODES} }",
        failures,
    )
    missing_agent_versions = sorted(
        name for name in AGENT_NODES if not measured_node_versions.get(name)
    )
    check(
        not missing_agent_versions,
        f"node_exporter build info missing on agent hosts: {missing_agent_versions}",
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
            and results[0]["metric"].get("node_name") == FIRST_GALERA_NODE
            and value == expected_value
            and float(results[0]["value"][0]) >= probe_started,
            f"invalid lifecycle config metric {metric_name}: {results}",
            failures,
        )

    # ISC-49 freshness unixtimes — non-zero and within an age window.
    now = time.time()
    for metric_name, max_age_hours in EXPECTED_FRESHNESS_METRICS.items():
        results, value = state_value(metric_name)
        fresh = value and value > 0 and (now - value) <= max_age_hours * 3600
        check(
            len(results) == 1
            and results[0]["metric"].get("node_name") == FIRST_GALERA_NODE
            and fresh
            and float(results[0]["value"][0]) >= probe_started,
            f"stale or zero freshness metric {metric_name} "
            f"(value={value}, max_age_hours={max_age_hours}): {results}",
            failures,
        )

    # Backup runner metrics — one fresh series from the scheduler, with the
    # logical-cluster and backend labels used by the managed alert rules.
    for metric_name in EXPECTED_GALERA_BACKUP_METRICS:
        results, value = state_value(metric_name)
        labels = results[0]["metric"] if len(results) == 1 else {}
        valid_value = value is not None and value >= 0
        if metric_name == "galera_backup_last_success_unixtime":
            valid_value = bool(
                value
                and value > 0
                and (now - value) <= BACKUP_FRESHNESS_SLA_HOURS * 3600
            )
        elif metric_name == "galera_backup_last_run_success":
            valid_value = value == 1
        elif metric_name == "galera_backup_last_size_bytes":
            valid_value = bool(value and value > 0)
        check(
            len(results) == 1
            and labels.get("node_name") == FIRST_GALERA_NODE
            and labels.get("logical_cluster")
            == CLUSTER_CONFIG["cluster"]["name"]
            and labels.get("backend") == CLUSTER_CONFIG["backup"]["destination"]
            and valid_value
            and float(results[0]["value"][0]) >= probe_started,
            f"invalid Galera backup metric {metric_name}: {results}",
            failures,
        )

    # TLS cert expiry — 0 when disabled, future epoch when full.
    results, value = state_value("isa_tls_cert_expiry_unixtime")
    if TLS_EXPIRY_DISABLED_EXPECTED:
        check(
            len(results) == 1
            and results[0]["metric"].get("node_name") == FIRST_GALERA_NODE
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
    # Nazwa uslugi pierwszego wezla zalezy od trybu (-mysql vs -mysql-agent), wiec
    # bierzemy ja z EXPECTED_MYSQL zamiast doklejac staly sufiks.
    first_mysql_service = next(
        name for name, node in EXPECTED_MYSQL.items() if node == FIRST_GALERA_NODE
    )
    check(
        measured_mysql.get(first_mysql_service) == "1",
        f"{first_mysql_service} is not reachable by PMM",
        failures,
    )

    galera_results = metric_results.get("galera", [])
    try:
        galera_cluster_size = float(galera_results[0]["value"][1])
    except (IndexError, KeyError, TypeError, ValueError):
        galera_cluster_size = 0
    check(
        len(galera_results) == 1 and galera_cluster_size > 0,
        f"{FIRST_GALERA_NODE} Galera cluster-size metric is missing or invalid",
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
        f"PASS: PMM {EXPECTED_PMM_VERSION}, {len(EXPECTED_NODES)} namespaced nodes, "
        f"{len(EXPECTED_NODE_EXPORTERS)} node exporters {EXPECTED_NODE_EXPORTER_VERSION}, "
        f"{len(EXPECTED_MYSQL)} MySQL services, "
        f"{len(EXPECTED_PROXYSQL)} ProxySQL metric exporters, QAN, live "
        "Galera/freshness/lifecycle metrics + ISC-47 rules and email route verified"
    )


if __name__ == "__main__":
    sys.exit(main())
