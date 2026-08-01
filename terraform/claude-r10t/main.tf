terraform {
  required_version = ">= 1.5"
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "0.111.1"
    }
  }
}

provider "proxmox" {} # endpoint + api_token + insecure z env PROXMOX_VE_*

# claude-r10t — klaster weryfikacyjny Rocky 10 z tls.mode=full.
#
# Powstal, zeby po raz pierwszy wykonac sciezke TLS `full` na zywym klastrze:
# szyfrowana replikacja wsrep (socket.ssl_*), szyfrowany SST oraz ISC-44
# (odrzucenie niezaufanego certyfikatu), ktory w ADR-002 pozostaje otwarty.
#
# Klaster jest SAMOWYSTARCZALNY w warstwie bazy: 3 wezly Galera + wlasny wezel
# restore. ProxySQL i PMM nie sa wdrazane — wlaczenie TLS na wspoldzielonym
# ProxySQL klastra claude-r10b przekonfigurowaloby zywy endpoint.
#
# OSOBNY katalog i OSOBNY stan: claude-r10b (.41-.47/.51-.53) i claude-r9g
# (.17-.19/.39) pozostaja nietkniete.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "data1"
  bridge    = "vmbr0"

  # Ten sam obraz EL10, ktorego uzywaja terraform/claude-r10 i claude-r10b.
  source_img = "local:import/Rocky-10.2-GenericCloud.qcow2"

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Adresacja .54-.57: rozlaczna z kazdym istniejacym klastrem w repo oraz z VM
  # poza repozytorium (.20-.29, .38, .90-.92, .100-.102). SAN certyfikatu w
  # tests/lab/tls/server-cert.pem pokrywa dokladnie te cztery adresy plus
  # nazwy hostow — zmiana adresacji wymaga regeneracji certyfikatu.
  #
  # 2560 MB/wezel: przydzielone 2048 MB daje ~1771 MB widzianych przez system,
  # co odbija sie od bramki f2_preflight `ansible_memtotal_mb >= 2048`.
  vms = {
    gtnode1 = { id = 9160, ip = 54, role = "galera", cpu = 2, ram = 2560, disk = 40 }
    gtnode2 = { id = 9161, ip = 55, role = "galera", cpu = 2, ram = 2560, disk = 40 }
    gtnode3 = { id = 9162, ip = 56, role = "galera", cpu = 2, ram = 2560, disk = 40 }
    rtnode1 = { id = 9163, ip = 57, role = "restore", cpu = 1, ram = 2560, disk = 40 }
    # Wlasna para ProxySQL: f7_proxysql.yml przy tls.mode=full rozprowadza CA na
    # te wezly i przestawia mysql_servers.use_ssl=1. Uzycie wspoldzielonych
    # pnode1/pnode2 klastra claude-r10b przekonfigurowaloby zywy endpoint.
    # VIP Keepalived to .60 — NIE .50, ktore nalezy do claude-r10b.
    ptnode1 = { id = 9164, ip = 58, role = "proxysql", cpu = 1, ram = 2560, disk = 40, store = "local-zfs" }
    ptnode2 = { id = 9165, ip = 59, role = "proxysql", cpu = 1, ram = 2560, disk = 40, store = "local-zfs" }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = "r10t-${each.key}"
  node_name   = local.node_name
  vm_id       = each.value.id
  pool_id     = local.pool_id
  tags        = ["claude", "rocky10", "r10t", "tls", each.value.role]
  description = "ISA cluster Rocky 10 TLS full (${each.value.role}) — prefix r10t-, VMID ${each.value.id}"

  # F2 instaluje i wlacza qemu-guest-agent; provider nie czeka na raport IP,
  # bo adresy sa statyczne, a agent moze wystartowac dopiero po restarcie VM.
  agent {
    enabled = true
    type    = "virtio"

    wait_for_ip {
      disabled = true
    }
  }
  stop_on_destroy = true
  operating_system { type = "l26" }

  cpu {
    type  = "host"
    cores = each.value.cpu
  }
  memory { dedicated = each.value.ram }

  # `store` per VM nadpisuje domyslna pule. Powod: data1 jest zapelnione
  # (904/922 GiB, ~19 GiB wolnego), wiec kolejne 40 GB zvole sie tam nie mieszcza,
  # a dopychanie puli do pelna zagraza VM-om, ktore juz na niej stoja — w tym
  # wezlom Galera tego klastra. Wezly ProxySQL ida na local-zfs (404 GiB wolne).
  disk {
    datastore_id = try(each.value.store, local.storage)
    interface    = "virtio0"
    import_from  = local.source_img
    size         = each.value.disk
    discard      = "on"
  }

  initialization {
    interface    = "scsi1"
    datastore_id = try(each.value.store, local.storage)
    # Klucz ed25519 idzie do roota — logowanie SSH bezposrednio jako root.
    user_account {
      username = "root"
      keys     = [local.ssh_pubkey]
    }
    ip_config {
      ipv4 {
        address = "192.168.1.${each.value.ip}/24"
        gateway = local.gateway
      }
    }
    dns { servers = ["1.1.1.1", "8.8.8.8"] }
  }

  network_device { bridge = local.bridge }
}

output "vms" {
  value = { for k, v in local.vms : k => {
    vmid = v.id, ip = "192.168.1.${v.ip}", role = v.role
    cpu  = v.cpu, ram_mb = v.ram, disk_gb = v.disk
  } }
}

output "tls_mode" { value = "full — certyfikaty z tests/lab/tls/ (gitignored)" }
