# Runbook: Maszyny spoza Terraforma

**Status:** Aktualny (zweryfikowany na najemcy `nova-r9`, 2026-08-25)
**Powiązane:** README „Własne hosty, krok po kroku"

## Przeznaczenie

Fabryka nie zna źródła maszyn: buduje na wszystkim, co ma SSH, systemd i Rocky
Linux 9 albo 10. Ten runbook pokazuje pełny cykl dla maszyn utworzonych **poza**
Terraformem i nazywa granicę własności, która z tego wynika.

## Granica własności — przeczytaj przed startem

`make galera-rebuild` i `terraform/pve-teardown.sh` wymagają katalogu
`terraform/<nazwa>` i jego stanu. Maszyny utworzone inaczej **nie są przez nie
widziane** i cele niszczące odmówią pracy.

To jest świadoma symetria, nie brak: skoro fabryka nie tworzy Twoich maszyn, nie
bierze też odpowiedzialności za ich kasowanie. Cykl życia maszyny należy do tego,
kto ją stworzył — Terraform, `virt-install`, konsola chmury albo procedura niżej.

Fabryka za to **posprząta po sobie logicznie**: `make cluster-deregister`
usuwa najemcę z ProxySQL i PMM niezależnie od pochodzenia maszyn.

## Utworzenie maszyn — REST API Proxmoxa, bez Terraforma

> **Automatyzacja:** Całą opisaną poniżej procedurę (pre-flight check wolumenów ZFS,
> `POST /qemu`, asynchroniczne czekanie na task, resize dysku do 40G, start i weryfikację SSH)
> realizuje gotowe narzędzie w repozytorium:
> ```bash
> ./tools/pve-create-vm.sh --vmid <ID> --name <NAZWA> --ip <IP> --cluster <KLASTER> [--role galera|restore]
> ```
> Poniższe kroki opisują działanie pod maską i służą do weryfikacji lub ręcznego wykonania.

Wzorzec zweryfikowany: trzy węzły Rocky 9.8 z czystego obrazu cloud. Klonowanie
działającej maszyny bazodanowej jest ZABRONIONE — klon dziedziczy `grastate.dat`,
certyfikaty węzła i konto SST, więc po starcie `mariadb` potrafi cicho dołączyć
do klastra źródłowego jako nadmiarowy węzeł.

```bash
# Zmienne jak dla providera terraform: PROXMOX_VE_ENDPOINT + PROXMOX_VE_API_TOKEN
EP="${PROXMOX_VE_ENDPOINT%/}"; H="Authorization: PVEAPIToken=$PROXMOX_VE_API_TOKEN"

# 0. WOLNY VMID TO NIE TO SAMO CO WOLNY MAGAZYN. Lista maszyn i pule nie widza
#    wolumenow po nieudanym albo przerwanym tworzeniu: zostaje wtedy sam
#    `vm-<id>-cloudinit` (4 MB), bez maszyny i bez wpisu w puli. Kolejny `POST`
#    na ten VMID pada na `zfs error: dataset already exists`, a Terraform
#    zatrzymuje sie w polowie apply. Zlapane 2026-08-25 na VMID 9861.
#    Sprawdz OBA zrodla, zanim wybierzesz numer:
for id in 9780 9781 9782; do
  used_vm=$(curl -sk -H "$H" "$EP/api2/json/nodes/$NODE/qemu" \
    | python3 -c "import json,sys; print(any(int(v['vmid'])==$id for v in json.load(sys.stdin)['data']))")
  used_vol=$(curl -sk -H "$H" "$EP/api2/json/nodes/$NODE/storage/local-zfs/content" \
    | python3 -c "import json,sys; print([c['volid'] for c in json.load(sys.stdin)['data'] if str(c.get('vmid'))=='$id'])")
  echo "$id: maszyna=$used_vm wolumeny=$used_vol"
done
#    Oba musza byc puste. Wolumen bez maszyny kasuj DOPIERO po sprawdzeniu, ze
#    nie odwoluje sie do niego zadna konfiguracja VM ani zaden wpis w puli —
#    procedura jak przy sierotach w sekcji o niszczeniu.

# 1. Utworzenie. `sshkeys` musi byc zakodowane URI (PVE odrzuca surowy klucz),
#    a `size=` przy `import-from` jest IGNOROWANE — dysk dostaje rozmiar obrazu.
#    `pool` NIE jest ozdoba. Terraform ustawia je przez `pool_id`
#    (terraform/modules/pve_vm_set/main.tf:29) i pula jest JEDYNYM markerem
#    wlasnosci na wspoldzielonym hyperwizorze. Do 2026-08-25 ten runbook o tym
#    milczal i szesc recznie utworzonych maszyn wypadlo poza `claude-isa`.
POOL=claude-isa
curl -sk -H "$H" -X POST "$EP/api2/json/nodes/$NODE/qemu" \
  --data-urlencode "vmid=9780" --data-urlencode "name=nv1" \
  --data-urlencode "pool=$POOL" \
  --data-urlencode "cores=2" --data-urlencode "cpu=host" \
  --data-urlencode "memory=3072" --data-urlencode "balloon=0" \
  --data-urlencode "agent=enabled=1,type=virtio" \
  --data-urlencode "virtio0=local-zfs:0,import-from=local:import/Rocky-9.8-GenericCloud.qcow2,aio=io_uring,discard=on" \
  --data-urlencode "ide2=local-zfs:cloudinit" \
  --data-urlencode "net0=virtio,bridge=vmbr0,firewall=0" \
  --data-urlencode "boot=order=virtio0;net0" --data-urlencode "ciuser=root" \
  --data-urlencode "sshkeys=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(open(sys.argv[1]).read(),safe=""))' secrets/ssh_key.pub)" \
  --data-urlencode "ipconfig0=gw=192.168.1.1,ip=192.168.1.36/24" \
  --data-urlencode "nameserver=1.1.1.1 8.8.8.8"

# 2. POCZEKAJ, az tworzenie sie SKONCZY. `POST /qemu` zwraca UPID natychmiast,
#    a import obrazu trwa i trzyma blokade pliku konfiguracji. Kazde kolejne
#    wywolanie wysłane w tym czasie tez zwraca UPID — i cicho pada w tle na
#    `can't lock file ... got timeout`. Sam sie na to nabralem 2026-08-25:
#    resize i start "przeszly", a maszyny stały puste bez dysku i nazwy.
wait_task() {  # $1 = UPID
  while [ "$(curl -sk -H "$H" "$EP/api2/json/nodes/$NODE/tasks/$1/status" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["status"])')" != "stopped" ]; do
    sleep 5
  done
  curl -sk -H "$H" "$EP/api2/json/nodes/$NODE/tasks/$1/status" \
    | python3 -c 'import json,sys; print("exit:", json.load(sys.stdin)["data"].get("exitstatus"))'
}
wait_task "$CREATE_UPID"   # UPID zwrocony przez POST /qemu

# 3. Rozmiar dysku — osobnym wywolaniem, inaczej zostanie rozmiar obrazu (10G).
#    Tez zwraca UPID: sprawdz jego wynik, nie samo przyjecie zlecenia.
curl -sk -H "$H" -X PUT "$EP/api2/json/nodes/$NODE/qemu/9780/resize" \
  --data-urlencode "disk=virtio0" --data-urlencode "size=40G"

# 4. Start.
curl -sk -H "$H" -X POST "$EP/api2/json/nodes/$NODE/qemu/9780/status/start"
```

Dalej normalna ścieżka z README: `cluster-trust-hosts`, PKI, `cluster-validate`,
`cluster-build`.

## Zniszczenie maszyn

```bash
# 1. NAJPIERW wyrejestruj najemce z warstwy wspolnej — inaczej w ProxySQL
#    zostana hostgroupy wskazujace na nieistniejace adresy, a w PMM martwe uslugi.
CLUSTER=nazwa-najemcy
make cluster-deregister CLUSTER="$CLUSTER" CONFIRM=yes

# 2. POTWIERDZ TOZSAMOSC, zanim cokolwiek skasujesz. VMID to trzy cyfry roznicy
#    od cudzego, zywego wezla, a `DELETE` nie pyta o zdanie. Ta petla NIE kasuje:
#    wypisuje nazwe, adres i pule kazdego VMID.
#
#    Pula ma TRZY stany, nie dwa. `claude-isa` to nasze. CUDZA pula to STOP.
#    BRAK puli NIE dowodzi niczego: maszyny tworzone recznie przed 2026-08-25
#    nie dostawaly `pool`, wiec legacy wyglada jak cudze. Wtedy rozstrzyga
#    zgodnosc nazwy i adresu z inwentarzem najemcy, ktory wlasnie kasujesz.
export VMIDS="9780 9781 9782"
members=$(curl -sk -H "$H" "$EP/api2/json/pools/claude-isa" \
  | python3 -c "import json,sys; print(' '.join(str(m['vmid']) for m in json.load(sys.stdin)['data']['members'] if m.get('type')=='qemu'))")
for id in $VMIDS; do
  case " $members " in
    *" $id "*) owner="claude-isa" ;;
    *)         owner="BRAK PULY - rozstrzygnij nazwa i adresem" ;;
  esac
  curl -sk -H "$H" "$EP/api2/json/nodes/$NODE/qemu/$id/config" \
    | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print('$id', d.get('name'), d.get('ipconfig0'), '| pula: $owner')"
done
grep -E 'ansible_host' "clusters/$CLUSTER/inventory.yml"

# 3. Dopiero gdy obie listy sie zgadzaja — kasowanie.
for id in $VMIDS; do
  curl -sk -H "$H" -X POST "$EP/api2/json/nodes/$NODE/qemu/$id/status/stop"
  curl -sk -H "$H" -X DELETE "$EP/api2/json/nodes/$NODE/qemu/$id?purge=1&destroy-unreferenced-disks=1"
done

# 4. Sieroty ZFS. `purge=1` zwykle wystarcza, ale wolumen `-cloudinit` potrafi
#    zostac i wtedy PONOWNE uzycie tego VMID (takze przez Terraform) pada na
#    `dataset already exists`. Sprawdz i skasuj recznie, jesli cos zostalo.
curl -sk -H "$H" "$EP/api2/json/nodes/$NODE/storage/local-zfs/content" \
  | python3 -c "import json,sys,os;[print(c['volid']) for c in json.load(sys.stdin)['data'] if any(f'vm-{i}-' in c['volid'] for i in os.environ['VMIDS'].split())]"
```

## Artefakty w repozytorium

Zniszczenie maszyn nie usuwa definicji. Po teardownie skasuj ręcznie, inaczej
kolejny najemca zderzy się z zajętą hostgroupą albo nazwą:

- `clusters/<nazwa>/` — definicja, inwentarz i `known_hosts`,
- `pki/<skrot>/` — CA i certyfikaty węzłów,
- wpis w `clusters/reserved-addresses.yml`, jeśli adresy były tam rejestrowane.

Sondy `tests/validation/probe-address-collision.py` i `probe-proxysql-tenancy.py`
wykryją pozostawione definicje kolidujące z nowym najemcą.
