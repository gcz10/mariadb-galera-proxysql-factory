# Wejscia modulu pve_vm_set. Konwencja:
#   - required: decyzje wlasne dla roota (obraz, klucz, maszyny, tagi),
#   - default:  stalone floty isa (wezel PVE, storage, bridge, adresacja),
#   - default null: atrybuty Optional+Computed providera — null oznacza
#     "nie ustawiaj" i zachowuje wartosc ze stanu (patrz main.tf).

variable "vms" {
  description = "Maszyny zbioru: klucz = nazwa VM w PVE. id to VMID, ip to ostatni oktet."
  type = map(object({
    id   = number
    ip   = number
    role = string
    cpu  = number
    ram  = number
    disk = number
  }))
}

variable "source_img" {
  description = "Cloud-image do importu dysku, np. local:import/Rocky-10.2-GenericCloud.qcow2"
  type        = string
}

variable "ssh_pubkey" {
  description = "Klucz publiczny operatora trafiajacy do konta root przez user_account."
  type        = string
}

variable "tags" {
  description = "Tagi klastrowe; modul dokleja role maszyny (porzadek tagow nieznaczacy — provider normalizuje)."
  type        = list(string)
}

variable "description_prefix" {
  description = "Poczatek opisu VM; pelny wzorzec: PREFIX (rola) DASH VMID id."
  type        = string
}

variable "node_name" {
  description = "Wezel hypervisora PVE."
  type        = string
  default     = "pve"
}

variable "pool_id" {
  description = "Pula PVE grupujaca cala flote isa."
  type        = string
  default     = "claude-isa"
}

variable "storage" {
  description = "Datastore na dyski VM i snippet cloud-init."
  type        = string
  default     = "local-zfs"
}

variable "bridge" {
  description = "Mostek sieciowy VM."
  type        = string
  default     = "vmbr0"
}

variable "ip_prefix" {
  description = "Prefiks adresu IPv4 bez ostatniego oktetu (np. 192.168.1.)."
  type        = string
  default     = "192.168.1."
}

variable "gateway" {
  description = "Brama domyslna maszyn zbioru."
  type        = string
  default     = "192.168.1.1"
}

variable "dns_servers" {
  description = "Serwery DNS wypychane przez cloud-init."
  type        = list(string)
  default     = ["1.1.1.1", "8.8.8.8"]
}

variable "description_dash" {
  description = "Separator miedzy rola a VMID w opisie: em-dash dla shared/r10, myslnik dla r9."
  type        = string
  default     = "—"
}

variable "os_type" {
  description = "Typ systemu operacyjnego (l26). Null pominie caly blok — stan Rocky 9 ma blok pusty."
  type        = string
  default     = null
}

variable "init_interface" {
  description = "Interfejs cloud-init. Null = domysl providera (ide2); Rocky 10 uzywa scsi1."
  type        = string
  default     = null
}

variable "user_data_file_id" {
  description = "Snippet cloud-init (user-data). Null = bez snippetu (Rocky 9 nie potrzebuje)."
  type        = string
  default     = null
}

variable "disk_file_format" {
  description = "Format pliku dysku; null zachowuje wartosc ze stanu."
  type        = string
  default     = null
}

variable "disk_aio" {
  description = "Tryb AIO dysku; null zachowuje wartosc ze stanu."
  type        = string
  default     = null
}

variable "started" {
  description = "Czy VM startuje po utworzeniu."
  type        = bool
  default     = true
}

variable "stop_on_destroy" {
  description = "Zatrzymaj VM przed usunieciem (zamiast twardego destroy)."
  type        = bool
  default     = true
}

variable "purge_on_destroy" {
  description = "Usun tez metadane PVE (napisy, zadania). Null zachowuje wartosc ze stanu."
  type        = bool
  default     = null
}

variable "delete_unreferenced_disks_on_destroy" {
  description = "Usun dyski niejasne z konfiguracji. Null zachowuje wartosc ze stanu."
  type        = bool
  default     = null
}
