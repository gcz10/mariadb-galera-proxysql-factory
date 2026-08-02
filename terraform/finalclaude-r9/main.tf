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

# finalclaude-r9 — warstwa BAZODANOWA klastra Rocky 9.
#
# Druga platforma obok finalclaude-r10. Warstwa bazy jest wersyjnie identyczna
# (MariaDB 11.4.12, galera-4 26.4.27) — rozni je wylacznie obraz i lockfile.
#
# ProxySQL, VIP, PMM i MinIO sa wspoldzielone i mieszkaja w terraform/shared/.
# Wlasny host restore, bo drill czysci datadir — wspolny host oznaczalby kolizje
# dwoch klastrow odtwarzajacych sie jednoczesnie.
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

  # RAM: limit operatora to 5 GB na VM; wezly Galera dostaja 3 GB, a buffer pool
  # zjezdza do 768M (clusters/finalclaude-r9/cluster.yml), zeby zostal zapas na
  # mariabackup podczas SST i backupu.
  vms = {
    f9g1 = { id = 9420, ip = 150, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    f9g2 = { id = 9421, ip = 151, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    f9g3 = { id = 9422, ip = 152, role = "galera", cpu = 2, ram = 3072, disk = 40 }
    f9r1 = { id = 9423, ip = 153, role = "restore", cpu = 1, ram = 2560, disk = 40 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = each.key
  node_name   = local.node_name
  vm_id       = each.value.id
  pool_id     = local.pool_id
  tags        = ["finalclaude", "galera", "rocky9", "fc9", each.value.role]
  description = "finalclaude-r9 Rocky 9 (${each.value.role}) — VMID ${each.value.id}"

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
