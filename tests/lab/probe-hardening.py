#!/usr/bin/env python3
"""Verify MariaDB hardening: ISC-40, ISC-41, ISC-42 + waznosc certyfikatow TLS."""

import os
import re
import subprocess
import sys
import yaml

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")
_inv = yaml.safe_load(open(INVENTORY))
GALERA_NODE = list(_inv["all"]["children"]["galera"]["hosts"])[0]
with open(CONFIG_PATH, encoding="utf-8") as _fh:
    CLUSTER_CONFIG = yaml.safe_load(_fh)
# Prog ostrzegania o wygasaniu: 30 dni. Dokumentacja Galery mowi wprost, ze
# socket.ssl_* "are not dynamic" — wymiana certu to restart KAZDEGO wezla, wiec
# ostrzezenie musi przyjsc z zapasem na zaplanowanie rolling restartu.
CERT_MIN_DAYS = int(os.environ.get("TLS_CERT_MIN_DAYS", "30"))


def run_query(node, query):
    """Run a MariaDB query on a node via ansible, return raw stdout."""
    cmd = [
        ANSIBLE, node, "-i", INVENTORY, "-m", "ansible.builtin.shell",
        "-a", f'mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "{query}"',
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

def run_shell(pattern, script):
    """Run a shell snippet on a host pattern via ansible, return {node: body}."""
    cmd = [
        ANSIBLE, pattern, "-i", INVENTORY, "-m", "ansible.builtin.shell",
        "-a", script, "--fork", "5",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
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

    # ISC-40: no anonymous users
    anon = run_query(GALERA_NODE, "SELECT CONCAT(user,'@',host) FROM mysql.user WHERE user=''")
    anon_users = anon.get(GALERA_NODE, "").strip()
    check(not anon_users, f"ISC-40: anonymous users exist: {anon_users}", failures)

    # ISC-40: no test database
    testdb = run_query(GALERA_NODE, "SHOW DATABASES LIKE 'test'")
    test_result = testdb.get(GALERA_NODE, "").strip()
    check(not test_result, f"ISC-40: test database exists: {test_result}", failures)

    # ISC-40: no empty passwords for non-system accounts
    empty_pw = run_query(
        GALERA_NODE,
        "SELECT CONCAT(user,'@',host) FROM mysql.user "
        "WHERE (authentication_string='' OR authentication_string IS NULL) "
        "AND user NOT IN ('mariadb.sys','mysql','PUBLIC') AND user != ''"
    )
    empty_users = empty_pw.get(GALERA_NODE, "").strip()
    check(not empty_users, f"ISC-40: empty password accounts: {empty_users}", failures)

    # ISC-41: root localhost-only
    remote_root = run_query(
        GALERA_NODE,
        "SELECT CONCAT(user,'@',host) FROM mysql.user "
        "WHERE user='root' AND host NOT IN ('localhost','127.0.0.1','::1')"
    )
    remote_root_result = remote_root.get(GALERA_NODE, "").strip()
    check(
        not remote_root_result,
        f"ISC-41: root has remote access: {remote_root_result}",
        failures,
    )

    # ISC-42: sst_user least privilege
    sst_grants = run_query(GALERA_NODE, "SHOW GRANTS FOR 'sst_user'@'localhost'")
    sst_result = sst_grants.get(GALERA_NODE, "")
    check(
        "RELOAD" in sst_result and "PROCESS" in sst_result and "LOCK TABLES" in sst_result,
        f"ISC-42: sst_user missing required grants: {sst_result}",
        failures,
    )

    # ISC-42: pmm_monitor least privilege
    pmm_grants = run_query(GALERA_NODE, "SHOW GRANTS FOR 'pmm_monitor'@'%'")
    pmm_result = pmm_grants.get(GALERA_NODE, "")
    check(
        "PROCESS" in pmm_result and "SELECT" in pmm_result,
        f"ISC-42: pmm_monitor missing required grants: {pmm_result}",
        failures,
    )

    # Waznosc certyfikatow TLS na KAZDYM wezle Galery (gdy tls.mode == full).
    #
    # Nasze certy sa wystawiane recznie przez tests/lab/tls/generate.sh i nikt ich
    # nie rotuje — CA i lisc dostaja 1095 dni, po czym po prostu wygasaja. Sciezka
    # naprawy jest droga: dokumentacja Galery mowi, ze socket.ssl_* "are not
    # dynamic", wiec wymiana certu wymaga restartu kazdego wezla. Dlatego prog
    # ostrzegania (30 dni) ma dac czas na zaplanowany rolling restart, a nie
    # postawic operatora przed faktem dokonanym w niedziele.
    #
    # Sprawdzamy wszystkie wezly, nie tylko pierwszy: dystrybucja moze byc
    # niekompletna i wtedy jeden wezel ma stary albo zaden cert.
    tls_mode = (CLUSTER_CONFIG.get("tls") or {}).get("mode", "disabled")
    tls_note = "TLS wylaczony"
    if tls_mode == "full":
        cert_script = (
            "for kv in $(grep -hE '^[[:space:]]*(ssl_ca|ssl_cert)[[:space:]]*=' "
            "/etc/my.cnf.d/*.cnf 2>/dev/null | tr -d ' \\t'); do "
            "f=${kv#*=}; "
            "[ -r \"$f\" ] || { echo \"MISSING $f\"; continue; }; "
            f"if openssl x509 -in \"$f\" -noout -checkend {CERT_MIN_DAYS * 86400} >/dev/null 2>&1; "
            "then echo \"OK $f\"; "
            "else echo \"EXPIRING $f $(openssl x509 -in \"$f\" -noout -enddate 2>/dev/null "
            "| cut -d= -f2 | tr ' ' '_')\"; fi; done"
        )
        cert_raw = run_shell("galera", cert_script)
        checked = 0
        for node, body in sorted(cert_raw.items()):
            lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
            if not lines:
                failures.append(
                    f"{node}: tls.mode=full, ale w /etc/my.cnf.d nie ma ssl_ca/ssl_cert "
                    f"— TLS Galery nie jest skonfigurowany"
                )
                continue
            for line in lines:
                checked += 1
                if line.startswith("MISSING "):
                    failures.append(f"{node}: brak pliku certyfikatu {line.split(' ', 1)[1]}")
                elif line.startswith("EXPIRING "):
                    parts = line.split()
                    when = parts[2].replace("_", " ") if len(parts) > 2 else "?"
                    failures.append(
                        f"{node}: certyfikat {parts[1]} wygasa przed uplywem "
                        f"{CERT_MIN_DAYS} dni (notAfter: {when}) — wymiana wymaga "
                        f"rolling restartu calego klastra"
                    )
        tls_note = f"certy TLS wazne >{CERT_MIN_DAYS} dni ({checked} plikow na {len(cert_raw)} wezlach)"

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print(
        "PASS: MariaDB hardening verified — no anon/test/empty-pw, root localhost-only, "
        f"least privilege SST+monitor; {tls_note}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
