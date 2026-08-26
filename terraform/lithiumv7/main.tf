terraform {
  required_version = ">= 1.5"
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "0.111.1"
    }
  }
}

provider "proxmox" {}

locals {
  node_name  = "pve"
  pool_id    = "claude-isa"
  storage    = "local-zfs"
  bridge     = "vmbr0"
  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"
  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"
  vms = {
    l7mon = { id = 9880, ip = 85, role = "infra", cpu = 2, ram = 5120, disk = 40 }
    l7p1  = { id = 9881, ip = 86, role = "proxysql", cpu = 2, ram = 3072, disk = 40 }
    l7p2  = { id = 9882, ip = 87, role = "proxysql", cpu = 2, ram = 3072, disk = 40 }
    l7app = { id = 9883, ip = 88, role = "app", cpu = 1, ram = 3072, disk = 40 }
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

  tags               = ["rocky9", "platform", "lithiumv7"]
  description_prefix = "lithiumv7 platforma Rocky 9"
  description_dash   = "-"
  disk_file_format   = "raw"
  disk_aio           = "io_uring"

  purge_on_destroy                     = true
  delete_unreferenced_disks_on_destroy = true
}

output "vms" {
  value = module.vms.vms
}

output "vip" {
  value = "192.168.1.89"
}
