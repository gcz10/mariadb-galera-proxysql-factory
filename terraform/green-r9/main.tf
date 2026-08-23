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

# Swiezy tenant Rocky 9 generacji green. Nie dziedziczy stanu ani nazw n17.
# ProxySQL, VIP, PMM, MinIO i host aplikacyjny naleza do terraform/green/.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"
  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Aktywny skan 2026-08-24: .28-.31 wolne. VMID 9754-9757 wolne w PVE.
  vms = {
    grg1 = { id = 9754, ip = 28, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    grg2 = { id = 9755, ip = 29, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    grg3 = { id = 9756, ip = 30, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    grr1 = { id = 9757, ip = 31, role = "restore", cpu = 1, ram = 2560, disk = 40 }
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

  tags               = ["rocky9", "galera", "green-r9", "gr9"]
  description_prefix = "green-r9 Rocky 9"
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
