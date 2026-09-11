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
    c17db1 = { id = 10058, ip = 90, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    c17db2 = { id = 10059, ip = 91, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    c17db3 = { id = 10060, ip = 92, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    c17r1  = { id = 10061, ip = 93, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

module "vms" {
  source = "../modules/pve_vm_set"

  source_img = local.source_img
  ssh_pubkey = local.ssh_pubkey
  vms        = local.vms

  tags               = ["rocky9", "galera", "cassiopeiav14"]
  description_prefix = "cassiopeiav17-r9 Rocky 9"
  description_dash   = "-"
  disk_file_format   = "raw"
  disk_aio           = "io_uring"

  purge_on_destroy                     = true
  delete_unreferenced_disks_on_destroy = true
}

output "vms" {
  value = module.vms.vms
}
