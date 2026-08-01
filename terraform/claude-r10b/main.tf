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
  source_img = "local:import/Rocky-10.2-GenericCloud.qcow2" # nazwa pliku na PVE (zweryfikowane 2026-07-27)

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Adresacja .44-.47 + .51-.53 + VIP .50 — drugi klaster EL10 obok claude-r10 (.31-.37 + .40).
  vms = {
    # Druga generacja wezlow Galera: nowe VMID/IP/hostname. ProxySQL, restore i infra
    # zostaja NIETKNIETE (te same VMID/IP) — wymianie podlega tylko warstwa bazy.
    # started=false: wezly Galera sa CELOWO wylaczone (zatrzymane 2026-08-01, dyski
    # zachowane). Warstwe bazy przejal klaster r10n (terraform/r10n/, .71-.73), a
    # wspoldzielony ProxySQL/VIP/PMM/restore ponizej dziala dalej. Bez tej flagi
    # `terraform apply` po cichu wystartowalby martwy klaster.
    gnode4 = { id = 9130, ip = 51, role = "galera", cpu = 2, ram = 4096, disk = 40, started = false }
    gnode5 = { id = 9131, ip = 52, role = "galera", cpu = 2, ram = 4096, disk = 40, started = false }
    gnode6 = { id = 9132, ip = 53, role = "galera", cpu = 2, ram = 4096, disk = 40, started = false }
    pnode1 = { id = 9123, ip = 44, role = "proxysql", cpu = 1, ram = 2560, disk = 40 }
    pnode2 = { id = 9124, ip = 45, role = "proxysql", cpu = 1, ram = 2560, disk = 40 }
    rnode1 = { id = 9125, ip = 46, role = "restore", cpu = 1, ram = 2560, disk = 40 }
    infra  = { id = 9126, ip = 47, role = "infra", cpu = 4, ram = 8192, disk = 80 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = "r10b-${each.key}"
  node_name   = local.node_name
  vm_id       = each.value.id
  pool_id     = local.pool_id
  tags        = ["claude", "rocky10", "r10b", each.value.role]
  description = "ISA cluster Rocky 10 (${each.value.role}) — prefix r10b-, VMID ${each.value.id}"

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
  # Domyslnie VM ma dzialac; mapa `vms` moze to nadpisac dla wezlow trzymanych w spoczynku.
  started = try(each.value.started, true)
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
    # Rocky 10 GenericCloud ma disable_root: true w domyslnym cloud.cfg — klucz
    # z user_account nie trafia do /root/.ssh/authorized_keys. Snippet wymusza klucz
    # operatora i konfiguracje sshd (PermitRootLogin prohibit-password, PubkeyAuth yes).
    user_data_file_id = "local:snippets/r10-cloud-init.yaml"
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
output "vip" { value = "192.168.1.50" }
output "pmm_url" { value = "https://192.168.1.47" }
