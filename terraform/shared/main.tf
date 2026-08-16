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

# WARSTWA WSPOLNA floty finalclaude — NIE jest klastrem.
#
# Zawiera dokladnie to, co jest wspoldzielone przez wszystkie klastry Galera,
# obecne i przyszle:
#   - infra: PMM + MinIO + maildev
#   - para ProxySQL w HA + VIP (Keepalived)
#
# DLACZEGO OSOBNY MODUL: w poprzedniej flocie te maszyny nalezaly do modulu
# klastra `claude-r10b`, a korzystaly z nich takze `claude-r10c` i `claude-r9t`.
# Skutek: uruchomienie f7_proxysql/f8_keepalived z inventarza wlasciciela
# przepinalo zywy VIP na jego wlasne, dawno wylaczone wezly Galera. Warstwa
# wspoldzielona nie moze byc wlasnoscia zadnego klastra — stad ten katalog.
#
# Cykl zycia: `terraform destroy` tutaj kladzie WSZYSTKIE klastry naraz.
locals {
  node_name = "pve"
  pool_id   = "claude-isa"
  storage   = "local-zfs"
  bridge    = "vmbr0"

  source_img = "local:import/Rocky-10.2-GenericCloud.qcow2"

  ssh_pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEi2JptnezdY/Nyec+JtsKltgffUiJICpRkUS4LHB/1m ansible-lab"
  gateway    = "192.168.1.1"

  # Adresacja floty finalclaude: .130-.133 warstwa wspolna, .140+ klastry.
  # VIP .133 nie jest maszyna — Keepalived podnosi go na fcp1/fcp2.
  # RAM: limit operatora to 5 GB na VM.
  #
  # `fcinfra` dostaje 5 GB, bo PMM 3.8.1 jest tu jedynym realnym konsumentem:
  # przy ZERZE zarejestrowanych uslug zajmowal juz 1.4 GB z 3 GB, a docelowo
  # dochodzi 5 wezlow z eksporterami i QAN (poprzednia flota dawala mu 8 GB).
  #
  # ProxySQL ma 3 GB, nie 2 GB: preflight (f2_preflight.yml) wymaga minimum
  # 2048 MB mierzonych przez `ansible_memtotal_mb`, a to pamiec WIDZIANA przez
  # system po rezerwach firmware/kernela — przydzial 2048 MB daje 1769 MB i cel
  # sie wywala. Prog trzeba przebic z zapasem, nie trafic w niego dokladnie.
  # `fcapp` to host APLIKACYJNY, nie element klastra: laczy sie do Galery tak,
  # jak zrobilaby to aplikacja — przez VIP, po sieci, z wlasna konfiguracja
  # klienta. Powstal, bo ta sesja dwa razy pokazala, ze zdrowy klaster nie
  # znaczy dzialajaca aplikacja: klient MariaDB 11.4 z domyslna weryfikacja
  # certu nie laczy sie przez VIP (auto-cert ProxySQL), a przy utracie kworum
  # aplikacja dostaje "ERROR 2027 malformed packet" zamiast bledu bazy. Oba
  # znaleziono przypadkiem, bo zaden test nie patrzyl z perspektywy klienta.
  #
  # Nalezy do warstwy WSPOLNEJ, nie do klastra: ma przezyc przebudowy klastrow
  # (v8 -> v9 -> ...) i testowac dowolny z nich, tak jak PMM je monitoruje.
  # Zero roli serwerowej — sam klient, zeby nie mylic go z wezlem bazy.
  vms = {
    fcinfra = { id = 9400, ip = 130, role = "infra", cpu = 4, ram = 5120, disk = 80 }
    fcp1    = { id = 9401, ip = 131, role = "proxysql", cpu = 1, ram = 3072, disk = 40 }
    fcp2    = { id = 9402, ip = 132, role = "proxysql", cpu = 1, ram = 3072, disk = 40 }
    fcapp   = { id = 9403, ip = 134, role = "app", cpu = 2, ram = 3072, disk = 40 }
  }
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each    = local.vms
  name        = each.key
  node_name   = local.node_name
  vm_id       = each.value.id
  pool_id     = local.pool_id
  tags        = ["finalclaude", "shared", "rocky10", each.value.role]
  description = "finalclaude warstwa wspolna (${each.value.role}) — VMID ${each.value.id}"

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
    # Rocky 10 GenericCloud ma disable_root: true w domyslnym cloud.cfg — klucz
    # z user_account nie trafia do /root/.ssh/authorized_keys. Snippet wymusza
    # klucz operatora i konfiguracje sshd.
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
output "vip" { value = "192.168.1.133" }
output "pmm_url" { value = "https://192.168.1.130" }
output "s3_endpoint" { value = "192.168.1.130:9000" }
