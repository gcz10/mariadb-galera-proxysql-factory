# Wspolny zbior VM floty isa (PVE): warstwa wspoldzielona i klastry konsumentow.
#
# Modul jest jedynym miejscem definicji `proxmox_virtual_environment_vm` w repo.
# Wczesniej kazdy root mial wlasna kopie tego zasobu i roznice miedzy kopiami
# (file_format, aio, flagi destroy) rozjezdzaly sie po kazdym nowym klastrze.
#
# Roznice platformowe rootow sa parametrami, nie odrebna logika:
#   - Rocky 10 (shared, finalclaude-r10): snippet cloud-init + scsi1 + os_type,
#   - Rocky 9 (newclaude16-r9): bez snippetu, domyslny ide2, raw/io_uring.
#
# Atrybuty Optional+Computed przekazywane jako null (file_format, aio, purge_
# on_destroy itd.) sa dla providera rownowazne z pominieciem: wartosc zostaje
# ze stanu, wiec migracja rootow na modul nie generuje fikcyjnych diffow.
terraform {
  required_version = ">= 1.5"
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "0.111.1"
    }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = var.vms
  name        = each.key
  node_name   = var.node_name
  vm_id       = each.value.id
  pool_id     = var.pool_id
  tags        = concat(var.tags, [each.value.role])
  description = "${var.description_prefix} (${each.value.role}) ${var.description_dash} VMID ${each.value.id}"

  # F2 instaluje i wlacza qemu-guest-agent; provider nie czeka na raport IP,
  # bo adresy sa statyczne, a agent moze wystartowac dopiero po restarcie VM.
  agent {
    enabled = true
    type    = "virtio"
    wait_for_ip { disabled = true }
  }

  started         = var.started
  stop_on_destroy = var.stop_on_destroy
  # Flagi destroy: jesli zdefiniowane per-maszyna, uzywamy jej wlasnej wartosci,
  # w przeciwnym razie wartosci ze zmiennych modulu.
  purge_on_destroy                     = try(each.value.purge_on_destroy, null) != null ? each.value.purge_on_destroy : var.purge_on_destroy
  delete_unreferenced_disks_on_destroy = try(each.value.delete_unreferenced_disks_on_destroy, null) != null ? each.value.delete_unreferenced_disks_on_destroy : var.delete_unreferenced_disks_on_destroy
  cpu {
    type    = "host"
    cores   = each.value.cpu
    sockets = 1
  }
  memory { dedicated = each.value.ram }

  # pve_vm_set uzywa wylacznie cloud-image (import_from); bez import_from cala
  # maszyna powstaje z pustego dysku i cloud-init nie ma czego uruchomic.
  disk {
    datastore_id = var.storage
    interface    = "virtio0"
    import_from  = var.source_img
    size         = each.value.disk
    discard      = "on"
    file_format  = var.disk_file_format
    aio          = var.disk_aio
  }

  # Rocky 9 (state z pustym blokiem operating_system) nie ustawia os_type,
  # wiec blok musi fizycznie zniknac z konfiguracji — dynamiczna obecnosc,
  # nie null w srodku bloku.
  dynamic "operating_system" {
    for_each = var.os_type == null ? [] : [var.os_type]
    content {
      type = operating_system.value
    }
  }

  initialization {
    interface    = var.init_interface
    datastore_id = var.storage
    # Rocky 10 GenericCloud ma disable_root: true w domyslnym cloud.cfg — klucz
    # z user_account nie trafia do /root/.ssh/authorized_keys. Rooty Rocky 10
    # przekazuja snippet wymuszajacy klucz operatora i konfiguracje sshd.
    user_data_file_id = var.user_data_file_id
    user_account {
      username = "root"
      keys     = [var.ssh_pubkey]
    }
    ip_config {
      ipv4 {
        address = "${var.ip_prefix}${each.value.ip}/24"
        gateway = var.gateway
      }
    }
    dns { servers = var.dns_servers }
  }

  network_device { bridge = var.bridge }
}
