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
    o9db1 = { id = 9916, ip = 141, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o9db2 = { id = 9917, ip = 142, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o9db3 = { id = 9918, ip = 143, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o9r1  = { id = 9919, ip = 144, role = "restore", cpu = 1, ram = 2560, disk = 40 }
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

  tags               = ["rocky9", "galera", "orionv9"]
  description_prefix = "orionv9-r9 Rocky 9"
  description_dash   = "-"
  disk_file_format   = "raw"
  disk_aio           = "io_uring"

  purge_on_destroy                     = true
  delete_unreferenced_disks_on_destroy = true
}

output "vms" {
  value = module.vms.vms
}

output "shared_vip" {
  value = "192.168.1.149 (platform/xenonv9)"
}
