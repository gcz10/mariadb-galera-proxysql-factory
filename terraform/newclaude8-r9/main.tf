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

# newclaude8-r9 — warstwa BAZODANOWA osmego przebiegu budowy od zera.
#
# Cel: naturalny dowod poprawki z PR #30. Marker DROPPED w F6/F11 byl dotad
# sprawdzony wylacznie wstrzyknietym anonimem na utwardzonym juz klastrze;
# tutaj F11 spotyka swieza instalacje MariaDB i musi zaraportowac `changed`
# sam z siebie, przy pierwszym przebiegu.
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
  # VMID 9480-9483 wolne (sprawdzone przez API na pelnej liscie VM klastra PVE).
  # IP .164-.167 potwierdzone AKTYWNYM skanem sieci (ping + port 22/8006),
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
    n8g1 = { id = 9480, ip = 164, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n8g2 = { id = 9481, ip = 165, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n8g3 = { id = 9482, ip = 166, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n8r1 = { id = 9483, ip = 167, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = each.key
  node_name   = local.node_name
  pool_id     = local.pool_id
  vm_id       = each.value.id
  description = "newclaude8-r9 Rocky 9 (${each.value.role}) — VMID ${each.value.id}"
  tags        = ["rocky9", "galera", "newclaude8", each.value.role, "n8"]

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
