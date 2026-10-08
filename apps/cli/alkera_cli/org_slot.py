"""What one org slot owns on a box: the contract the supervisor, which hands
slots out (``alkera_cli.supervisor.slots``), and the org worker that runs in
one both read.

Slot ``n`` owns a host uid/gid range ``[ORG_UID_BASE + n * ORG_UID_SPAN, ...
+ ORG_UID_SPAN)``, which the org's user namespace maps from its own
``0..ORG_UID_SPAN``, its data root ``<orgs root>/<n>``, and a /30 of
:data:`ORG_NET` for the link between the host and its network namespace.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from alkera_core.compute import box_egress, host_forward

from alkera_cli.account.org_id import MalformedOrgIdError, canonical_org_id

#: The first host id an org range starts at: far above the distros' own
#: subordinate ranges (100000 and up, per login user) and the box's own chat
#: range, below the 32-bit limit for every slot this box can hold.
ORG_UID_BASE: Final = 10_000_000
#: The ids one org owns: a full 16-bit namespace, as container runtimes give.
ORG_UID_SPAN: Final = 65_536
#: The most orgs one box serves at once over its life (bounded by the id space).
MAX_SLOTS: Final = 4_096
#: The block the host-to-org links are carved from, one /30 per slot. Apart
#: from the chat block (10.200.0.0/14), which each worker reuses inside its own
#: namespace behind its own NAT.
ORG_NET: Final = ipaddress.IPv4Network("10.204.0.0/16")
#: The prefix of the host end of every org's link; the box firewall keys its
#: org rules on it.
ORG_VETH_PREFIX: Final = "vo"


def open_org_links(env: Mapping[str, str]) -> tuple[str, ...]:
    """Let the org links through every firewall the host runs besides the box
    table (:func:`~alkera_core.compute.host_forward.open_links`); a host
    without one is left as it is. Again at every heartbeat: a firewall that
    restarted (Docker's, ufw's, firewalld's) rebuilds its chains."""
    return host_forward.open_links(env, (ORG_VETH_PREFIX,))


def prepare_org_links(env: Mapping[str, str]) -> None:
    """Before any worker starts: the private addresses its link may reach
    (:func:`~alkera_core.compute.box_egress.apply_allowlist`), and the link
    let through the host's own firewalls."""
    box_egress.apply_allowlist(env)
    open_org_links(env)


class SlotError(RuntimeError):
    """The table cannot give an org a slot, or is not what it should be."""


@dataclass(frozen=True, slots=True)
class Slot:
    """What one slot owns on the host."""

    index: int
    org_id: str

    @property
    def uid_base(self) -> int:
        """The host id the org's namespace maps its root to."""
        return ORG_UID_BASE + self.index * ORG_UID_SPAN

    @property
    def uid_range(self) -> range:
        return range(self.uid_base, self.uid_base + ORG_UID_SPAN)

    @property
    def host_if(self) -> str:
        return f"{ORG_VETH_PREFIX}{self.index}"

    @property
    def host_ip(self) -> str:
        return str(ORG_NET.network_address + self.index * 4 + 1)

    @property
    def worker_ip(self) -> str:
        return str(ORG_NET.network_address + self.index * 4 + 2)


def canonical_org(raw: str) -> str:
    """The org id a slot is keyed by, in its one spelling
    (:func:`~alkera_cli.account.org_id.canonical_org_id`), so one org can never
    take two slots because two reads spelled it differently. A value that is
    not an org id is a :class:`SlotError`, the table's own refusal."""
    try:
        return canonical_org_id(raw)
    except MalformedOrgIdError as exc:
        raise SlotError(str(exc)) from exc


__all__ = [
    "MAX_SLOTS",
    "ORG_NET",
    "ORG_UID_BASE",
    "ORG_UID_SPAN",
    "ORG_VETH_PREFIX",
    "Slot",
    "SlotError",
    "canonical_org",
    "open_org_links",
    "prepare_org_links",
]
