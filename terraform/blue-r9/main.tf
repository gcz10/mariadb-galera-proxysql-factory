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

# Drugi najemca warstwy wspolnej `green`, budowany dla dowodu, ze fabryka stawia
# klaster obok istniejacego bez dotykania go. ProxySQL, VIP, PMM, MinIO i host
# aplikacyjny naleza do terraform/green/ i NIE sa tu zarzadzane.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"
  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Aktywny skan 2026-08-24: .32-.35 wolne (.38 w rejestrze zajetych).
  # VMID 9760-9763 wolne w PVE.
  vms = {
    bg1 = { id = 9760, ip = 32, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    bg2 = { id = 9761, ip = 33, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    bg3 = { id = 9762, ip = 34, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    br1 = { id = 9763, ip = 35, role = "restore", cpu = 1, ram = 2560, disk = 40 }
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

  tags               = ["rocky9", "galera", "blue-r9", "bl9"]
  description_prefix = "blue-r9 Rocky 9"
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
  value = "192.168.1.27 (terraform/green/)"
}
