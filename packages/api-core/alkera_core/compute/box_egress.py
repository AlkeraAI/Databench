"""The box's egress policy: what a chat or an org worker may reach off the box.

A chat sandbox (its namespace's ``vc*`` link, or a chat uid on a ``none`` box)
and an org worker (its ``vo*`` link) go out through the box firewall
(``sandbox_prereqs.sh``). Public addresses are theirs to reach. Every range in
:data:`~alkera_core.net_ranges.DENIED` (private networks, the VPC, loopback,
link-local with the metadata service, reserved space) is dropped, but for the
few (address, protocol, port) entries the box itself needs: the backend, the
model gateway, any endpoint the deployment names, and the box's own DNS
resolvers on port 53. Those live in a set the box fills at start
(:func:`apply_allowlist`); the deny sets are written into the ruleset itself,
so a box that never fills the allowlist fails closed.

This module is the one owner of that policy. The ruleset in
``sandbox_prereqs.sh`` carries the text :func:`deny_set_lines`,
:func:`forward_rules` and :func:`output_rules` render, and a test holds the two
together. The metadata service is never allowed, whatever an endpoint
resolves to.

A host that also runs a firewall of its own (Docker's, ufw, firewalld) can
drop a link's traffic whatever this table accepts; the node lets its links
through each of those in :mod:`alkera_core.compute.host_forward`.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from alkera_core.net_ranges import DENIED, NEVER_ALLOWED, IPNetwork
from alkera_core.process import SpawnSpec, run

logger = logging.getLogger(__name__)

#: The nftables table every box rule lives in, and the four sets of this policy.
NFT_TABLE: Final = ("inet", "alkera_sandbox")
DENY_V4: Final = "egress_deny_v4"
DENY_V6: Final = "egress_deny_v6"
ALLOW_V4: Final = "egress_allow_v4"
ALLOW_V6: Final = "egress_allow_v6"
#: The metadata service's addresses: never allowed, not even for DNS.
METADATA_ADDRESSES: Final = frozenset({"169.254.169.254", "fd00:ec2::254"})
#: Endpoints beyond the backend and the gateway a deployment's boxes reach on a
#: private address (a Files store inside the VPC, say), comma-separated URLs.
ENV_EXTRA_ENDPOINTS: Final = "ALKERA_BOX_EGRESS_ALLOW"
#: Set in every node's environment; a process without it is not on a box.
ENV_SANDBOX_MODE: Final = "ALKERA_SANDBOX_MODE"
_ENDPOINT_ENVS: Final = ("ALKERA_API_URL", "ALKERA_GATEWAY_URL")
_DEFAULT_PORTS: Final = {"http": 80, "https": 443, "ws": 80, "wss": 443}
#: Where the box's own resolvers are named (systemd-resolved's upstream first).
RESOLV_CONF_PATHS: Final = ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf")

Proto = Literal["tcp", "udp"]
Resolve = Callable[[str, int], Sequence[str]]


def _networks(version: int) -> list[IPNetwork]:
    return [n for n, _ in DENIED if n.version == version]


def deny_elements(version: Literal[4, 6]) -> str:
    """The deny set's elements, merged where they overlap (an nftables
    interval set refuses overlapping elements)."""
    merged = ipaddress.collapse_addresses(_networks(version))  # type: ignore[type-var]
    return ", ".join(str(n) for n in merged)


def deny_set_lines() -> tuple[str, ...]:
    """The four set declarations, the deny sets with their elements."""
    return (
        f"set {DENY_V4} {{ type ipv4_addr; flags interval; elements = {{ {deny_elements(4)} }} }}",
        f"set {DENY_V6} {{ type ipv6_addr; flags interval; elements = {{ {deny_elements(6)} }} }}",
        f"set {ALLOW_V4} {{ type ipv4_addr . inet_proto . inet_service; }}",
        f"set {ALLOW_V6} {{ type ipv6_addr . inet_proto . inet_service; }}",
    )


def _allow_then_deny(match: str) -> tuple[str, ...]:
    return (
        f"{match} ip daddr . meta l4proto . th dport @{ALLOW_V4} accept",
        f"{match} ip6 daddr . meta l4proto . th dport @{ALLOW_V6} accept",
        f"{match} ip daddr @{DENY_V4} counter drop",
        f"{match} ip6 daddr @{DENY_V6} counter drop",
    )


def forward_rules(link_glob: str) -> tuple[str, ...]:
    """What traffic forwarded from a ``link_glob`` link (``vc*`` for chats,
    ``vo*`` for org workers) may reach, in order, after the metadata drops."""
    return _allow_then_deny(f'iifname "{link_glob}"')


def output_rules(uid_min: int, uid_max: int) -> tuple[str, ...]:
    """The same policy for chat uids on the host's own network (a ``none``
    box): their loopback stays theirs (the daemon's tool server is there)."""
    skuid = f"meta skuid {uid_min}-{uid_max}"
    return (f'{skuid} oif "lo" accept', *_allow_then_deny(skuid))


@dataclass(frozen=True, slots=True, order=True)
class AllowEntry:
    """One (address, protocol, port) a sandbox may reach in a denied range."""

    address: str
    proto: Proto
    port: int

    @property
    def version(self) -> int:
        return ipaddress.ip_address(self.address).version

    def element(self) -> str:
        return f"{self.address} . {self.proto} . {self.port}"


def _denied(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return any(ip in n for n, _ in DENIED if n.version == ip.version)


def _never(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return any(ip in n for n in NEVER_ALLOWED if n.version == ip.version)


def system_resolve(host: str, port: int) -> Sequence[str]:
    """Every address ``host`` resolves to, or none."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return ()
    return tuple(sorted({str(info[4][0]).split("%", 1)[0] for info in infos}))


def allow_entries(
    endpoints: Sequence[str], resolvers: Sequence[str], *, resolve: Resolve = system_resolve
) -> tuple[AllowEntry, ...]:
    """The entries the box needs: each endpoint URL's addresses on its port
    (TCP), and each resolver on port 53 (UDP and TCP). Only addresses in a
    denied range need an entry. An endpoint never opens a link-local or
    metadata range; a resolver may sit in link-local (some clouds put it
    there) but is never the metadata service."""
    found: set[AllowEntry] = set()
    for url in endpoints:
        parts = urlsplit(url.strip())
        if not parts.hostname:
            continue
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme, 443)
        for address in resolve(parts.hostname, port):
            if _denied(address) and not _never(address):
                found.add(AllowEntry(address, "tcp", port))
    for server in resolvers:
        try:
            address = str(ipaddress.ip_address(server))
        except ValueError:
            continue
        if address in METADATA_ADDRESSES or ipaddress.ip_address(address).is_loopback:
            continue
        if _denied(address):
            found |= {AllowEntry(address, "udp", 53), AllowEntry(address, "tcp", 53)}
    return tuple(sorted(found))


def box_endpoints(env: Mapping[str, str]) -> tuple[str, ...]:
    """The URLs this box itself talks to, from its environment."""
    urls = [env.get(name, "").strip() for name in _ENDPOINT_ENVS]
    urls += [u.strip() for u in env.get(ENV_EXTRA_ENDPOINTS, "").split(",")]
    return tuple(u for u in urls if u)


def box_resolvers(read: Callable[[str], str | None]) -> tuple[str, ...]:
    """The nameservers the first resolver file that names one lists."""
    for path in RESOLV_CONF_PATHS:
        text = read(path)
        if not text:
            continue
        servers = [
            parts[1]
            for line in text.splitlines()
            if len(parts := line.split()) >= 2 and parts[0] == "nameserver"
        ]
        if servers:
            return tuple(servers)
    return ()


def allowlist_script(entries: Sequence[AllowEntry]) -> str:
    """The nft input that replaces both allow sets with ``entries``, as one
    transaction: the sets are never seen half-filled."""
    family, table = NFT_TABLE
    lines = [f"flush set {family} {table} {ALLOW_V4}", f"flush set {family} {table} {ALLOW_V6}"]
    for name, version in ((ALLOW_V4, 4), (ALLOW_V6, 6)):
        elements = [e.element() for e in entries if e.version == version]
        if elements:
            lines.append(f"add element {family} {table} {name} {{ {', '.join(elements)} }}")
    return "\n".join(lines) + "\n"


#: Where the prerequisites keep the ruleset, and what the host's boot config
#: (/etc/nftables.conf) includes it by.
NFT_FILE: Final = "/etc/alkera/sandbox.nft"
NFTABLES_CONF: Final = "/etc/nftables.conf"
#: The forwarding the prerequisites persist.
SYSCTL_FILE: Final = "/etc/sysctl.d/90-alkera-sandbox.conf"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def apply_allowlist(
    env: Mapping[str, str],
    *,
    resolve: Resolve = system_resolve,
    read: Callable[[str], str | None] = _read,
    nft: str = "nft",
) -> tuple[AllowEntry, ...]:
    """Fill the allow sets with what this box needs, and return them. Only on
    a box (its environment names a sandbox mode, as every node's does): a
    developer's own machine has no box ruleset. Failing to (no nft, no
    ruleset) is logged: the deny sets stay and the box's sandboxes simply
    reach no private address."""
    if not env.get(ENV_SANDBOX_MODE, "").strip():
        return ()
    entries = allow_entries(box_endpoints(env), box_resolvers(read), resolve=resolve)
    spec = SpawnSpec(
        argv=(nft, "-f", "-"), env=dict(env), stdin="pipe", stdout="pipe", stderr="pipe"
    )
    try:
        done = run(spec, timeout=30, input=allowlist_script(entries).encode())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning("could not fill the box egress allowlist: %s", exc)
        return entries
    if done.returncode != 0:
        logger.warning(
            "nft refused the box egress allowlist: %s", done.stderr.decode(errors="replace")
        )
    return entries


__all__ = [
    "ALLOW_V4",
    "ALLOW_V6",
    "DENY_V4",
    "DENY_V6",
    "ENV_EXTRA_ENDPOINTS",
    "METADATA_ADDRESSES",
    "NFTABLES_CONF",
    "NFT_FILE",
    "NFT_TABLE",
    "SYSCTL_FILE",
    "AllowEntry",
    "allow_entries",
    "allowlist_script",
    "apply_allowlist",
    "box_endpoints",
    "box_resolvers",
    "deny_elements",
    "deny_set_lines",
    "forward_rules",
    "output_rules",
    "system_resolve",
]
