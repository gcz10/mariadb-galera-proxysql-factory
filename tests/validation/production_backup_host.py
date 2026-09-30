#!/usr/bin/env python3
"""Verify a production backup destination against declared source identities.

The schema validator uses the structural checks without DNS. The remote
preflight enables DNS on the database host before deployment mutations.
Only public endpoint/source identities are accepted; never credentials.
"""

import ipaddress
import json
import socket
import sys
from urllib.parse import urlsplit


def endpoint_host(destination, endpoint):
    if destination not in ("s3", "smb"):
        return None
    value = str(endpoint).strip()
    if destination == "smb":
        if not value.startswith("//"):
            return None
        value = "smb:" + value
    elif "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme != ("smb" if destination == "smb" else "https"):
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        return parsed.hostname.lower().rstrip(".") if parsed.hostname else None
    except ValueError:
        return None


def normalized_address(value):
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return None
    return address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address


def off_host_errors(destination, endpoint, source_names, source_addresses, resolve=False):
    host = endpoint_host(destination, endpoint)
    if not host:
        return ["production backup requires a remote S3 or SMB endpoint without embedded credentials"]
    names = {str(name).lower().rstrip(".") for name in source_names if name}
    sources = {address for value in source_addresses if (address := normalized_address(value)) is not None}
    if not sources:
        return ["production backup off-host verification requires database source addresses"]
    if host == "localhost" or host.endswith(".localhost") or host in names:
        return ["production backup destination is a database source host or localhost"]
    direct = normalized_address(host)
    addresses = {direct} if direct is not None else set()
    if resolve and direct is None:
        try:
            addresses = {address for record in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
                         if (address := normalized_address(record[4][0])) is not None}
        except OSError:
            return ["production backup endpoint DNS resolution failed; off-host location is unverified"]
        if not addresses:
            return ["production backup endpoint DNS returned no usable addresses"]
    if any(address.is_loopback or address.is_unspecified or address in sources for address in addresses):
        return ["production backup endpoint resolves to a database source address or loopback"]
    return []


def main():
    try:
        data = json.load(sys.stdin)
        errors = off_host_errors(data["destination"], data["endpoint"],
                                 data["source_names"], data["source_addresses"], resolve=True)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        print("FAIL: malformed production backup location preflight input", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print("FAIL: " + error, file=sys.stderr)
        return 1
    print("PASS: production backup endpoint is outside the database source identities")
    return 0


if __name__ == "__main__":
    sys.exit(main())
