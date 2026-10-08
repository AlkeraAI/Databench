"""The box firewall's half of the sandbox: the addresses it guards and the
nftables rules it keeps for the chat uids and their network namespaces.
The node's sandbox prerequisites script carries the same rule text; the
shape test holds the two together."""

from __future__ import annotations

from collections.abc import Mapping

from alkera_core.compute import box_egress, host_forward

from alkera_cli.harness.sandbox_uid import UID_MAX, UID_MIN

#: The address block the per-chat network namespaces are carved from: one /30
#: per chat uid (host end, container end), which the whole uid range fits in.
DEFAULT_CHAT_NET = "10.200.0.0/14"
#: The cloud instance metadata service. The instance user data carries the
#: machine credential, so no chat may reach it. The box firewall drops it for the
#: chat uid range and on the forward path from every chat namespace, and
#: gVisor's own network stack has no route to it.
METADATA_IPV4 = "169.254.169.254"
METADATA_IPV6 = "fd00:ec2::254"

#: The prefix of the host end of every chat's veth pair; the box firewall
#: matches ``vc*`` for what may come in from a chat and what may be forwarded.
VETH_PREFIX = "vc"
#: The nftables table and the set the box firewall keeps: which (interface,
#: port) pairs a chat may reach on the host — its own daemon's tool server and
#: nothing else.
NFT_TABLE = "inet alkera_sandbox"
NFT_CHAT_PORTS_SET = "chat_ports"


def metadata_block_rules() -> tuple[str, str]:
    """The nftables rules that keep every chat uid away from the instance
    metadata service (v4 and v6). The sandbox prerequisites script carries the
    same text; the shape test holds the two together. gVisor's own netstack has
    no route to the metadata service either, so this is defence in depth and the
    only metadata boundary a ``none`` box has."""
    skuid = f"meta skuid {UID_MIN}-{UID_MAX}"
    return (
        f"{skuid} ip daddr {METADATA_IPV4} counter drop",
        f"{skuid} ip6 daddr {METADATA_IPV6} counter drop",
    )


def chat_uid_egress_rules() -> tuple[str, ...]:
    """The box egress policy for chat uids on the host's own network
    (:func:`alkera_core.compute.box_egress.output_rules`), after the metadata
    drops in the same output chain."""
    return box_egress.output_rules(UID_MIN, UID_MAX)


def chat_network_rules(chat_net: str = DEFAULT_CHAT_NET) -> tuple[str, ...]:
    """The nftables rules the box keeps for the chat namespaces, in the order
    they must appear: on the input path a chat's host end reaches only the
    (interface, port) pairs granted in the ``chat_ports`` set and nothing else
    on the host; on the forward path a chat never reaches the metadata service
    or another chat's block, and otherwise goes out; on the way out its
    address is masqueraded as the host's. A private, loopback, link-local or
    reserved address is reached only through the box's egress allowlist
    (:mod:`alkera_core.compute.box_egress`). The sandbox prerequisites script
    carries the same text; the shape test holds the two together."""
    veth = f'"{VETH_PREFIX}*"'
    return (
        f"iifname . tcp dport @{NFT_CHAT_PORTS_SET} accept",
        f"iifname {veth} counter drop",
        f"iifname {veth} ip daddr {METADATA_IPV4} counter drop",
        f"iifname {veth} ip6 daddr {METADATA_IPV6} counter drop",
        f"iifname {veth} ip daddr {chat_net} counter drop",
        *box_egress.forward_rules(f"{VETH_PREFIX}*"),
        f"iifname {veth} oifname != {veth} accept",
        f"ip saddr {chat_net} oifname != {veth} masquerade",
    )


def open_chat_links(env: Mapping[str, str]) -> tuple[str, ...]:
    """Let the chats' links through every firewall the host runs besides the
    box table (:func:`~alkera_core.compute.host_forward.open_links`); a host
    without one is left as it is."""
    return host_forward.open_links(env, (VETH_PREFIX,))


__all__ = [
    "DEFAULT_CHAT_NET",
    "METADATA_IPV4",
    "METADATA_IPV6",
    "NFT_CHAT_PORTS_SET",
    "NFT_TABLE",
    "VETH_PREFIX",
    "chat_network_rules",
    "chat_uid_egress_rules",
    "metadata_block_rules",
    "open_chat_links",
]
