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

Wzorzec zweryfikowany: trzy węzły Rocky 9.8 z czystego obrazu cloud. Klonowanie
działającej maszyny bazodanowej jest ZABRONIONE — klon dziedziczy `grastate.dat`,
certyfikaty węzła i konto SST, więc po starcie `mariadb` potrafi cicho dołączyć
do klastra źródłowego jako nadmiarowy węzeł.

```bash
# Zmienne jak dla providera terraform: PROXMOX_VE_ENDPOINT + PROXMOX_VE_API_TOKEN
EP="${PROXMOX_VE_ENDPOINT%/}"; H="Authorization: PVEAPIToken=$PROXMOX_VE_API_TOKEN"

# 1. Utworzenie. `sshkeys` musi byc zakodowane URI (PVE odrzuca surowy klucz),
#    a `size=` przy `import-from` jest IGNOROWANE — dysk dostaje rozmiar obrazu.
curl -sk -H "$H" -X POST "$EP/api2/json/nodes/$NODE/qemu" \
  --data-urlencode "vmid=9780" --data-urlencode "name=nv1" \
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

# 2. Rozmiar dysku — osobnym wywolaniem, inaczej zostanie rozmiar obrazu (10G).
curl -sk -H "$H" -X PUT "$EP/api2/json/nodes/$NODE/qemu/9780/resize" \
  --data-urlencode "disk=virtio0" --data-urlencode "size=40G"

# 3. Start. Import dysku trzyma blokade pliku konfiguracji: start tuz po
#    utworzeniu konczy sie `can't lock file ... got timeout`. Ponow z odstepem.
curl -sk -H "$H" -X POST "$EP/api2/json/nodes/$NODE/qemu/9780/status/start"
```

Dalej normalna ścieżka z README: `cluster-trust-hosts`, PKI, `cluster-validate`,
`cluster-build`.

## Zniszczenie maszyn

```bash
# 1. NAJPIERW wyrejestruj najemce z warstwy wspolnej — inaczej w ProxySQL
#    zostana hostgroupy wskazujace na nieistniejace adresy, a w PMM martwe uslugi.
make cluster-deregister CLUSTER=<nazwa> CONFIRM=yes

# 2. Maszyny (nie ma stanu Terraforma, wiec wprost przez API).
for id in 9780 9781 9782; do
  curl -sk -H "$H" -X POST "$EP/api2/json/nodes/$NODE/qemu/$id/status/stop"
  curl -sk -H "$H" -X DELETE "$EP/api2/json/nodes/$NODE/qemu/$id?purge=1&destroy-unreferenced-disks=1"
done

# 3. Sieroty ZFS. `purge=1` zwykle wystarcza, ale wolumen `-cloudinit` potrafi
#    zostac i wtedy PONOWNE uzycie tego VMID pada na `dataset already exists`.
curl -sk -H "$H" "$EP/api2/json/nodes/$NODE/storage/local-zfs/content" \
  | python3 -c "import json,sys;[print(c['volid']) for c in json.load(sys.stdin)['data'] if any(f'vm-{i}-' in c['volid'] for i in (9780,9781,9782))]"
```

## Artefakty w repozytorium

Zniszczenie maszyn nie usuwa definicji. Po teardownie skasuj ręcznie, inaczej
kolejny najemca zderzy się z zajętą hostgroupą albo nazwą:

- `clusters/<nazwa>/` — definicja, inwentarz i `known_hosts`,
- `pki/<skrot>/` — CA i certyfikaty węzłów,
- wpis w `clusters/reserved-addresses.yml`, jeśli adresy były tam rejestrowane.

Sondy `tests/validation/probe-address-collision.py` i `probe-proxysql-tenancy.py`
wykryją pozostawione definicje kolidujące z nowym najemcą.
