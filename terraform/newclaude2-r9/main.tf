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

# newclaude2-r9 — klaster kontrolny: budowa OD ZERA na tym samym kodzie.
#
# Po co istnieje: to DRUGI przebieg budowy od zera. Pierwszy (`newclaude-r9`,
# 2026-08-15) postawil dzialajacy klaster, ale po drodze znalazl szesc defektow
# — trzy z nich w F11, gdzie sciezka z lokalnym pmm-agentem po cichu zakladala
# artefakty tworzone przez sciezke agentless. Wszystkie naprawione. Ten klaster
# sprawdza, czy poprawki trzymaja przy budowie bez recznej interwencji.
# Wczesniejsze takie cwiczenie (2026-08-02) znalazlo piec innych defektow.
#
# Wszystko rozlaczne z `finalclaude-r9`, ktory zostaje WYLACZONY (nie skasowany)
# i moze wrocic: inne VMID, adresy, nazwy hostow, wsrep_cluster_name, hostgroupy
# ProxySQL, uzytkownik aplikacyjny, etykieta PMM, bucket MinIO i certyfikaty TLS.
#
# ProxySQL, VIP, PMM i MinIO sa wspoldzielone i mieszkaja w terraform/shared/ —
# ten katalog ich nie tworzy ani nie niszczy.
#
# Rocky 9 GenericCloud NIE wymaga snippetu cloud-init: w przeciwienstwie do EL10
# nie ma `disable_root: true`, wiec klucz z `user_account` trafia prosto do roota.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Ten sam profil zasobow co finalclaude-r9 — porownywalnosc jest calym sensem
  # tego klastra. Limit operatora: 5 GB na VM. Galera dostaje 3 GB, a
  # innodb_buffer_pool_size zjezdza do 768M, zeby zostal zapas na mariabackup
  # podczas SST i backupu.
  #
  # VMID 9430-9433: zwolnione po `newclaude-r9`, zweryfikowane jako wolne wraz
  # z brakiem sierot ZFS. Zajete w puli: 9400-9402 (shared), 9410-9413 (r10),
  # 9420-9423 (r9, wylaczony ale ISTNIEJE — nie wolno nadpisac).
  # Adresy .160-.163: zwolnione po tym samym teardownie.
  vms = {
    n2g1 = { id = 9430, ip = 160, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n2g2 = { id = 9431, ip = 161, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n2g3 = { id = 9432, ip = 162, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    n2r1 = { id = 9433, ip = 163, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = each.key
  node_name   = local.node_name
  vm_id       = each.value.id
  pool_id     = local.pool_id
  tags        = ["newclaude2", "galera", "rocky9", "n2", each.value.role]
  description = "newclaude2-r9 Rocky 9 (${each.value.role}) — VMID ${each.value.id}"

  # F2 instaluje i wlacza qemu-guest-agent; provider nie czeka na raport IP,
  # bo adresy sa statyczne, a agent moze wystartowac dopiero po restarcie VM.
  agent {
    enabled = true
    type    = "virtio"
    wait_for_ip { disabled = true }
  }
  stop_on_destroy = true
  started         = true
  operating_system { type = "l26" }

  cpu {
    type  = "host"
    cores = each.value.cpu
  }
  memory { dedicated = each.value.ram }

  disk {
    datastore_id = local.storage
    interface    = "virtio0"
    import_from  = local.source_img
    size         = each.value.disk
    discard      = "on"
  }

  initialization {
    interface    = "scsi1"
    datastore_id = local.storage
    user_account {
      username = "root"
      keys     = [local.ssh_pubkey]
    }
    ip_config {
      ipv4 {
        address = "192.168.1.${each.value.ip}/24"
        gateway = local.gateway
      }
    }
    dns { servers = ["1.1.1.1", "8.8.8.8"] }
  }

  network_device { bridge = local.bridge }
}

output "vms" {
  value = { for k, v in local.vms : k => {
    vmid = v.id, ip = "192.168.1.${v.ip}", role = v.role
    cpu  = v.cpu, ram_mb = v.ram, disk_gb = v.disk
  } }
}
output "shared_vip" { value = "192.168.1.133 (terraform/shared/)" }
