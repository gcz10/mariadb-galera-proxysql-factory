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
    x17mon = { id = 10050, ip = "192.168.1.70/24", role = "infra", cpu = 2, ram = 4096, disk = 60, purge_on_destroy = false, delete_unreferenced_disks_on_destroy = false }
    x17p1  = { id = 10051, ip = "192.168.1.71/24", role = "proxysql", cpu = 2, ram = 3072, disk = 40 }
    x17p2  = { id = 10052, ip = "192.168.1.72/24", role = "proxysql", cpu = 2, ram = 3072, disk = 40 }
    x17app = { id = 10053, ip = "192.168.1.73/24", role = "app", cpu = 1, ram = 2048, disk = 40 }
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

  tags               = ["rocky9", "infra", "xenonv17"]
  description_prefix = "xenonv17 Rocky 9"
  description_dash   = "-"
  disk_file_format   = "raw"
  disk_aio           = "io_uring"

  purge_on_destroy                     = true
  delete_unreferenced_disks_on_destroy = true
}

output "vms" {
  value = module.vms.vms
}
