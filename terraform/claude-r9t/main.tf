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
# Klaster jest samowystarczalny: 3 wezly Galera, para ProxySQL i wlasny restore.
# PMM oraz MinIO (.47) sa wspoldzielone z claude-r10b.
#
# Adresacja .61-.66 i VIP .70 jest rozlaczna z claude-r10t (.54-.60),
# claude-r9g (.17-.19/.39), claude-r10b (.41-.47/.51-.53)
# i claude-pve (.10-.16/.20).
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
    g9tnode1 = { id = 9180, ip = 61, role = "galera", cpu = 2, ram = 2560, disk = 40 }
    g9tnode2 = { id = 9181, ip = 62, role = "galera", cpu = 2, ram = 2560, disk = 40 }
    g9tnode3 = { id = 9182, ip = 63, role = "galera", cpu = 2, ram = 2560, disk = 40 }
    r9tnode1 = { id = 9183, ip = 66, role = "restore", cpu = 1, ram = 2560, disk = 40 }
    p9tnode1 = { id = 9184, ip = 64, role = "proxysql", cpu = 1, ram = 2560, disk = 40, store = "local-zfs" }
    p9tnode2 = { id = 9185, ip = 65, role = "proxysql", cpu = 1, ram = 2560, disk = 40, store = "local-zfs" }
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
    datastore_id = try(each.value.store, local.storage)
    interface    = "virtio0"
    import_from  = local.source_img
    size         = each.value.disk
    discard      = "on"
  }

  initialization {
    interface    = "scsi1"
    datastore_id = try(each.value.store, local.storage)
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
output "vip" { value = "192.168.1.70" }
output "tls_mode" { value = "full — certyfikaty z tests/lab/tls/r9t/ (gitignored)" }
