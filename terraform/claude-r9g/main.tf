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

# claude-r9g — MALY klaster weryfikacyjny EL9: TYLKO warstwa Galera.
#
# Powstal, zeby udowodnic przenosnosc galera-backup na Rocky 9 obok dzialajacego
# EL10 (claude-r10b). Swiadomie NIE tworzy ProxySQL, restore ani infra —
# clusters/claude-r9g/inventory.yml wskazuje na juz dzialajace wezly klastra
# claude-r10b (.44/.45 ProxySQL, .46 restore, .47 PMM+MinIO).
#
# OSOBNY katalog i OSOBNY stan: terraform/claude-pve/ (pelny EL9, .10-.16) oraz
# terraform/claude-r10b/ (zywy EL10) pozostaja nietkniete.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "data1"
  bridge    = "vmbr0"

  # Ten sam obraz EL9, ktorego uzywa terraform/claude-pve/ — musi byc wczesniej
  # zaimportowany na PVE do `local:import/`.
  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Adresacja .17-.19 + .39: rozlaczna z claude-pve (.10-.16), claude-r10
  # (.31-.37 + VIP .40) i claude-r10b (.41-.47 + .51-.53). .20-.29 oraz .38
  # zajmuja VM poza tym repozytorium.
  #
  # 2560 MB/wezel — to proof przenosnosci OS, nie test wydajnosci. Przydzielone
  # 2048 MB daje tylko ~1771 MB widzianych przez system (rezerwacja kernela i
  # firmware), co odbija sie od bramki f2_preflight `ansible_memtotal_mb >= 2048`.
  # 2560 MB przechodzi guard bez jego oslabiania. Buffer pool zostaje 512M,
  # gcache 256M — patrz clusters/claude-r9g/cluster.yml.
  #
  # r9node1 MUSI byc na EL9. f10_restore.yml instaluje pakiety przypiete z
  # lockfile'a klastra, czyli MariaDB-server-11.4.12-1.el9 — na hoscie Rocky 10
  # takiego RPM-a nie ma w repo i restore konczy sie bledem "No package
  # ... available". Host restore nie moze wiec byc dzielony miedzy rodziny OS.
  vms = {
    g9node1 = { id = 9150, ip = 17, role = "galera", cpu = 2, ram = 2560, disk = 40 }
    g9node2 = { id = 9151, ip = 18, role = "galera", cpu = 2, ram = 2560, disk = 40 }
    g9node3 = { id = 9152, ip = 19, role = "galera", cpu = 2, ram = 2560, disk = 40 }
    r9node1 = { id = 9153, ip = 39, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = "r9g-${each.key}"
  node_name   = local.node_name
  vm_id       = each.value.id
  pool_id     = local.pool_id
  tags        = ["claude", "rocky9", "r9g", each.value.role]
  description = "ISA cluster Rocky 9 galera-only (${each.value.role}) — prefix r9g-, VMID ${each.value.id}"

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

  disk {
    datastore_id = local.storage
    interface    = "virtio0"
    import_from  = local.source_img
    size         = each.value.disk
    discard      = "on"
  }

  initialization {
    interface    = "scsi1"
    datastore_id = local.storage
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

# Endpoint i monitoring sa DZIEDZICZONE po claude-r10b — ten katalog ich nie tworzy.
output "shared_endpoint" { value = "192.168.1.50:6033 (ProxySQL VIP klastra claude-r10b)" }
output "shared_pmm_url" { value = "https://192.168.1.47" }
