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
  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"
  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  vms = {
    o8db1 = { id = 9904, ip = 123, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o8db2 = { id = 9905, ip = 124, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o8db3 = { id = 9906, ip = 125, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o8r1  = { id = 9910, ip = 129, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

module "vms" {
  source = "../modules/pve_vm_set"

  source_img = local.source_img
  ssh_pubkey = local.ssh_pubkey
  vms        = local.vms

  tags               = ["rocky9", "galera", "orionv8"]
  description_prefix = "orionv8-r9 Rocky 9"
  description_dash   = "-"
  disk_file_format   = "raw"
  disk_aio           = "io_uring"

  purge_on_destroy                     = true
  delete_unreferenced_disks_on_destroy = true
  started                              = false
}

output "vms" {
  value = module.vms.vms
}
