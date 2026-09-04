output "vms" {
  description = "Parametry maszyn zbioru: vmid, ip, rola, cpu, ram_mb, disk_gb, flagi destroy."
  value = { for k, v in var.vms : k => {
    vmid                                 = v.id
    ip                                   = "${var.ip_prefix}${v.ip}"
    role                                 = v.role
    cpu                                  = v.cpu
    ram_mb                               = v.ram
    disk_gb                              = v.disk
    purge_on_destroy                     = try(v.purge_on_destroy, null)
    delete_unreferenced_disks_on_destroy = try(v.delete_unreferenced_disks_on_destroy, null)
  } }
}
