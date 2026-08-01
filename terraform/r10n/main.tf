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

# r10n — TYLKO warstwa Galera trzeciego klastra Rocky 10.
# OSOBNY katalog i OSOBNY stan; VMID 9190-9192 i IP .71-.73 sa rozlaczne ze
# wszystkimi pozostalymi modulami (claude-r10 .31-.37, claude-r10b .44-.47/.51-.53,
# claude-r10t/.claude-r9t .54-.60, claude-r9g .17-.19/.39).
#
# ProxySQL (.44/.45), VIP (.50), restore (.46) i infra/PMM/MinIO (.47) NALEZA do
# terraform/claude-r10b/ i sa przez r10n wspoldzielone. Ten modul ich NIE tworzy —
# `terraform destroy` tutaj kasuje wylacznie wezly bazy, uslugi wspoldzielone zostaja.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  # Ten sam obraz co pozostale klastry EL10; musi byc wczesniej zaimportowany na PVE.
  source_img = "local:import/Rocky-10.2-GenericCloud.qcow2"

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Adresacja .71-.73 — blok zweryfikowany jako wolny (brak ARP/SSH przed apply).
  vms = {
    galera1 = { id = 9190, ip = 71 }
    galera2 = { id = 9191, ip = 72 }
    galera3 = { id = 9192, ip = 73 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = "r10n-${each.key}"
  node_name   = local.node_name
  vm_id       = each.value.id
  pool_id     = local.pool_id
  tags        = ["claude", "galera", "rocky10", "r10n"]
  description = "ISA new Rocky 10 Galera cluster (${each.key}) — VMID ${each.value.id}"

  # F2 instaluje i wlacza qemu-guest-agent; provider nie czeka na raport IP,
  # bo adresy sa statyczne, a agent moze wystartowac dopiero po restarcie VM.
  agent {
    enabled = true
    type    = "virtio"
    wait_for_ip { disabled = true }
  }
  stop_on_destroy = true
  operating_system { type = "l26" }

  cpu {
    type  = "host"
    cores = 2
  }
  memory { dedicated = 4096 }

  disk {
    datastore_id = local.storage
    interface    = "virtio0"
    import_from  = local.source_img
    size         = 40
    discard      = "on"
  }

  initialization {
    interface    = "scsi1"
    datastore_id = local.storage
    # Rocky 10 GenericCloud ma disable_root: true w domyslnym cloud.cfg — snippet
    # wymusza klucz operatora i konfiguracje sshd (patrz terraform/claude-r10b/).
    user_data_file_id = "local:snippets/r10-cloud-init.yaml"

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
    vmid = v.id
    ip   = "192.168.1.${v.ip}"
    name = "r10n-${k}"
  } }
}
# Wspoldzielone z claude-r10b — wypisane, zeby operator nie szukal ich w tym module.
output "shared_endpoint" { value = "192.168.1.50:6033 (ProxySQL VIP claude-r10b)" }
output "shared_pmm_url" { value = "https://192.168.1.47" }
