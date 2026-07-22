# F0 Discovery — raport

**Status: NIEURUCHOMIONY** — brak dostępu do testowych hostów (BLK-1, BLK-2).

Ten raport zostanie wygenerowany przez `playbooks/f0_discovery.yml` po uzyskaniu dostępu do testowych hostów Rocky Linux 9.

## Zbierane fakty (zgodnie z MASTER_PROMPT §9)

- [x] Facts Ansible (OS, kernel, CPU, RAM, mounts)
- [x] Wersje OS/kernel (`/etc/rocky-release`)
- [x] CPU/RAM/NUMA (`numactl --hardware`)
- [x] Dyski, filesystem, mount options, wolne miejsce (`lsblk`, `findmnt`)
- [ ] IOPS i fsync latency (`fio`) — uruchamiane TYLKO na hostach `[discovery_bench]` z `allow_bench: true`
- [x] DNS, routing, osiągalność portów (`resolv.conf`, `ip route`, `ss -tlnp`)
- [x] chrony/NTP (`chronyc tracking`)
- [x] SELinux i firewalld (`getenforce`, `firewall-cmd --list-all`)
- [x] Repozytoria i dostępne wersje pakietów (`dnf repolist`)
- [x] Istniejące usługi MariaDB/ProxySQL (`systemctl`, `rpm -qa`)
- [x] Istniejący monitoring i log shipping (`ps`, `systemctl`)
- [ ] Dostępny secret backend — **wymaga F0 + decyzji principal**
- [x] Audyt PK w `information_schema` (jeśli istnieje MariaDB)
- [ ] Tempo zapisów i przyrost danych — **wymaga reprezentatywnego workloadu**

## Oczekiwany wynik F0

- `versions/discovered-versions.json` — wypełnione wersje z hostów
- `/var/tmp/f0-discovery-<host>.json` per host — pełne fakty
- `gcache.size` wyliczony z mierzonego write rate × okno IST (30 min)
- Lista blockerów zaktualizowana w ISA.md

## Wymagany dostęp (BLK)

- **BLK-1**: ≥3 testowe VM Rocky Linux 9 (Galera) + ≥2 VM (ProxySQL), laboratory/staging
- **BLK-2**: SSH + privilege escalation (sudo) do powyższych hostów
- **BLK-3**: Ustalenie secret backendu i backup backendu (SMB mount / S3)
- **BLK-4**: Internet do oficjalnych źródeł (MariaDB/Galera/ProxySQL/Rocky docs) — dla F1
