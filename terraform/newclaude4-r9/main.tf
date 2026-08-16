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

# newclaude4-r9 — klaster kontrolny: czwarty pelny przebieg budowy od zera.
#
# Weryfikuje zaktualizowany stos fabryki:
# - ProxySQL 3.0.10 (przypiete sha256 w versions.lock.yml)
# - PMM 3.9.0 (monitoring push z pmm-client)
# - MariaDB 11.4.12 LTS + Galera 26.4.27
# - TLS mode: full (rozlaczne CA per klaster)
#
# Adresacja: n4g1-3 na .170-.172 (VMID 9440-9442), restore n4r1 .173 (9443).
# ProxySQL fcp1/fcp2 (.131/.132) i infra fcinfra (.130) naleza do terraform/shared/.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # RAM: limit operatora to 5 GB na VM. Galera dostaje 3 GB, a buffer pool 768M.
  vms = {
    n4g1 = { id = 9440, ip = 170, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n4g2 = { id = 9441, ip = 171, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n4g3 = { id = 9442, ip = 172, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n4r1 = { id = 9443, ip = 173, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = each.key
  node_name   = local.node_name
  pool_id     = local.pool_id
  vm_id       = each.value.id
  description = "newclaude4-r9 Rocky 9 (${each.value.role}) — VMID ${each.value.id}"
  tags        = ["rocky9", "galera", "newclaude4", each.value.role, "n4"]

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
