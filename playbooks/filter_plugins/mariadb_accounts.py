"""Adresy zrodel kont sieciowych MariaDB -> wartosci kolumny `Host`.

Konta logujace sie po sieci (aplikacja przez ProxySQL, monitor ProxySQL,
monitoring PMM) maja host rowny ADRESOM, z ktorych faktycznie sie lacza,
a nie '%'. Przy `skip_name_resolve=ON` kolumna Host moze zawierac wylacznie
adresy IP albo `localhost` (MariaDB: server-system-variables, skip_name_resolve),
wiec filtr przyjmuje tylko adresy i sieci, nigdy nazwy ani wzorce LIKE.

  192.0.2.11        -> 192.0.2.11
  192.0.2.10/32     -> 192.0.2.10
  198.51.100.0/24   -> 198.51.100.0/255.255.255.0   (forma base_ip/netmask)

Siec IPv6 szersza niz jeden adres jest odrzucana: MariaDB nie stosuje masek
do IPv6 (create-user.md, "Host Name Component"). Zly albo pusty wpis konczy
render bledem, zamiast cicho poszerzyc albo odebrac dostep.
"""

import ipaddress

from ansible.errors import AnsibleFilterError


def mariadb_account_hosts(sources):
    if isinstance(sources, (str, bytes)) or not sources:
        raise AnsibleFilterError(
            f"mariadb_account_hosts: oczekiwana niepusta lista adresow, jest {sources!r}"
        )
    hosts = []
    for raw in sources:
        text = str(raw).strip()
        try:
            network = ipaddress.ip_network(text, strict=True)
        except ValueError as exc:
            raise AnsibleFilterError(
                f"mariadb_account_hosts: {text!r} nie jest adresem ani siecia IP ({exc})"
            ) from exc
        if network.num_addresses == 1:
            host = str(network.network_address)
        elif network.version == 4:
            host = f"{network.network_address}/{network.netmask}"
        else:
            raise AnsibleFilterError(
                f"mariadb_account_hosts: {text!r} — MariaDB nie obsluguje masek IPv6; "
                "podaj pojedyncze adresy"
            )
        if host not in hosts:
            hosts.append(host)
    return hosts


class FilterModule:
    def filters(self):
        return {"mariadb_account_hosts": mariadb_account_hosts}
