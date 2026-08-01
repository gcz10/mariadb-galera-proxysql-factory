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

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = each.key
  node_name   = local.node_name
  vm_id       = each.value.id
  pool_id     = local.pool_id
  tags        = ["finalclaude", "galera", "rocky10", "fc10", each.value.role]
  description = "finalclaude-r10 Rocky 10 (${each.value.role}) — VMID ${each.value.id}"

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
    # Rocky 10 GenericCloud ma disable_root: true w domyslnym cloud.cfg — snippet
    # wymusza klucz operatora i konfiguracje sshd.
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
    vmid = v.id, ip = "192.168.1.${v.ip}", role = v.role
    cpu  = v.cpu, ram_mb = v.ram, disk_gb = v.disk
  } }
}
output "shared_vip" { value = "192.168.1.133 (terraform/shared/)" }
