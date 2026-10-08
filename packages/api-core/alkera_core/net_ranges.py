"""The address ranges no fetch on the platform's behalf may reach unasked:
private, loopback, link-local, reserved, and the cloud metadata services.

Standard library only, so every process that draws a network boundary reads
the same table: a server-side fetch (:mod:`alkera_core.egress`) and the box
firewall that keeps chats and org workers off the box's private network
(:mod:`alkera_core.compute.box_egress`).
"""

from __future__ import annotations

import ipaddress
from typing import Final

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

#: Stated explicitly rather than left to ``is_global`` alone: the table is the
#: contract, ``is_global`` is the net under it for ranges a future registry adds.
DENIED: Final[tuple[tuple[IPNetwork, str], ...]] = tuple(
    (ipaddress.ip_network(cidr), label)
    for cidr, label in (
        ("0.0.0.0/8", "unspecified"),
        ("10.0.0.0/8", "private"),
        ("100.64.0.0/10", "shared (CGNAT)"),
        ("127.0.0.0/8", "loopback"),
        ("169.254.0.0/16", "link-local"),
        ("172.16.0.0/12", "private"),
        ("192.0.0.0/24", "reserved"),
        ("100.100.100.200/32", "cloud metadata"),
        ("168.63.129.16/32", "cloud metadata"),
        ("192.0.2.0/24", "documentation"),
        ("192.88.99.0/24", "reserved"),
        ("192.168.0.0/16", "private"),
        ("198.18.0.0/15", "benchmark"),
        ("198.51.100.0/24", "documentation"),
        ("203.0.113.0/24", "documentation"),
        ("224.0.0.0/4", "multicast"),
        ("240.0.0.0/4", "reserved"),
        ("::/128", "unspecified"),
        ("::1/128", "loopback"),
        ("100::/64", "discard"),
        ("2001:db8::/32", "documentation"),
        ("2001::/32", "Teredo tunnel"),
        ("2002::/16", "6to4 relay"),
        ("fc00::/7", "private"),
        ("fe80::/10", "link-local"),
        ("ff00::/8", "multicast"),
    )
)

#: Ranges no allowlist may open: the instance-metadata services live here, and
#: they hand out the machine's own credentials to anyone who can GET them. The
#: link-local range (AWS, GCP, OpenStack, most clouds) and the per-provider
#: addresses outside it: Azure's wire server, Alibaba's and Oracle's metadata.
NEVER_ALLOWED: Final[tuple[IPNetwork, ...]] = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("168.63.129.16/32"),
    ipaddress.ip_network("100.100.100.200/32"),
    ipaddress.ip_network("192.0.0.192/32"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::/32"),
)


__all__ = ["DENIED", "NEVER_ALLOWED", "IPNetwork"]
