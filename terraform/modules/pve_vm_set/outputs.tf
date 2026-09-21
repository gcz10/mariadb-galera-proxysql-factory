output "vms" {
  description = "Parametry maszyn zbioru: vmid, ip (adres bez maski — kontrakt odbiorcow `terraform output`), rola, cpu, ram_mb, disk_gb, flagi destroy."
  value = { for k, v in var.vms : k => {
    vmid = v.id
    # Wejscie niesie CIDR (do ip_config), a output zwraca sam adres — ksztalt
    # `terraform output -json vms` pozostaje taki sam jak przed migracja,
    # chociaz zapis maszyn przeszedl na pelne CIDR-y.
    ip                                   = split("/", v.ip)[0]
    role                                 = v.role
    cpu                                  = v.cpu
    ram_mb                               = v.ram
    disk_gb                              = v.disk
    purge_on_destroy                     = try(v.purge_on_destroy, null) != null ? v.purge_on_destroy : var.purge_on_destroy
    delete_unreferenced_disks_on_destroy = try(v.delete_unreferenced_disks_on_destroy, null) != null ? v.delete_unreferenced_disks_on_destroy : var.delete_unreferenced_disks_on_destroy
  } }
}
