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

# Rownolegla warstwa wspolna generacji `green`. Stare fcinfra/fcp1/fcp2/fcapp
# pozostaja zatrzymanym rollbackiem i nie sa w tym stanie Terraform.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-10.2-GenericCloud.qcow2"
  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Aktywny skan 2026-08-24: .20-.22 i .38 sa zajete; blok .23-.31 wolny.
  # VMID 9750-9757 nie wystepuja w PVE. Dyski warstwy wspolnej sa mniejsze niz
  # w starym stacku, ale zachowuja zapas wobec zmierzonego uzycia PMM/ProxySQL.
  vms = {
    grinfra = { id = 9750, ip = 23, role = "infra", cpu = 4, ram = 5120, disk = 40 }
    grp1    = { id = 9751, ip = 24, role = "proxysql", cpu = 1, ram = 3072, disk = 20 }
    grp2    = { id = 9752, ip = 25, role = "proxysql", cpu = 1, ram = 3072, disk = 20 }
    grapp   = { id = 9753, ip = 26, role = "app", cpu = 2, ram = 3072, disk = 20 }
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

  tags               = ["green", "shared", "rocky10"]
  description_prefix = "green warstwa wspolna"

  os_type           = "l26"
  init_interface    = "scsi1"
  user_data_file_id = "local:snippets/r10-cloud-init.yaml"
}

output "vms" {
  value = module.vms.vms
}
output "vip" { value = "192.168.1.27" }
output "pmm_url" { value = "https://192.168.1.23" }
output "s3_endpoint" { value = "192.168.1.23:9000" }
