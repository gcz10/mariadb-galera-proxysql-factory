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

# orion-r9 - najemca stawiany SCIEZKA TERRAFORMOWA, rownolegle z `vega-r9`,
# ktory te same trzy wezly dostaje z golego REST API. Oba wchodza na te sama
# warstwe `kobalt` (ProxySQL kp1/kp2, VIP .31, PMM kmon, host aplikacyjny kapp)
# i oba przechodza identyczna sciezke z README od kroku 2. Roznica jest
# wylacznie w tym, KTO tworzy maszyny - i to jest cel tego przebiegu.
#
# Ten katalog nie tworzy ani nie niszczy warstwy wspolnej; ona zyje
# w terraform/shared/ i przezyla juz kilka pokolen najemcow.
#
# Rocky 9 GenericCloud nie wymaga snippetu cloud-init: nie ma
# `disable_root: true`, wiec klucz z `user_account` trafia prosto do roota.
#
# VMID 9800-9802 i adresy .43-.45 sa SWIEZE - nie reuzywam po `nova-r9`
# (9780-9782, .36/.37/.39) ani `sigma-r9` (9790-9792, .40-.42), bo definicje
# tamtych najemcow ZOSTAJA w repozytorium jako slad przebiegu i nadal
# rezerwuja swoje adresy w tests/validation/probe-address-collision.py.
#
# RAM: limit operatora to 5 GB na VM; wezly Galera biora 3 GB, a
# innodb_buffer_pool_size zjezdza do 768M, zeby zostal zapas na mariabackup.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Bez hosta `restore`: ten najemca ma `backup.enabled: false`, wiec drill nie
  # ma czego odtwarzac. Dokladanie maszyny, ktora nic nie robi, tylko zjada RAM.
  vms = {
    og1 = { id = 9800, ip = 43, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    og2 = { id = 9801, ip = 44, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    og3 = { id = 9802, ip = 45, role = "galera", cpu = 2, ram = 3072, disk = 40 }
  }
}

# Definicja VM zyje we wspolnym module pve_vm_set; ten root decyduje wylacznie
# o skladzie klastra. Bloku `moved` tu nie ma i byc nie moze - to swiezy root,
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

  tags               = ["rocky9", "galera", "orion"]
  description_prefix = "orion-r9 Rocky 9"
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
  value = "192.168.1.31 (platform/kobalt)"
}
