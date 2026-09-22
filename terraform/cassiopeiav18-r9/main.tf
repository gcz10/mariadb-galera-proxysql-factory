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

  # Topologia tego roota, jawna u operatora: modul pve_vm_set nie ma zadnej
  # domyslnej wartosci infrastruktury, wiec nowy root NIC nie odziedziczy po
  # poprzednim. Adresy VM sa pelnymi CIDR-ami — podsiec i prefiks naleza do
  # deklaracji, nie do modulu.
  node_name   = "pve"
  pool_id     = "claude-isa"
  storage     = "local-zfs"
  bridge      = "vmbr0"
  gateway     = "192.168.1.1"
  dns_servers = ["1.1.1.1", "8.8.8.8"]

  vms = {
    c18db1 = { id = 10062, ip = "192.168.1.94/24", role = "galera", cpu = 2, ram = 3072, disk = 40 }
    c18db2 = { id = 10063, ip = "192.168.1.95/24", role = "galera", cpu = 2, ram = 3072, disk = 40 }
    c18db3 = { id = 10064, ip = "192.168.1.96/24", role = "galera", cpu = 2, ram = 3072, disk = 40 }
    c18r1  = { id = 10065, ip = "192.168.1.97/24", role = "restore", cpu = 1, ram = 2560, disk = 40 }
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

  tags               = ["cassiopeiav18", "rocky9"]
  description_prefix = "cassiopeiav18-r9 Rocky 9"
  description_dash   = "-"
  disk_file_format   = "raw"
  disk_aio           = "io_uring"

  purge_on_destroy                     = true
  delete_unreferenced_disks_on_destroy = true
}

output "vms" {
  value = module.vms.vms
}
