"""Whether a provider's machines can reach this deployment.

A rented machine (a RunPod pod, an EC2 instance) runs elsewhere and calls the
deployment back at ``settings.node_api_url``: its claim, its heartbeat, every
chat. A deployment whose address is loopback or private (a developer's
laptop with no tunnel) rents a machine that bills from its first minute and
can never connect: it sits on "Installing Alkera" until the boot timeout
ends it. :func:`callback_refusal` says so before anything is bought or
started. Whether a provider's machines call back from outside is its boot
profile's to say (``NodeBootProfile.callback``,
:mod:`alkera_core.compute.bootstrap`); a provider that runs beside the
deployment (``localdev``, ``callback="host"``) is never refused for it.

Only the address itself is read, never resolved: a name is taken as public
unless it is ``localhost``, under ``.localhost`` or ``.local``, or a
loopback, private or link-local address.
"""

from __future__ import annotations

import ipaddress
from typing import Final
from urllib.parse import urlsplit

from alkera_core.compute.bootstrap import BOOT_PROFILES

#: What the buyer reads when a rented machine could not connect back.
NO_PUBLIC_ADDRESS: Final = (
    "This deployment has no public address, so a rented machine could not connect back."
)

_LOCAL_SUFFIXES: Final = (".localhost", ".local", ".internal")


#: The setting that names the address remote machines use to reach this server.
NODE_API_URL_SETTING: Final = "ALKERA_NODE_API_URL"
#: The setting that names the model gateway remote machines' chats go through.
NODE_GATEWAY_URL_SETTING: Final = "ALKERA_NODE_GATEWAY_URL"


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").strip().lower().rstrip(".")


def is_loopback_address(url: str) -> bool:
    """Whether ``url`` names the machine it is read on (``localhost``, a
    loopback or unspecified address, or no host at all). On a remote host such
    an address is the host itself, never this deployment."""
    host = _host_of(url)
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def loopback_callback_refusal(urls: dict[str, str]) -> str | None:
    """Why a host attached over SSH could never reach this deployment back at
    one of ``urls`` (setting name to address), or ``None``. Only a loopback
    address is refused here: a private one may well be routable from the host,
    which the probe over SSH then says."""
    for setting, url in urls.items():
        if is_loopback_address(url):
            return (
                f"Machines are told to reach this server at {url}, which on the host is "
                f"the host itself. Set {setting} to an address the host can reach."
            )
    return None


def is_public_address(url: str) -> bool:
    """Whether a machine elsewhere could reach ``url``, as far as the address
    alone can say."""
    host = _host_of(url)
    if not host or host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
    )


def callback_refusal(kind: str, node_api_url: str) -> str | None:
    """Why a machine of provider ``kind`` cannot be bought or started here,
    or ``None`` when it can be. A provider with no boot profile renders no
    bootstrap and is refused where its launch is, not here."""
    profile = BOOT_PROFILES.get(kind)
    if profile is None or not profile.needs_public_callback:
        return None
    if is_public_address(node_api_url):
        return None
    return NO_PUBLIC_ADDRESS


__all__ = [
    "NODE_API_URL_SETTING",
    "NODE_GATEWAY_URL_SETTING",
    "NO_PUBLIC_ADDRESS",
    "callback_refusal",
    "is_loopback_address",
    "is_public_address",
    "loopback_callback_refusal",
]
