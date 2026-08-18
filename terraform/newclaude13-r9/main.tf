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

# newclaude13-r9 — warstwa BAZODANOWA trzynastego przebiegu budowy od zera.
#
# Cel: naturalny dowod parametrow produkcyjnych z PR #50. THP off, OOMScoreAdjust,
# wsrep_slave_threads i GARP powstaly i byly mierzone na DZIALAJACYM n12 — czyli
# na maszynach po bootstrapie, SST i restartach. Tutaj musza zadzialac w innej
# kolejnosci: `disable-thp.service` przed pierwszym startem mariadbd, drop-in
# OOMScoreAdjust przed pierwszym uruchomieniem jednostki, a `wsrep_slave_threads`
# z faktow zebranych na surowym hoscie.
#
# ProxySQL, VIP, PMM, MinIO i host aplikacyjny sa wspoldzielone i mieszkaja
# w terraform/shared/ — ten katalog ich nie tworzy ani nie niszczy.
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
  # VMID 9530-9533 wolne (sprawdzone przez API na pelnej liscie VM klastra PVE).
  # IP .164-.167 potwierdzone AKTYWNYM skanem sieci (ping + port 22/8006) juz PO
  # zniszczeniu n12, wiec blok jest realnie pusty, a nie tylko zwolniony w planie.
  #
  # Skan jest falsyfikowalny i to sie oplaca: kontrola .179 w tym samym przebiegu
  # wyszla ZYWA (host spoza floty, odnotowany w clusters/reserved-addresses.yml).
  #
  # Blok nalezal kiedys do n8 (dawno zniszczonego), wiec w known_hosts moga siedziec
  # stare klucze SSH. Czysci je `make cluster-trust-hosts` przed pierwszym playbookiem.
  #
  # NIE UZYWAC .180-.183: `.181` to adres zarzadzania hypervisora Proxmox
  # (PROXMOX_VE_ENDPOINT). Zywe sa tez .179, .184 i .189. Kolizje z tymi blokami
  # lowi tests/validation/probe-address-collision.py w CI.
  #
  # RAM: limit operatora to 5 GB na VM; wezly Galera dostaja 3 GB, a
  # innodb_buffer_pool_size zjezdza do 768M, zeby zostal zapas na mariabackup
  # podczas SST i backupu.
  vms = {
    n13g1 = { id = 9530, ip = 164, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n13g2 = { id = 9531, ip = 165, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n13g3 = { id = 9532, ip = 166, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n13r1 = { id = 9533, ip = 167, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = each.key
  node_name   = local.node_name
  pool_id     = local.pool_id
  vm_id       = each.value.id
  description = "newclaude13-r9 Rocky 9 (${each.value.role}) — VMID ${each.value.id}"
  tags        = ["rocky9", "galera", "newclaude13", each.value.role, "n13"]

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
