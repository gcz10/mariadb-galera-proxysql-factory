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

# newclaude16-r9 - warstwa BAZODANOWA szesnastego przebiegu budowy od zera.
#
# Cel: pierwszy klaster stawiany JEDNYM poleceniem `make cluster-build` po tym,
# jak cel przestal konczyc sie na samej konfiguracji backupu. Build materializuje
# teraz dowod (backup -> drill restore -> odswiezenie metryk) PRZED bramka
# `lab-post-build-gate`, wiec probe-backup i probe-restore maja co mierzyc juz
# przy pierwszym przebiegu.
#
# ProxySQL, VIP, PMM, MinIO i host aplikacyjny sa wspoldzielone i mieszkaja
# w terraform/shared/ - ten katalog ich nie tworzy ani nie niszczy.
#
# Rocky 9 GenericCloud NIE wymaga snippetu cloud-init: w przeciwienstwie do EL10
# nie ma `disable_root: true`, wiec klucz z `user_account` trafia prosto do roota.
# Z tego samego powodu ten root nie przekazuje init_interface (domysl providera,
# ide2) ani os_type (pusty blok operating_system w stanie).
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"
  # VMID 9550-9553 i IP .172-.175 sa REUZYWANE po newclaude15-r9, ktory zostal
  # w tym samym przebiegu zderejestrowany (PMM, Grafana, ProxySQL, konto MinIO)
  # i zniszczony. Reuse jest tu swiadomym testem kompletnosci sprzatania: gdyby
  # derejestracja czegos nie usunela, kolizja wyszlaby natychmiast — w PMM
  # (nazwy hostow), w ProxySQL (hostgroupy) albo na ZFS (sieroty po VMID).
  # Dlatego NAZWY hostow sa nowe: PMM Inventory wymaga globalnej unikalnosci
  # w calej flocie, a rozjazd nazw wykrylby zapomniany obiekt po n15.
  #
  # NIE UZYWAC .180-.183: `.181` to adres zarzadzania hypervisora Proxmox
  # (PROXMOX_VE_ENDPOINT). Zywe sa tez .179, .184 i .189. Kolizje z tymi blokami
  # lowi tests/validation/probe-address-collision.py w CI.
  #
  # RAM: limit operatora to 5 GB na VM; wezly Galera dostaja 3 GB, a
  # innodb_buffer_pool_size zjezdza do 768M, zeby zostal zapas na mariabackup
  # podczas SST i backupu.
  vms = {
    n16g1 = { id = 9550, ip = 172, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n16g2 = { id = 9551, ip = 173, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n16g3 = { id = 9552, ip = 174, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n16r1 = { id = 9553, ip = 175, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

# Definicja VM zyje we wspolnym module pve_vm_set; ten root decyduje wylacznie
# o skladzie klastra. Bloku `moved` tu nie ma i byc nie moze — to swiezy root,
# ktory nie dziedziczy zadnych adresow stanu.
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

  tags               = ["rocky9", "galera", "newclaude16"]
  description_prefix = "newclaude16-r9 Rocky 9"
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
  value = "192.168.1.135 (terraform/shared/)"
}
