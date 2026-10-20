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

  # Najemca weryfikacyjny: swieza budowa MariaDB 12.3 na Rocky 9 (ADR-005).
  # Adresy .84-.87 potwierdzone 2026-10-20: brak w konfiguracji VM na PVE,
  # brak odpowiedzi na ping, poza clusters/reserved-addresses.yml.
  node_name   = "pve"
  pool_id     = "claude-isa"
  storage     = "local-zfs"
  bridge      = "vmbr0"
  gateway     = "192.168.1.1"
  dns_servers = ["1.1.1.1", "8.8.8.8"]

  vms = {
    l19db1 = { id = 10066, ip = "192.168.1.84/24", role = "galera", cpu = 2, ram = 3072, disk = 40 }
    l19db2 = { id = 10067, ip = "192.168.1.85/24", role = "galera", cpu = 2, ram = 3072, disk = 40 }
    l19db3 = { id = 10068, ip = "192.168.1.86/24", role = "galera", cpu = 2, ram = 3072, disk = 40 }
    l19r1  = { id = 10069, ip = "192.168.1.87/24", role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

module "vms" {
  source = "../modules/pve_vm_set"

  source_img = local.source_img
  ssh_pubkey = local.ssh_pubkey
  vms        = local.vms

  node_name   = local.node_name
  pool_id     = local.pool_id
  storage     = local.storage
  bridge      = local.bridge
  gateway     = local.gateway
  dns_servers = local.dns_servers

  tags               = ["lyrav19", "rocky9"]
  description_prefix = "lyrav19-r9 Rocky 9"
  description_dash   = "-"
  disk_file_format   = "raw"
  disk_aio           = "io_uring"

  purge_on_destroy                     = true
  delete_unreferenced_disks_on_destroy = true
}

output "vms" {
  value = module.vms.vms
}
