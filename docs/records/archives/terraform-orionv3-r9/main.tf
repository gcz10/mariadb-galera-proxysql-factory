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

# orionv3-r9: czwarty czysty przebieg. Ten root tworzy wylacznie trzy wezly
# Galery; ProxySQL, VIP, PMM i host aplikacyjny naleza do platformy kobalt.
# Rownolegly vegav3-r9 dostaje maszyny z golego REST API — ta sama fabryka,
# dwa zrodla maszyn.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"
  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  vms = {
    o3g1 = { id = 9840, ip = 57, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o3g2 = { id = 9841, ip = 58, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o3g3 = { id = 9842, ip = 59, role = "galera", cpu = 2, ram = 3072, disk = 40 }
  }
}

module "vms" {
  source = "../modules/pve_vm_set"

  node_name  = local.node_name
  pool_id    = local.pool_id
  storage    = local.storage
  bridge     = local.bridge
  source_img = local.source_img
  ssh_pubkey = local.ssh_pubkey
  gateway    = local.gateway
  vms        = local.vms

  tags               = ["rocky9", "galera", "orionv3"]
  description_prefix = "orionv3-r9 Rocky 9"
  description_dash   = "-"

  disk_file_format = "raw"
  disk_aio         = "io_uring"

  purge_on_destroy                     = true
  delete_unreferenced_disks_on_destroy = true
}

output "vms" {
  value = module.vms.vms
}

output "shared_vip" {
  value = "192.168.1.31 (platform/kobalt)"
}
