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

# Warstwa wspolna `kobalt`: monitoring i para ProxySQL z VIP-em. BEZ magazynu
# kopii — S3 jest usluga zewnetrzna i nie dzieli cyklu zycia z monitoringiem.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"
  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Aktywny skan 2026-08-24: .28-.30 wolne, .31 zarezerwowane pod VIP.
  vms = {
    kmon = { id = 9770, ip = 28, role = "infra", cpu = 2, ram = 5120, disk = 40 }
    kp1  = { id = 9771, ip = 29, role = "proxysql", cpu = 2, ram = 3072, disk = 40 }
    kp2  = { id = 9772, ip = 30, role = "proxysql", cpu = 2, ram = 3072, disk = 40 }
    # Jedyny host patrzacy na klaster OCZAMI APLIKACJI: po sieci, przez VIP.
    # Bez niego bramka warstwy nie ma skad zmierzyc TLS endpointu i konczy sie
    # UNDETERMINED — sonda odmawia zielonego bez pomiaru.
    kapp = { id = 9776, ip = 32, role = "app", cpu = 1, ram = 3072, disk = 40 }
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

  tags               = ["rocky9", "platform", "kobalt"]
  description_prefix = "kobalt platforma Rocky 9"
  description_dash   = "-"

  disk_file_format = "raw"
  disk_aio         = "io_uring"

  purge_on_destroy                     = true
  delete_unreferenced_disks_on_destroy = true
}

output "vms" {
  value = module.vms.vms
}
output "vip" {
  value = "192.168.1.31 (Keepalived, wdrazany przez make platform-endpoint)"
}
