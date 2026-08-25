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

# Najemca `kobalt-r9`: wylacznie wezly Galery. ProxySQL, VIP i monitoring naleza
# do terraform/kobalt/ i NIE sa tu zarzadzane.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"
  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Aktywny skan 2026-08-24: .33-.35 wolne.
  vms = {
    kg1 = { id = 9773, ip = 33, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    kg2 = { id = 9774, ip = 34, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    kg3 = { id = 9775, ip = 35, role = "galera", cpu = 2, ram = 3072, disk = 40 }
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

  vms = local.vms

  tags               = ["rocky9", "galera", "kobalt-r9", "kb9"]
  description_prefix = "kobalt-r9 Rocky 9"
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
  value = "192.168.1.31 (terraform/kobalt/)"
}
