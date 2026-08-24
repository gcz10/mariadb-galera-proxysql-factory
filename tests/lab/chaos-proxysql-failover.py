#!/usr/bin/env python3
"""Co sie dzieje z ruchem aplikacji, gdy pada WEZEL ProxySQL (lab-only, destrukcyjny).

Caly dotychczasowy chaos celowal w Galere: zabijany byl writer albo dzielona siec
wezlow bazy. Warstwa posrednia — ta, przez ktora aplikacja faktycznie chodzi —
byla sprawdzana wylacznie w stanie ustalonym. Ta sonda zamyka te luke.

TRZY ROZNE TRYBY AWARII, bo sprawdzaja trzy rozne mechanizmy:

  PROXYSQL_FAILOVER_MODE=worker
    Ginie WYLACZNIE proces obslugujacy ruch; angel process (rodzic) zyje.
    To jedyna awaria, ktora ProxySQL obsluguje SAM: dokumentacja (watchdog)
    mowi "The parent process (which does not serve traffic) automatically
    restarts the child process responsible for handling traffic", a FAQ
    obiecuje restart "in less than a second". Poprawne zachowanie to wiec
    BRAK failoveru — asercja jest tu ODWROCONA i przelaczenie VIP bylo by
    bledem (zbedne przerwanie ruchu, ryzyko flappingu przy kazdym restarcie).

  PROXYSQL_FAILOVER_MODE=service (domyslny)
    Ginie CALY ProxySQL — `pkill -x proxysql` trafia w rodzica I dziecko, wiec
    angel nie ma kogo wskrzesic, a jednostka systemd ma Restart=no (zmierzone).
    DECYZJA: zostawiamy Restart=no, jednostki producenta nie zmieniamy. Angel
    obsluguje crash workera (tryb `worker` wyzej, 0,1 s przerwy), a smierc samego
    angela jest rzadka i NIE powoduje awarii — keepalived zabiera VIP i ruch
    idzie dalej. Brakowalo wylacznie WIDOCZNOSCI tego stanu i to zostalo
    domkniete regula ISC-47 `isa-shared-proxysql-down`, ktora liczy zdrowe
    instancje. `Restart=on-failure` dodalby samoleczenie, ale maskowalby petle
    crashy na wspoldzielonej infrastrukturze — alert mowi prawde, auto-restart
    ja ukrywa.
    Maszyna i keepalived zyja, VRRP nie widzi tu NICZEGO — sasiad dostaje
    advertisementy jak gdyby nic. Jedyne, co moze zabrac VIP, to
    `vrrp_script chk_proxysql` z `weight 0`, ktory wprowadza instancje w FAULT.
    Jesli track_script nie dziala, VIP zostaje na maszynie z martwym ProxySQL
    i mamy PELNA niedostepnosc bez zadnego przelaczenia — najgorszy mozliwy
    wynik, i taki, ktorego zwykly test "wylacz maszyne" NIGDY nie wykryje.

  PROXYSQL_FAILOVER_MODE=node
    Znika cala maszyna (twardy stop przez API Proxmoksa). Tu dziala klasyczny
    VRRP: sasiad przestaje dostawac advertisementy i przejmuje VIP.

CZEGO PILNUJE:
  1. PRZELACZENIE: VIP musi trafic na drugi wezel w czasie < RTO z cluster.yml.
  2. CIAGLOSC: aplikacja musi wrocic do zapisywania bez interwencji.
  3. BRAK UTRATY: kazda transakcja POTWIERDZONA klientowi przed awaria musi
     istniec po przelaczeniu. Zerwane polaczenia sa oczekiwane i dozwolone —
     utrata potwierdzonego commita nie jest.
  4. WIELODZIERZAWNOSC: wezel przejmujacy musi miec hostgroupy TEGO klastra.
     Konfiguracja ProxySQL nie replikuje sie sama; gdyby f7 wdrozyl ja tylko na
     aktywnym wezle, failover przelaczylby ruch w pustke.

Zerwanie istniejacych polaczen jest NIEUNIKNIONE i sonda tego nie karze:
ProxySQL nie replikuje stanu sesji miedzy wezlami, wiec sesje TCP gina razem
z wezlem. Sonda mierzy, ILE to kosztuje i czy klient wstaje sam.

ZMIERZONE na tej flocie (n11, VIP .133, fcp1->fcp2):
  worker  — VIP SIE NIE RUSZA, przerwa w commitach 0,1 s
  node    — VIP u sasiada po 3,3-5,4 s, przerwa w commitach 3,1-4,1 s
  service — VIP u sasiada po 5,5 s,     przerwa w commitach 3,5-4,6 s
Roznica rzedu wielkosci miedzy `worker` a reszta jest sednem: samoleczacy sie
crash kosztuje aplikacje 0,1 s, a kazde przelaczenie VIP ~4 s. Dlatego failover
przy crashu workera bylby REGRESJA, nie zabezpieczeniem.
`service` jest wolniejszy od `node` i to tez zgadza sie z konfiguracja: VRRP
wykrywa cisze po ~3 advertisementach (advert_int 1), a chk_proxysql musi zawiesc
dwa razy (interval 2 x fall 2) zanim wprowadzi instancje w FAULT.
We wszystkich trybach: 0 utraconych potwierdzonych transakcji, sesja dlugozyjaca
zerwana (ERROR 2013 "Lost connection to server") — takze w trybie `worker`, gdzie
wezel pozostaje aktywny: sesja ginie z procesem, nie z wezlem.

ZALOZENIE DOKUMENTACJI, KTOREGO NIE SPELNIAMY SWIADOMIE: ProxySQL Cluster
(proxysql_servers, admin-cluster_username) synchronizuje mysql_servers,
mysql_users, query rules i zmienne miedzy wezlami. My tego NIE uzywamy — obie
instancje konfiguruje f7 z repo, bo zrodlem prawdy jest cluster.yml, a nie stan
jednego wezla. Konsekwencja jest realna i dlatego sonda asertuje punkt 4 PRZED
zabiciem: nic nie replikuje konfiguracji automatycznie.

OBSERWACJA BEZ WYJASNIENIA: powrot VIP na naprawiony wezel kosztuje ~0 s przerwy
(zmierzone 0,0 i 0,1 s), mimo `preempt_delay 30` w keepalived.conf.j2 VIP wracal
po 7 s (service) i 2 s (node) od przywrocenia. Rozbieznosc miedzy konfiguracja a
pomiarem jest ODNOTOWANA, nie wytlumaczona — mechanizmu nie zgaduje. Do
sprawdzenia w dokumentacji keepalived przy okazji prac nad endpointem.

Wymaga APP_DB_PASSWORD. Tryb `node` wymaga PROXMOX_VE_API_TOKEN i mapowania
nazwa->VMID w PROXYSQL_VMIDS (np. "fcp1=9401,fcp2=9402").
Odmawia uruchomienia na profilu produkcyjnym (ISC-64).
"""

import os
import re
import subprocess
import sys
import time
import urllib3
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONFIG_PATH = os.environ.get("CLUSTER_CONFIG", "clusters/example-cluster/cluster.yml")
INVENTORY = os.environ.get("CLUSTER_INVENTORY", "clusters/example-cluster/inventory.yml")
ANSIBLE = os.environ.get("ANSIBLE", "ansible")
APP_PW = os.environ.get("APP_DB_PASSWORD", "")
MODE = os.environ.get("PROXYSQL_FAILOVER_MODE", "service")

with open(CONFIG_PATH, encoding="utf-8") as fh:
    CLUSTER = yaml.safe_load(fh)
with open(INVENTORY, encoding="utf-8") as fh:
    INV = yaml.safe_load(fh)

ENVIRONMENT = CLUSTER["cluster"]["environment"]
VIP = CLUSTER["proxysql"]["endpoint"]["address"]
VIP_PORT = CLUSTER["proxysql"]["endpoint"]["port"]
APP_USER = CLUSTER.get("proxysql", {}).get("app_user", "app_user")
HG_BASE = int(CLUSTER.get("proxysql", {}).get("hostgroup_base", 10))

PROXIES = list(INV["all"]["children"]["proxysql"]["hosts"].keys())
_app = (INV["all"]["children"].get("app") or {}).get("hosts") or {}
APP_HOST = next(iter(_app)) if _app else None

CNF_REMOTE = "/root/.workload.cnf"
SCRIPT_REMOTE = "/tmp/workload-numbered.sh"
LOG_REMOTE = "/tmp/workload-proxysql.log"


def parse_duration(text):
    """'2m'->120, '30s'->30, '1h'->3600, '90'->90. Bez cichego domyslu.

    Semantyka jak _duration_seconds w chaos-failover.py: nieparsowalne
    availability.rto_node_failure to brak kontraktu do sprawdzenia — domysl
    120s pozwolilby przejsc awarie 119s w klastrze deklarujacym RTO 30s,
    wiec odmawiamy (REFUSED, exit 1) zamiast mierzyc wzgledem zmyslonego SLA.
    """
    m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", str(text or ""))
    if not m:
        raise SystemExit(
            f"REFUSED: nie umiem sparsowac availability.rto_node_failure={text!r} "
            "(dozwolone formy: '90', '30s', '2m', '1h')"
        )
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


RTO = parse_duration((CLUSTER.get("availability") or {}).get("rto_node_failure"))


def sh(host, script, timeout=120, check=False):
    r = subprocess.run(
        [ANSIBLE, host, "-i", INVENTORY, "-m", "ansible.builtin.shell", "-a", script],
        capture_output=True, text=True, timeout=timeout)
    out = r.stdout
    m = re.search(rf'^{re.escape(host)}\s*\|\s*\w+\s*\|\s*rc=(\d+)\s*>>?\s*$', out, re.M)
    body = out[m.end():].strip() if m else (out + r.stderr).strip()
    rc = int(m.group(1)) if m else 1
    if check and rc != 0:
        raise RuntimeError(f"{host}: {body[:200]}")
    return rc, body


def vip_holder():
    """Ktory wezel ProxySQL ma teraz VIP na interfejsie (a nie: ktory powinien)."""
    for host in PROXIES:
        rc, out = sh(host, f"ip -4 -o addr show | grep -q '{VIP}/' && echo YES || echo NO")
        if rc == 0 and out.strip().endswith("YES"):
            return host
    return None


def hostgroups_present(host):
    """Czy wezel zna hostgroupy TEGO klastra (dowod, ze failover nie idzie w pustke)."""
    rc, out = sh(host,
                 "mariadb --defaults-extra-file=/etc/proxysql/admin-check.cnf "
                 f"-h127.0.0.1 -P6032 -uadmin -N -B -e \"SELECT COUNT(*) FROM "
                 f"runtime_mysql_servers WHERE hostgroup_id={HG_BASE}\" 2>/dev/null")
    try:
        return int(out.strip().splitlines()[-1]) > 0
    except (ValueError, IndexError):
        return False


def committed_rows():
    rc, out = sh(APP_HOST, f"cat {LOG_REMOTE} 2>/dev/null | wc -l")
    try:
        return int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def committed_seqs():
    rc, out = sh(APP_HOST, f"cat {LOG_REMOTE} 2>/dev/null")
    seqs, times = [], []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                times.append(float(parts[0]))
                seqs.append(int(parts[1]))
            except ValueError:
                pass
    return times, seqs


def pve(vmid, action):
    """Twardy stop/start VM przez API Proxmoksa.

    Biblioteka STANDARDOWA, nie `requests`: host kontrolny go nie ma i pierwszy
    przebieg trybu `node` wywalil sie na ImportError — na szczescie ZANIM cokolwiek
    zgasil, ale rownie dobrze mogl paść przy przywracaniu i zostawic wezel wylaczony.
    """
    import ssl
    import urllib.request

    tok = os.environ["PROXMOX_VE_API_TOKEN"]
    endpoint = os.environ.get("PROXMOX_VE_ENDPOINT", "https://192.168.1.181:8006").rstrip("/")
    url = f"{endpoint}/api2/json/nodes/pve/qemu/{vmid}/status/{action}"
    req = urllib.request.Request(
        url, data=b"", method="POST",
        headers={"Authorization": f"PVEAPIToken={tok}"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return resp.read().decode()


def main():
    if ENVIRONMENT == "production":
        print("REFUSED: chaos-proxysql-failover jest destrukcyjny (ISC-64)")
        return 1
    if not APP_PW:
        print("FAIL: brak APP_DB_PASSWORD")
        return 1
    if not APP_HOST:
        print("FAIL: inventory nie ma grupy 'app' — sonda mierzy z hosta aplikacyjnego")
        return 1
    if MODE not in ("service", "node", "worker"):
        print(f"FAIL: PROXYSQL_FAILOVER_MODE={MODE!r} (dozwolone: worker, service, node)")
        return 1
    if len(PROXIES) < 2:
        print(f"FAIL: failover wymaga >=2 wezlow ProxySQL, jest {len(PROXIES)}")
        return 1

    victim = vip_holder()
    if victim is None:
        print(f"FAIL: zaden wezel nie ma VIP {VIP} — nie ma czego przelaczac")
        return 1
    survivor = next(h for h in PROXIES if h != victim)
    print(f"VIP {VIP} trzyma {victim}; kandydat na przejecie: {survivor} (tryb {MODE})")

    # 4. WIELODZIERZAWNOSC — sprawdzana PRZED awaria. Po awarii nie odroznilibysmy
    #    "konfiguracja nigdy tam nie byla" od "failover ja zepsul".
    failures = []
    if not hostgroups_present(survivor):
        print(f"FAIL: {survivor} nie zna hostgroup {HG_BASE} tego klastra — "
              f"failover przelaczylby ruch w pustke. Uruchom f7 na obu wezlach.")
        return 1

    vmid = None
    if MODE == "node":
        mapping = dict(kv.split("=") for kv in
                       os.environ.get("PROXYSQL_VMIDS", "").split(",") if "=" in kv)
        if victim not in mapping:
            print(f"FAIL: tryb node wymaga VMID {victim} w PROXYSQL_VMIDS")
            return 1
        vmid = mapping[victim]

    subprocess.run([ANSIBLE, APP_HOST, "-i", INVENTORY, "-m", "copy",
                    "-a", f"src=tests/lab/workload-numbered.sh dest={SCRIPT_REMOTE} mode=0755"],
                   capture_output=True, text=True, check=True)
    sh(APP_HOST, f"printf '[client]\\nuser={APP_USER}\\npassword={APP_PW}\\n' > {CNF_REMOTE} "
                 f"&& chmod 0600 {CNF_REMOTE}", check=True)
    # TRUNCATE jest OBOWIAZKOWY, nie kosmetyczny. workload-numbered.sh numeruje
    # seq od 1, a `seq` to PRIMARY KEY — po wczesniejszym `lab-failover-test` na
    # tym samym klastrze tabela juz zawiera 1..N, wiec KAZDY insert wpada w
    # duplikat klucza. Skrypt tlumi stderr i loguje wylacznie rc=0, wiec objaw
    # jest cichy: proces zyje, log pusty. Zmierzone — pierwszy przebieg tej
    # sondy padl dokladnie tak, na bramie "workload nie zapisuje przed awaria".
    sh(APP_HOST,
       "mariadb --defaults-extra-file=" + CNF_REMOTE + f" --skip-ssl -h{VIP} -P{VIP_PORT} "
       "isa_test -e 'CREATE TABLE IF NOT EXISTS isa_failover "
       "(seq BIGINT PRIMARY KEY, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP); "
       "TRUNCATE isa_failover'", check=True)

    restored = False
    t_restore_marker = time.time()
    try:
        sh(APP_HOST, f"touch /tmp/workload.run; nohup bash {SCRIPT_REMOTE} {VIP} {VIP_PORT} "
                     f"{CNF_REMOTE} {LOG_REMOTE} >/tmp/workload-proxysql.out 2>&1 & echo start",
           check=True)
        time.sleep(6)
        before = committed_rows()
        if before == 0:
            print("FAIL: workload nie zapisuje JESZCZE PRZED awaria — test nic nie zmierzy")
            return 1

        # SESJA DLUGOZYJACA — odpowiedz na pytanie "czy polaczenia sa zrywane".
        # Workload otwiera nowe polaczenie na kazdy insert, wiec nie mowi NIC o
        # sesji juz nawiazanej. Tu trzymamy jedna otwarta przez cala awarie.
        # Zerwanie jest OCZEKIWANE i nie jest bledem: ProxySQL nie replikuje
        # stanu sesji miedzy wezlami, wiec gina one razem z wezlem. Sonda ma to
        # ZMIERZYC i nazwac, zeby nikt nie projektowal aplikacji w zalozeniu, ze
        # polaczenie przez VIP przezyje failover.
        sh(APP_HOST,
           f"nohup mariadb --defaults-extra-file={CNF_REMOTE} --skip-ssl -h{VIP} "
           f"-P{VIP_PORT} --connect-timeout=5 isa_test -e 'SELECT SLEEP(90)' "
           f">/tmp/longsession.out 2>&1 & echo held", check=True)
        time.sleep(3)

        t_kill = time.time()
        t_switch = 0.0
        if MODE == "worker":
            # Ginie WYLACZNIE proces obslugujacy ruch; angel (rodzic) zyje.
            # Dokumentacja (design-goals, watchdog): "The parent process (which
            # does not serve traffic) automatically restarts the child process
            # responsible for handling traffic", a FAQ mowi o restarcie "in less
            # than a second". To NAJCZESTSZY realny crash i jedyny, ktory ProxySQL
            # obsluguje SAM — wiec poprawne zachowanie to BRAK failoveru.
            rc_p, out_p = sh(victim,
                             "ps -eo pid,ppid,comm | awk '$3==\"proxysql\" && $2!=1 {print $1}'")
            child = out_p.strip().splitlines()[-1].strip() if out_p.strip() else ""
            if not child.isdigit():
                print(f"FAIL: nie znaleziono procesu potomnego proxysql na {victim} "
                      f"— brak angel process, tryb 'worker' nie ma czego zabic")
                return 1
            sh(victim, f"kill -9 {child}; echo killed", timeout=120)
        elif MODE == "service":
            # Zabija RODZICA I DZIECKO (`pgrep -x proxysql` zwraca oba PID-y), wiec
            # angel nie ma kogo wskrzesic. Jednostka ma Restart=no — zmierzone —
            # czyli nikt tego wezla nie podniesie. Keepalived zostaje zywy: to
            # celowe, bo sprawdzamy track_script, a nie VRRP.
            sh(victim, "pkill -9 -x proxysql; echo killed", timeout=120)
            rc_alive, alive = sh(victim, "pgrep -x proxysql >/dev/null && echo ALIVE || echo DEAD")
            if not alive.strip().endswith("DEAD"):
                print(f"FAIL: proxysql nadal zyje na {victim} — awaria nie zaistniala")
                return 1
        else:
            pve(vmid, "stop")

        if MODE == "worker":
            # ASERCJA ODWROCONA: tu failover bylby BLEDEM. Przelaczenie VIP przy
            # crashu, ktory ProxySQL naprawia sam w <1 s, to zbedne przerwanie
            # ruchu i ryzyko flappingu.
            deadline = time.time() + 30
            revived = False
            while time.time() < deadline:
                rc_c, out_c = sh(victim, "pgrep -x proxysql | wc -l")
                try:
                    if int(out_c.strip().splitlines()[-1]) >= 2:
                        revived = True
                        break
                except (ValueError, IndexError):
                    pass
                time.sleep(1)
            t_switch = time.time() - t_kill
            if not revived:
                failures.append(
                    f"angel process NIE wskrzesil workera na {victim} w 30s — "
                    f"dokumentacja obiecuje restart ponizej sekundy")
            rc_h, out_h = sh(victim,
                             f"ip -4 -o addr show | grep -q '{VIP}/' && echo YES || echo NO")
            if not out_h.strip().endswith("YES"):
                failures.append(
                    f"VIP UCIEKL z {victim} mimo ze crash workera jest naprawiany "
                    f"samoczynnie — zbedny failover, ryzyko flappingu przy kazdym "
                    f"restarcie procesu")
            else:
                print(f"worker wskrzeszony przez angel po {t_switch:.1f}s; "
                      f"VIP pozostal na {victim} (bez zbednego failoveru)")
        else:
            # 1. PRZELACZENIE w granicach RTO.
            #
            # Odpytujemy WYLACZNIE ocalalego. Pierwsza wersja iterowala po obu wezlach,
            # wiec w trybie `node` kazda iteracja czekala na timeout SSH do martwej
            # maszyny i zmierzyla 42,1 s przy przerwie w commitach 3,1 s. Te dwie
            # liczby sie wykluczaly i to bylo widac — czas przelaczenia byl artefaktem
            # sondy, nie zachowaniem VRRP.
            deadline = time.time() + RTO
            holder = None
            while time.time() < deadline:
                rc_v, out_v = sh(survivor,
                                 f"ip -4 -o addr show | grep -q '{VIP}/' && echo YES || echo NO")
                if rc_v == 0 and out_v.strip().endswith("YES"):
                    holder = survivor
                    break
                time.sleep(1)
            t_switch = time.time() - t_kill
            if holder != survivor:
                failures.append(
                    f"VIP NIE przeszedl na {survivor} w {RTO}s (trzyma: {holder}). "
                    f"W trybie 'service' oznacza to, ze vrrp_script chk_proxysql nie "
                    f"wprowadzil instancji w FAULT — VIP zostaje na wezle z martwym "
                    f"ProxySQL, czyli endpoint jest niedostepny bez zadnego przelaczenia."
                )
            else:
                print(f"VIP przejety przez {survivor} po {t_switch:.1f}s (RTO {RTO}s)")

        # 2. CIAGLOSC — klient musi sam wrocic do zapisywania.
        resumed, deadline = False, time.time() + 60
        mark = committed_rows()
        while time.time() < deadline:
            if committed_rows() > mark + 3:
                resumed = True
                break
            time.sleep(2)
        if not resumed:
            failures.append("aplikacja NIE wrocila do zapisywania po przejeciu VIP")

        # Werdykt sesji dlugozyjacej: raportowany, NIE karany.
        rc_s, out_s = sh(APP_HOST, "cat /tmp/longsession.out 2>/dev/null; "
                                   "pgrep -f 'SELECT SLEEP(90)' >/dev/null && echo STILL_OPEN || echo GONE")
        session_survived = "STILL_OPEN" in out_s
        session_err = next((ln.strip() for ln in out_s.splitlines()
                            if "ERROR" in ln or "Lost connection" in ln), "")
        sh(APP_HOST, "pkill -f 'SELECT SLEEP(90)' >/dev/null 2>&1; echo done", timeout=60)

    finally:
        # WORKLOAD CELOWO NADAL BIEGNIE. `preempt_delay` w keepalived.conf.j2
        # sprawia, ze wracajacy wezel o wyzszym priorytecie ODBIERA VIP z powrotem
        # — czyli awaria jednego wezla kosztuje aplikacje DWIE przerwy, nie jedna,
        # a ta druga zdarza sie w momencie wybranym przez operatora ("naprawilem
        # fcp1"). Zatrzymanie obciazenia przed przywroceniem ukryloby ja calkowicie.
        t_restore_marker = time.time()
        try:
            if MODE == "worker":
                # Nie ma czego przywracac: angel juz to zrobil, a VIP sie nie ruszyl.
                restored = True
            elif MODE == "service":
                sh(victim, "systemctl start proxysql", timeout=180)
                restored = True
            elif vmid:
                pve(vmid, "start")
                time.sleep(45)
                restored = True
        except Exception as exc:                                  # noqa: BLE001
            print(f"UWAGA: nie udalo sie przywrocic {victim}: {exc}")

        # W trybie `worker` VIP sie nie ruszyl, wiec nie ma powrotu do mierzenia.
        if restored and MODE != "worker":
            t_restore = time.time()
            # preempt_delay + margines na powrot uslugi i zbieznosc VRRP.
            deadline = time.time() + 90
            back = False
            while time.time() < deadline:
                rc_b, out_b = sh(victim,
                                 f"ip -4 -o addr show | grep -q '{VIP}/' && echo YES || echo NO")
                if rc_b == 0 and out_b.strip().endswith("YES"):
                    back = True
                    break
                time.sleep(2)
            print(f"powrot VIP na {victim}: "
                  + (f"tak, po {time.time() - t_restore:.0f}s od przywrocenia"
                     if back else "nie w ciagu 90s (VIP zostal na ocalalym)"))
            time.sleep(5)

        sh(APP_HOST, "rm -f /tmp/workload.run", timeout=60)
        time.sleep(2)

    # 3. BRAK UTRATY POTWIERDZONYCH TRANSAKCJI.
    times, seqs = committed_seqs()
    # Przerwy liczone OSOBNO dla obu zdarzen: awarii i powrotu VIP. Jedna wspolna
    # wartosc "max gap" pokazywalaby tylko wieksza z nich i milczaco chowala fakt,
    # ze aplikacja placi dwa razy.
    gaps_fail = [b - a for a, b in zip(times, times[1:]) if b <= t_restore_marker]
    gaps_back = [b - a for a, b in zip(times, times[1:]) if b > t_restore_marker]
    gap = max(gaps_fail, default=0.0)
    gap_back = max(gaps_back, default=0.0)
    if seqs:
        rc, out = sh(APP_HOST,
                     f"mariadb --defaults-extra-file={CNF_REMOTE} --skip-ssl -h{VIP} "
                     f"-P{VIP_PORT} -N -B isa_test -e "
                     f"'SELECT COUNT(*) FROM isa_failover WHERE seq IN ({','.join(map(str, seqs))})'")
        try:
            present = int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            present = -1
        if present != len(seqs):
            failures.append(
                f"UTRATA DANYCH: klient potwierdzil {len(seqs)} transakcji, "
                f"po przelaczeniu jest {present}")
    else:
        # Pusty log po awarii to nie dowod "nic nie utracono", tylko brak dowodu
        # w ogole — asercja pkt 3 jest wtedy niewykonalna. Wczesniejsze pominiecie
        # tej galezi wypelniloby raport PASS-em przy zerowych danych pomiarowych.
        failures.append(
            "brak danych workload po awarii - asercja integralnosci niewykonalna")
    sh(APP_HOST, f"rm -f {CNF_REMOTE}", timeout=60)

    if failures:
        print("FAIL: failover endpointu ProxySQL naruszony:")
        for f in failures:
            print(f"  - {f}")
        return 1

    if MODE == "worker":
        print(
            f"PASS: crash workera na {victim} — angel wskrzesil go w {t_switch:.1f}s, "
            f"VIP NIE ruszyl sie z miejsca (brak zbednego failoveru), "
            f"{len(seqs)}/{len(seqs)} potwierdzonych transakcji obecnych (0 utraconych), "
            f"najdluzsza przerwa w commitach {gap:.1f}s; sesja dlugozyjaca "
            + ("PRZETRWALA" if session_survived else "ZERWANA (ginie z procesem, "
               "mimo ze wezel zostaje aktywny)")
        )
        return 0

    print(
        f"PASS: {victim} padl ({MODE}), VIP przejal {survivor} w {t_switch:.1f}s "
        f"(< {RTO}s RTO); aplikacja wznowila zapisy sama, {len(seqs)}/{len(seqs)} "
        f"potwierdzonych transakcji obecnych (0 utraconych), najdluzsza przerwa "
        f"w commitach {gap:.1f}s (przy powrocie VIP {gap_back:.1f}s); sesja dlugozyjaca "
        + ("PRZETRWALA" if session_survived
           else f"ZERWANA{' (' + session_err[:60] + ')' if session_err else ''} — "
                f"aplikacja MUSI umiec sie przelaczyc")
        + f"; {victim} przywrocony={restored}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
