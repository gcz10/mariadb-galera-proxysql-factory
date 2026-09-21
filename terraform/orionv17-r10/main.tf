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
    o17db1 = { id = 10054, ip = "192.168.1.80/24", role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o17db2 = { id = 10055, ip = "192.168.1.81/24", role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o17db3 = { id = 10056, ip = "192.168.1.82/24", role = "galera", cpu = 2, ram = 3072, disk = 40 }
    o17r1  = { id = 10057, ip = "192.168.1.83/24", role = "restore", cpu = 1, ram = 2560, disk = 40 }
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

  tags               = ["rocky10", "galera", "orionv17-r10"]
  description_prefix = "orionv17-r10 Rocky 10"
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
