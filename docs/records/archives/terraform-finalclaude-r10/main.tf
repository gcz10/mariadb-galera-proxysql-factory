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

# finalclaude-r10 — warstwa BAZODANOWA klastra Rocky 10.
#
# Ten modul tworzy wylacznie to, co nalezy do tego klastra: wezly Galera i wlasny
# host restore. ProxySQL, VIP, PMM i MinIO sa wspoldzielone przez cala flote
# i mieszkaja w terraform/shared/ — dzieki temu zaden klastr nie jest wlascicielem
# warstwy dostepowej i nie moze jej przepiac na swoje wezly przy converge.
#
# Wlasny host restore (nie wspoldzielony): drill restore czysci datadir, wiec dwa
# klastry odtwarzajace sie na jednej maszynie skasowalyby sobie nawzajem probe.
# Harmonogramy drilli sa identyczne (`0 4 * * 0`), wiec kolizja bylaby regularna.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-10.2-GenericCloud.qcow2"

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # RAM: limit operatora to 3 GB na VM. Buffer pool zjezdza do 768M
  # (clusters/finalclaude-r10/cluster.yml), zeby zostal zapas na mariabackup
  # podczas SST i backupu.
  vms = {
    f10g1 = { id = 9410, ip = 140, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    f10g2 = { id = 9411, ip = 141, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    f10g3 = { id = 9412, ip = 142, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    f10r1 = { id = 9413, ip = 143, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

# Definicja VM zyje we wspolnym module pve_vm_set; ten root decyduje wylacznie
# o skladzie klastra. Blok moved nizej przenosi istniejace adresy stanu,
# wiec plan po migracji nie zawiera destroy/create.
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

  tags               = ["finalclaude", "galera", "rocky10", "fc10"]
  description_prefix = "finalclaude-r10 Rocky 10"

  os_type        = "l26"
  init_interface = "scsi1"
  # Rocky 10 GenericCloud ma disable_root: true w domyslnym cloud.cfg — snippet
  # wymusza klucz operatora i konfiguracje sshd.
  user_data_file_id = "local:snippets/r10-cloud-init.yaml"
}

moved {
  from = proxmox_virtual_environment_vm.node
  to   = module.vms.proxmox_virtual_environment_vm.node
}

output "vms" {
  value = module.vms.vms
}
output "shared_vip" { value = "192.168.1.139 (terraform/shared/)" }
