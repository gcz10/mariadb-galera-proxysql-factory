# Wejscia modulu pve_vm_set. Konwencja:
#   - required topologia: wezel, pula, storage, bridge, adresacja i DNS sa
#     deklaracja roota. Modul nie ma dla nich wartosci domyslnej — domyslna
#     bylaby topologia jednego laboratorium wpisana w fabryke wielokrotnego
#     uzytku (ISC-59 pilnuje tego skanem po `terraform/modules`),
#   - required: decyzje wlasne dla roota (obraz, klucz, maszyny, tagi, opis),
#   - default: wartosci niezwiazane z infrastruktura,
#   - default null: atrybuty Optional+Computed providera — null oznacza
#     "nie ustawiaj" i zachowuje wartosc ze stanu (patrz main.tf).

variable "vms" {
  description = "Maszyny zbioru: klucz = nazwa VM w PVE. id to VMID, ip to pelny adres IPv4 z prefiksem sieci (CIDR)."
  type = map(object({
    id                                   = number
    ip                                   = string
    role                                 = string
    cpu                                  = number
    ram                                  = number
    disk                                 = number
    purge_on_destroy                     = optional(bool)
    delete_unreferenced_disks_on_destroy = optional(bool)
  }))

  # Adres jest pelnym CIDR-em, nie oktetem doklejonym do prefiksu sieci modulu:
  # dzieki temu zbior stoi na dowolnej podsieci i dowolnym prefiksie. Walidacja
  # zamienia literowke w adresie na blad planu, a nie na blad API hypervisora —
  # wczesniejszy typ `number` dawal to za darmo (oktet nie mogl byc niepoprawny).
  validation {
    condition = alltrue([
      for vm in var.vms : can(cidrhost(vm.ip, 0)) && can(cidrnetmask(vm.ip))
    ])
    error_message = "Kazdy `ip` musi byc adresem IPv4 w notacji CIDR, np. 192.0.2.10/24."
  }
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
  description = "Wezel hypervisora PVE, na ktorym powstaja maszyny zbioru."
  type        = string
}

variable "pool_id" {
  description = "Pula PVE grupujaca zbior (kazda maszyna zbioru trafia do tej puli)."
  type        = string
}

variable "storage" {
  description = "Datastore na dyski VM i snippet cloud-init."
  type        = string
}

variable "bridge" {
  description = "Mostek sieciowy VM."
  type        = string
}

variable "gateway" {
  description = "Brama domyslna maszyn zbioru (ta sama dla calego zbioru — zbior stoi na jednej sieci)."
  type        = string
}

variable "dns_servers" {
  description = "Serwery DNS wypychane przez cloud-init; jawna decyzja roota, nie domysl publiczny."
  type        = list(string)
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
