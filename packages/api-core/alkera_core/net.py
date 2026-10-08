"""Network address classification shared by every surface that must be honest
about reachability.

The gate's CI leg and the GitHub App's webhook deliveries both originate
OUTSIDE the deployment (a GitHub-hosted runner, GitHub's webhook servers), so
an API URL that only resolves inside this machine or this network can never
receive them. :func:`is_private_host` is the one rule the backend status
endpoint, the CLI's upload hints, and the generated-workflow warnings agree
on, so "works on my machine, silent in CI" cannot happen without a warning.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

#: Suffixes that never resolve on the public internet: mDNS (.local), the
#: cloud-internal convention (.internal), and explicit loopback names.
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal")


def is_private_host(url: str) -> bool:
    """Whether ``url`` points at a host the public internet cannot reach:
    loopback, RFC-1918/link-local/unique-local addresses, mDNS or internal
    name suffixes, or a bare hostname with no domain. A URL whose host cannot
    be parsed counts as private (it is certainly not publicly reachable)."""
    host = urlsplit(url).hostname
    if not host:
        return True
    host = host.lower().rstrip(".")
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return True
    return "." not in host


__all__ = ["is_private_host"]
