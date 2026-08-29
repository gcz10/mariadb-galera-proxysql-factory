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
    x8mon = { id = 9900, ip = 118, role = "infra", cpu = 2, ram = 5120, disk = 40 }
    x8p1  = { id = 9901, ip = 119, role = "proxysql", cpu = 2, ram = 3072, disk = 40 }
    x8p2  = { id = 9902, ip = 120, role = "proxysql", cpu = 2, ram = 3072, disk = 40 }
    x8app = { id = 9903, ip = 121, role = "app", cpu = 1, ram = 3072, disk = 40 }
  }
}

module "vms" {
  source = "../modules/pve_vm_set"

  source_img = local.source_img
  ssh_pubkey = local.ssh_pubkey
  vms        = local.vms

  tags               = ["rocky9", "platform", "xenonv8"]
  description_prefix = "xenonv8 platforma Rocky 9"
  description_dash   = "-"
  disk_file_format   = "raw"
  disk_aio           = "io_uring"

  purge_on_destroy                     = true
  delete_unreferenced_disks_on_destroy = true
}

output "vms" {
  value = module.vms.vms
}
