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

# claude-r9t — klaster weryfikacyjny Rocky Linux 9 z tls.mode=full.
#
# Powstal jako odpowiednik claude-r10t (EL10 TLS), ale na EL9 — zeby udowodnic,
# ze sciezka TLS full dziala takze na Rocky 9, nie tylko na Rocky 10.
# Klaster jest samowystarczalny: 3 wezly Galera + wlasny wezel restore.
# ProxySQL (VIP .60) i PMM (.47) zostaja wspoldzielone — nie sa tworzone tutaj.
#
# Adresacja .54-.57: przejeta po wylaczonym claude-r10t. Rozlaczna z claude-r9g
# (.17-.19/.39), claude-r10b (.41-.47/.51-.53) i claude-pve (.10-.16).
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "data1"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-9.8-GenericCloud.qcow2"

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # 2560 MB: przydzielone 2048 daje ~1771 MB widzianych przez system — odbija sie
  # od bramki f2_preflight ansible_memtotal_mb >= 2048. 2560 przechodzi bez
  # oslabiania guardu.
  vms = {
    g9tnode1 = { id = 9170, ip = 54, role = "galera", cpu = 2, ram = 2560, disk = 40 }
    g9tnode2 = { id = 9171, ip = 55, role = "galera", cpu = 2, ram = 2560, disk = 40 }
    g9tnode3 = { id = 9172, ip = 56, role = "galera", cpu = 2, ram = 2560, disk = 40 }
    r9tnode1 = { id = 9173, ip = 57, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = "r9t-${each.key}"
  node_name   = local.node_name
  vm_id       = each.value.id
  pool_id     = local.pool_id
  tags        = ["claude", "rocky9", "r9t", "tls", each.value.role]
  description = "ISA cluster Rocky 9 TLS full (${each.value.role}) — prefix r9t-, VMID ${each.value.id}"

  agent {
    enabled = true
    type    = "virtio"
    wait_for_ip { disabled = true }
  }
  stop_on_destroy = true
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
output "tls_mode" { value = "full — certyfikaty z tests/lab/tls/r9t/ (gitignored)" }