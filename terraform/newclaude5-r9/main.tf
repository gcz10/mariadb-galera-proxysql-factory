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

# newclaude5-r9 — warstwa BAZODANOWA piatego przebiegu budowy od zera.
#
# Weryfikuje stos po aktualizacjach z tej sesji: ProxySQL 3.0.10 (PR #26),
# PMM 3.9.0 (PR #23), sciezka deprovisioningu cluster-deregister (PR #25).
#
# Rozny od `newclaude4-r9` proceduralnie: przebieg idzie dokladnie w kolejnosci
# z README.md:32-38 (F11 przed F6, lab-seed-smoke przed drillem, weryfikacja
# monitoringu na samym koncu). Poprzednia runda zlamala wszystkie trzy zaleznosci.
#
# ProxySQL, VIP, PMM i MinIO sa wspoldzielone i mieszkaja w terraform/shared/ —
# ten katalog ich nie tworzy ani nie niszczy.
#
# Rocky 9 GenericCloud NIE wymaga snippetu cloud-init: w przeciwienstwie do EL10
# nie ma `disable_root: true`, wiec klucz z `user_account` trafia prosto do roota.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"
  # VMID 9450-9453 wolne. IP .170-.173 zwolnione po `newclaude4-r9`.
  #
  # NIE UZYWAC .180-.183: `.181` to adres zarzadzania hypervisora Proxmox
  # (PROXMOX_VE_ENDPOINT). Przypisanie go VM tworzy konflikt ARP, ktory zrywa
  # wywolania API w trakcie `terraform apply` (objaw: "failed to perform HTTP
  # POST request"). Wolnosc adresow potwierdzona AKTYWNYM skanem sieci
  # (ping + port 22/8006), nie sama konfiguracja Proxmoxa — ta pomija
  # hypervisor i hosty spoza niego (wykryto tez zywe .184 i .190).
  #
  # RAM: limit operatora to 5 GB na VM; wezly Galera dostaja 3 GB, a
  # innodb_buffer_pool_size zjezdza do 768M, zeby zostal zapas na mariabackup
  # podczas SST i backupu.
  vms = {
    n5g1 = { id = 9450, ip = 170, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n5g2 = { id = 9451, ip = 171, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n5g3 = { id = 9452, ip = 172, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n5r1 = { id = 9453, ip = 173, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = each.key
  node_name   = local.node_name
  pool_id     = local.pool_id
  vm_id       = each.value.id
  description = "newclaude5-r9 Rocky 9 (${each.value.role}) — VMID ${each.value.id}"
  tags        = ["rocky9", "galera", "newclaude5", each.value.role, "n5"]

  agent {
    enabled = true
    timeout = "15m"
  }

  stop_on_destroy                      = true
  delete_unreferenced_disks_on_destroy = true
  purge_on_destroy                     = true

  cpu {
    cores   = each.value.cpu
    type    = "host"
    sockets = 1
  }

  memory {
    dedicated = each.value.ram
  }

  disk {
    datastore_id = local.storage
    import_from  = local.source_img
    interface    = "virtio0"
    file_format  = "raw"
    size         = each.value.disk
    discard      = "on"
    aio          = "io_uring"
  }

  initialization {
    datastore_id = local.storage
    ip_config {
      ipv4 {
        address = "192.168.1.${each.value.ip}/24"
        gateway = local.gateway
      }
    }
    user_account {
      username = "root"
      keys     = [local.ssh_pubkey]
    }
    dns {
      servers = ["1.1.1.1", "8.8.8.8"]
    }
  }

  network_device {
    bridge = local.bridge
  }
}

output "vms" {
  value = {
    for k, v in local.vms : k => {
      vmid    = v.id
      ip      = "192.168.1.${v.ip}"
      role    = v.role
      ram_mb  = v.ram
      cpu     = v.cpu
      disk_gb = v.disk
    }
  }
}

output "shared_vip" {
  value = "192.168.1.133 (terraform/shared/)"
}
