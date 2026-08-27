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

# lyrav5-r9: pierwszy najemca warstwy argonv5. Rownolegly mirav5-r9 dostaje
# maszyny z golego REST API — ta sama fabryka, dwa zrodla maszyn.
# `-parallelism=1` przy apply: import obrazu trzyma jeden lock local-zfs.
locals {
  node_name  = "pve"
  pool_id    = "claude-isa"
  storage    = "local-zfs"
  bridge     = "vmbr0"
  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"
  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  vms = {
    lg1 = { id = 9864, ip = 68, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    lg2 = { id = 9865, ip = 69, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    lg3 = { id = 9866, ip = 70, role = "galera", cpu = 2, ram = 3072, disk = 40 }
  }
}

module "vms" {
  source     = "../modules/pve_vm_set"
  node_name  = local.node_name
  pool_id    = local.pool_id
  storage    = local.storage
  bridge     = local.bridge
  source_img = local.source_img
  ssh_pubkey = local.ssh_pubkey
  gateway    = local.gateway
  vms        = local.vms

  tags               = ["rocky9", "galera", "lyrav5"]
  description_prefix = "lyrav5-r9 Rocky 9"
  description_dash   = "-"

  disk_file_format = "raw"
  disk_aio         = "io_uring"

  purge_on_destroy                     = true
  delete_unreferenced_disks_on_destroy = true
}

output "vms" { value = module.vms.vms }
output "shared_vip" { value = "192.168.1.67 (platform/argonv5)" }
