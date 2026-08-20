output "vms" {
  description = "Parametry maszyn zbioru: vmid, ip, rola, cpu, ram_mb, disk_gb."
  value = { for k, v in var.vms : k => {
    vmid    = v.id
    ip      = "${var.ip_prefix}${v.ip}"
    role    = v.role
    cpu     = v.cpu
    ram_mb  = v.ram
    disk_gb = v.disk
  } }
}
