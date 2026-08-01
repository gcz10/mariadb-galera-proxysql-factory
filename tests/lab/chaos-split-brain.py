#!/usr/bin/env python3
"""ISC-30/64: split-brain / network-partition test (lab-only, destructive).

Partitions one Galera node from the other two with firewalld direct rules, then proves:
  - the majority partition (2/3) stays a single Primary Component and accepts writes,
  - the minority partition (1/3) goes non-Primary and REFUSES writes,
  - therefore there are never two independent writable Primaries (ISC-30).
  - ISC-64: refuses to run on the production profile.

The partition is always healed (try/finally) and the node rejoins the cluster.
"""

import os
import re
import subprocess
import sys
import time
import yaml

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/lab-cluster/cluster.yml")
INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/lab-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER = yaml.safe_load(fh)
with open(INVENTORY, encoding="utf-8") as fh:
    INV = yaml.safe_load(fh)

ENVIRONMENT = CLUSTER["cluster"]["environment"]
GALERA = INV["all"]["children"]["galera"]["hosts"]


def _pick_minority(hosts):
    """Wybierz deterministycznie wezel do izolacji: najwyzszy galera_node_idx.

    Nazwa NIE moze byc zapisana na sztywno. Wcześniej bylo tu MINORITY="gnode3",
    co na klastrze o innych nazwach wezlow (np. gtnode1-3) dawalo host, ktorego
    nie ma w inventory — a `ansible <nieistniejacy-host>` konczy sie rc=0
    ("no hosts matched" nie jest bledem), wiec can_write() raportowal UDANY zapis
    na nieistniejacym wezle i test oglaszal falszywy split-brain. Dodatkowo blok
    finally wykonywal `iptables -F` na tej nazwie, czyli na klastrze, ktory
    faktycznie ma taki host, wyczyscilby mu cala polityke firewalla.
    """
    ranked = sorted(
        hosts.items(),
        key=lambda kv: (int((kv[1] or {}).get("galera_node_idx", 0)), kv[0]),
    )
    return ranked[-1][0]


MINORITY = _pick_minority(GALERA)
MAJORITY = [h for h in GALERA if h != MINORITY]   # pozostale zostaja Primary
if MINORITY not in GALERA or len(MAJORITY) != len(GALERA) - 1:
    raise SystemExit(
        f"nie udalo sie wybrac wezla mniejszosci z inventory: "
        f"MINORITY={MINORITY!r}, galera={sorted(GALERA)}"
    )
# Gorna granica czekania na przebudowe widoku po partycji. Nie jest to czas
# uspienia — patrz petla w main(), ktora czeka na obserwowalny stan. Budzet z
# zapasem nad evs.inactive_timeout (PT15S) + evs.install_timeout (PT7.5S).
PARTITION_CONVERGE_TIMEOUT = 90


def sh(node, script, timeout=60, check=False):
    cmd = [ANSIBLE, node, "-i", INVENTORY, "-m", "ansible.builtin.shell", "-a", script]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    # `ansible` konczy sie rc=0, gdy wzorzec hosta nie pasuje do NICZEGO. Bez tej
    # bramki kazde wywolanie na nieistniejacym wezle wygladalo jak sukces —
    # can_write() raportowal wtedy udany zapis i test oglaszal falszywy split-brain.
    # To zawsze blad testu, nigdy wynik, wiec nie zalezy od `check`.
    haystack = f"{r.stdout}\n{r.stderr}"
    for marker in ("Could not match supplied host pattern", "No hosts matched"):
        if marker in haystack:
            raise RuntimeError(
                f"wzorzec hosta {node!r} nie pasuje do zadnego hosta w {INVENTORY}: {marker}"
            )
    if check and r.returncode != 0:
        raise RuntimeError(f"ansible {node} failed: {r.stdout}\n{r.stderr}")
    return r


def body(node, result):
    out = result.stdout
    m = re.search(rf'^{re.escape(node)}\s*\|\s*\w+\s*\|\s*rc=\d+\s*>>?\s*$', out, re.M)
    return out[m.end():].strip() if m else out.strip()


def wsrep(node, var):
    q = f"SHOW STATUS LIKE '{var}'"
    r = sh(node, f'mariadb --socket=/var/lib/mysql/mysql.sock -N -B -e "{q}"')
    parts = body(node, r).split("\t")
    return parts[1] if len(parts) == 2 else ""


def ensure_write_target(node):
    """Utworz schemat testowy PRZED partycja, na zdrowym klastrze.

    Wczesniej robil to can_write() przez CREATE TABLE IF NOT EXISTS. To bylo
    zle z dwoch powodow:
    1) `CREATE TABLE IF NOT EXISTS isa_test.x` pada, gdy nie ma BAZY isa_test —
       a baze tworzyl dopiero chaos-failover.py. Na klastrze, gdzie tamten test
       nie byl uruchamiany, can_write() zwracalo False ZAWSZE, dla obu stron
       partycji. Asercja ISC-30 "mniejszosc nie przyjela zapisu" byla wtedy
       spelniana z bledu, nie z zachowania klastra — falszywy negatyw i ukryta
       zaleznosc miedzy testami.
    2) DDL w Galerze to operacja TOI o innej semantyce niz zwykly zapis, wiec
       nie powinna byc czescia testu zapisu.
    """
    sh(
        node,
        "mariadb --socket=/var/lib/mysql/mysql.sock -e \""
        "CREATE DATABASE IF NOT EXISTS isa_test; "
        "CREATE TABLE IF NOT EXISTS isa_test.split_brain (id BIGINT PRIMARY KEY)\"",
        check=True,
    )


def can_write(node, token):
    """Sprobuj zapisu na wezle; True gdy commit sie udal (samo DML, bez DDL)."""
    q = f"INSERT INTO isa_test.split_brain (id) VALUES ({token})"
    r = sh(node, f'timeout 8 mariadb --socket=/var/lib/mysql/mysql.sock -e "{q}"')
    return r.returncode == 0


def partition_rules(action):
    """action: 'add' wstawia reguly drop, 'remove' je usuwa (leczy partycje).

    Uzywa rich-rule firewalld (nftables), nie --direct. --direct idzie przez
    shim iptables-nft, ktory na EL10 nie obsluguje celu REJECT (brak modulu
    kernela) i gubi te semantyke. DROP przez rich-rule jest podtrzymywany na
    EL9 i EL10. Partycja to czarna dziura, nie odrzucenie — ale probe czeka
    na obserwowalny stan, a na 3-wezlowym klastrze EVS moze potrzebowac pelnego
    evs.inactive_timeout (PT15S) na instalacje nowego widoku, wiec budzet
    PARTITION_CONVERGE_TIMEOUT obsluguje ten margines. check=True przerywa,
    gdy nie udalo sie zalozyc izolacji: bez niej zadna obserwacja nie ma wartosci.
    """
    heal = action == "remove"
    cmd = "add" if not heal else "remove"
    for peer in MAJORITY:
        ip = GALERA[peer]["galera_node_address"]
        for direction in (f'source address="{ip}"', f'destination address="{ip}"'):
            sh(
                MINORITY,
                f'firewall-cmd --{cmd}-rich-rule '
                f"'rule family=ipv4 {direction} drop'",
                check=not heal,
            )


def main():
    failures = []

    if ENVIRONMENT == "production":
        print("REFUSED: chaos-split-brain is destructive and must not run on production (ISC-64)")
        return 1

    # Schemat testowy musi istniec ZANIM zalozymy partycje — na zdrowym klastrze
    # zapis sie zreplikuje na wszystkie wezly, wiec pozniej can_write() mierzy
    # wylacznie zdolnosc do commitu, a nie brak tabeli.
    ensure_write_target(MAJORITY[0])

    partitioned = False
    try:
        # Isolate the minority node from the majority.
        partition_rules("add")
        partitioned = True
        print(f"partitioned {MINORITY} from {MAJORITY}; czekam na przebudowe widoku")
        # Nie usypiaj na stala. Provider potrzebuje evs.inactive_timeout (PT15S)
        # plus evs.install_timeout (PT7.5S) = 22.5s, wiec dawne PARTITION_WAIT=25
        # dawalo 2.5s marginesu.
        #
        # Nie wystarczy tez pojedyncze trafienie na JEDNYM wezle: widok potrafi
        # przebudowac sie wiecej niz raz, wiec probe lapal moment miedzy
        # instalacjami i raportowal falszywa utrate kworum przez wiekszosc
        # (status=non-Primary przy size=2), choc reczna partycja pokazuje
        # stabilne Primary. Wymagamy wiec zgodnosci WSZYSTKICH wezlow wiekszosci
        # w dwoch kolejnych probkach — obserwacja stabilnego stanu zamiast
        # zgadywania czasu.
        deadline = time.time() + PARTITION_CONVERGE_TIMEOUT
        stable = 0
        last_seen = {}
        while time.time() < deadline:
            last_seen = {
                node: (
                    wsrep(node, "wsrep_cluster_status"),
                    wsrep(node, "wsrep_cluster_size"),
                )
                for node in MAJORITY
            }
            converged = all(
                st == "Primary" and sz == str(len(MAJORITY))
                for st, sz in last_seen.values()
            )
            stable = stable + 1 if converged else 0
            if stable >= 2:
                break
            time.sleep(3)
        else:
            # Budzet wyczerpany. Bez odczytu per-wezel taki timeout jest nie do
            # zdiagnozowania: raport ponizej pokazuje tylko MAJORITY[0], wiec nie
            # widac, czy widok nie zbiegl sie w ogole, czy tylko na jednym wezle.
            print(
                f"UWAGA: widok nie ustabilizowal sie w {PARTITION_CONVERGE_TIMEOUT}s; "
                f"ostatni odczyt: "
                + ", ".join(f"{n}={st}/{sz}" for n, (st, sz) in sorted(last_seen.items()))
            )

        maj_status = wsrep(MAJORITY[0], "wsrep_cluster_status")
        maj_size = wsrep(MAJORITY[0], "wsrep_cluster_size")
        min_status = wsrep(MINORITY, "wsrep_cluster_status")
        min_size = wsrep(MINORITY, "wsrep_cluster_size")
        print(f"majority {MAJORITY[0]}: status={maj_status} size={maj_size}; "
              f"minority {MINORITY}: status={min_status} size={min_size}")

        token = int(time.time())
        maj_write = can_write(MAJORITY[0], token)
        min_write = can_write(MINORITY, token + 1)

        # Majority: single Primary of size 2, accepts writes.
        if maj_status != "Primary":
            failures.append(f"majority {MAJORITY[0]} status={maj_status} (expected Primary)")
        if maj_size != str(len(MAJORITY)):
            failures.append(f"majority size={maj_size} (expected {len(MAJORITY)})")
        if not maj_write:
            failures.append(f"majority {MAJORITY[0]} could NOT write while Primary")

        # Minority: non-Primary, refuses writes — no second writable Primary.
        if min_status == "Primary":
            failures.append(
                f"SPLIT-BRAIN: minority {MINORITY} is also Primary (two writable Primaries)")
        if min_write:
            failures.append(
                f"SPLIT-BRAIN: minority {MINORITY} accepted a write while partitioned (ISC-30)")

    finally:
        if partitioned:
            partition_rules("remove")
            # Awaryjnie: usun ewentualne pozostale reguly direct i przeladuj
            # polityke, gdyby usuwanie po nazwie czegos nie objelo. `iptables -F`
            # nie ma na EL10, a nawet gdyby bylo, kasowalo by CALA polityke hosta.
            sh(MINORITY, "firewall-cmd --reload", check=False)
            # Wait for the minority to rejoin the Primary Component.
            for _ in range(20):
                if wsrep(MINORITY, "wsrep_local_state_comment") == "Synced" and \
                   wsrep(MINORITY, "wsrep_cluster_size") == str(len(GALERA)):
                    break
                time.sleep(3)

    # Confirm the cluster healed back to full size, single Primary.
    healed_size = wsrep(MAJORITY[0], "wsrep_cluster_size")
    if healed_size != str(len(GALERA)):
        failures.append(f"cluster did not heal: size={healed_size} (expected {len(GALERA)})")

    if failures:
        print("FAIL: split-brain test failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"PASS: no split-brain — during partition only the majority ({MAJORITY[0]}, size 2) "
        f"was Primary and writable; minority ({MINORITY}) went non-Primary and refused writes; "
        f"cluster healed to size {healed_size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
