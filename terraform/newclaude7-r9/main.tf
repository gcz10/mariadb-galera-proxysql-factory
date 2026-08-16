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

# newclaude7-r9 — warstwa BAZODANOWA siodmego przebiegu budowy od zera.
#
# Cel: pierwszy klaster, na ktorym naprawa z PR #29 dziala BEZ recznej pomocy.
# Na v6 task usuwajacy anonimowe konta cieniujace pmm_monitor wykonal sie jako
# no-op, bo debugging tamtej sesji wyczyscil je wczesniej recznie. Tu F11 ma
# przejsc za pierwszym razem na nietknietym mysql.user.
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
  # VMID 9470-9473 wolne (sprawdzone przez API na pelnej liscie VM klastra PVE).
  # IP .160-.163 potwierdzone AKTYWNYM skanem sieci (ping + port 22/8006),
  # nie sama konfiguracja Proxmoxa — ta pomija hypervisor i hosty spoza niego.
  #
  # NIE UZYWAC .180-.183: `.181` to adres zarzadzania hypervisora Proxmox
  # (PROXMOX_VE_ENDPOINT). Zywe sa tez .179 i .184. Kolizje z tymi blokami
  # lowi tests/validation/probe-address-collision.py w CI.
  #
  # RAM: limit operatora to 5 GB na VM; wezly Galera dostaja 3 GB, a
  # innodb_buffer_pool_size zjezdza do 768M, zeby zostal zapas na mariabackup
  # podczas SST i backupu.
  vms = {
    n7g1 = { id = 9470, ip = 160, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n7g2 = { id = 9471, ip = 161, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n7g3 = { id = 9472, ip = 162, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n7r1 = { id = 9473, ip = 163, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = each.key
  node_name   = local.node_name
  pool_id     = local.pool_id
  vm_id       = each.value.id
  description = "newclaude7-r9 Rocky 9 (${each.value.role}) — VMID ${each.value.id}"
  tags        = ["rocky9", "galera", "newclaude7", each.value.role, "n7"]

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
