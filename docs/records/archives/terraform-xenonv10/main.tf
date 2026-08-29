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
  source_img = "local:import/Rocky-10.2-GenericCloud.qcow2"
  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  vms = {
    x10mon = { id = 10000, ip = 160, role = "infra", cpu = 2, ram = 5120, disk = 40 }
    x10p1  = { id = 10001, ip = 161, role = "proxysql", cpu = 2, ram = 3072, disk = 40 }
    x10p2  = { id = 10002, ip = 162, role = "proxysql", cpu = 2, ram = 3072, disk = 40 }
    x10app = { id = 10003, ip = 163, role = "app", cpu = 1, ram = 3072, disk = 40 }
  }
}

module "vms" {
  source = "../modules/pve_vm_set"

  source_img = local.source_img
  ssh_pubkey = local.ssh_pubkey
  vms        = local.vms

  tags               = ["rocky10", "platform", "xenonv10"]
  description_prefix = "xenonv10 platforma Rocky 10"
  description_dash   = "-"
  disk_file_format   = "raw"
  disk_aio           = "io_uring"
  os_type            = "l26"
  init_interface     = "scsi1"
  user_data_file_id  = "local:snippets/r10-cloud-init.yaml"

  purge_on_destroy                     = true
  delete_unreferenced_disks_on_destroy = true
}

output "vms" {
  value = module.vms.vms
}
