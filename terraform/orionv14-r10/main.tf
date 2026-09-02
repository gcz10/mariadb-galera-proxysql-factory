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
    o14db1 = { id = 10025, ip = 50, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o14db2 = { id = 10026, ip = 51, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o14db3 = { id = 10027, ip = 52, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o14r1  = { id = 10028, ip = 53, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

module "vms" {
  source = "../modules/pve_vm_set"

  source_img = local.source_img
  ssh_pubkey = local.ssh_pubkey
  vms        = local.vms

  tags               = ["rocky10", "galera", "orionv14-r10"]
  description_prefix = "orionv14-r10 Rocky 10"
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
