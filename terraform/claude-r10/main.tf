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

# Rocky Linux 10 — druga platforma obok EL9.
# OSOBNY katalog i OSOBNY stan: modyfikacja terraform/claude-pve/ przebudowalaby
# dzialajacy klaster EL9. VMID i adresy sa rozlaczne z EL9, wiec obie platformy
# moga istniec rownolegle (przydatne, gdy EL9 wroci do zycia dla regresji).
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  # Rocky-10-GenericCloud-Base-10.2 — jedyny obraz GenericCloud opublikowany dla 10
  # (zweryfikowane 2026-07-26: download.rockylinux.org/pub/rocky/10/images/x86_64/).
  # Obraz musi byc wczesniej zaimportowany na PVE do `local:import/`.
  source_img = "local:import/Rocky-10-GenericCloud-Base-10.2.qcow2"

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Adresacja .30-.36 + VIP .40 — lustrzana wobec EL9 (.10-.16 + .20), zeby mapowanie
  # bylo oczywiste. Blok zweryfikowany jako wolny (zajete .25-.28); .37-.39 zostaja
  # jako zapas na czwarty wezel Galera albo arbitra garbd.
  vms = {
    gnode1 = { id = 9110, ip = 30, role = "galera", cpu = 2, ram = 4096, disk = 40 }
    gnode2 = { id = 9111, ip = 31, role = "galera", cpu = 2, ram = 4096, disk = 40 }
    gnode3 = { id = 9112, ip = 32, role = "galera", cpu = 2, ram = 4096, disk = 40 }
    pnode1 = { id = 9113, ip = 33, role = "proxysql", cpu = 1, ram = 2560, disk = 40 }
    pnode2 = { id = 9114, ip = 34, role = "proxysql", cpu = 1, ram = 2560, disk = 40 }
    rnode1 = { id = 9115, ip = 35, role = "restore", cpu = 1, ram = 2560, disk = 40 }
    infra  = { id = 9116, ip = 36, role = "infra", cpu = 4, ram = 8192, disk = 80 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = "r10-${each.key}"
  node_name   = local.node_name
  vm_id       = each.value.id
  pool_id     = local.pool_id
  tags        = ["claude", "rocky10", each.value.role]
  description = "ISA cluster Rocky 10 (${each.value.role}) — prefix r10-, VMID ${each.value.id}"

  # F2 instaluje i wlacza qemu-guest-agent; provider nie czeka na raport IP,
  # bo adresy sa statyczne, a agent moze wystartowac dopiero po restarcie VM.
  agent {
    enabled = true
    type    = "virtio"

    wait_for_ip {
      disabled = true
    }
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
    # Klucz ed25519 idzie do roota — logowanie SSH bezposrednio jako root.
    # Omija problem snippetu (wymagajacego SSH do noda); become:true dla roota
    # to no-op, wiec nic w playbookach repo nie trzeba zmieniac.
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
output "vip" { value = "192.168.1.40" }
output "pmm_url" { value = "https://192.168.1.36" }
