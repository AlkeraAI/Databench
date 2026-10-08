"""What a distributed connection may reach, read once for every connector.

A team or personal connection saved on the server is opened by other machines: a
member's laptop, and a cloud box that runs chats for many people and many orgs. An
endpoint that is the dialing machine itself (``localhost``, a loopback, link-local,
unspecified, multicast or instance-metadata address, in any spelling) is not a
database server; on a box it reaches the box's own services. Two checks share this
module:

- :func:`host_reach_refusal` reads a host STRING, for the server's save and for a
  machine materializing a record. A connector registers it through the catalog
  (:func:`endpoint_refusal`), naming the endpoints its own attributes carry.
- :func:`vet_endpoints` resolves every endpoint ONCE, at the moment a machine opens
  the connection, refuses any answer that is the machine (plus whatever the caller
  counts as local: its own interfaces, its bridges), and hands back the vetted
  addresses so the caller can pin the dial to them. A name that answers public to the
  check and loopback to the driver then has nothing to win.

Which hosts each connector dials, and whether a private-range address counts as a
server, are registered (:data:`ENDPOINT_SOURCES`, :data:`REACH_POLICIES`): the open
platform reads a generic SQL URL's hosts itself and refuses private ranges unless the
operator opts them in; a distribution with richer connectors registers the vendor
host rules and, where its warehouses live in a customer's VPC, a policy that admits
private ranges.

Pure: the resolver and the local-address test are parameters, so the policy is
testable with plain data and the I/O lives with the caller.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from alkera_core.egress import (
    METADATA_HOSTNAMES,
    EgressPolicy,
    EgressRefusedError,
    IPAddress,
    embedded_addresses,
    parse_address,
    parse_ipv4_spelling,
)
from alkera_core.extensions import ExtensionError, ExtensionPoint

#: ``host -> every address it resolves to``. Raises ``OSError`` when it does not.
Resolve = Callable[[str], list[str]]

#: Names that resolve to the machine itself.
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})

#: Names a cloud hands its instance metadata out under: never a server.
_METADATA_NAMES = frozenset({"metadata"} | METADATA_HOSTNAMES)

#: Judges only what is never a server: the shared egress policy refuses every
#: instance-metadata address, in whatever spelling. Private ranges are the reach
#: policy's question (:func:`reach_policy`), not this one's.
_SHARED_POLICY = EgressPolicy(allow_private=True)


@dataclass(frozen=True, slots=True)
class ConnectionReachPolicy:
    """Which servers a connection opened by a machine other than the one it was typed
    on may name. Link-local, unspecified, multicast and metadata addresses are never
    admitted, and neither is a chat sandbox network on the machine that dials.

    ``admits_private_ranges`` decides the rest of the non-public space (RFC 1918,
    unique-local, shared address space). ``admits_machine_network`` decides the
    dialing machine's own network: its loopback, its interfaces and the bridges it
    sits on. Both are refused by default, where a box serves many people who must not
    reach its services or each other's networks."""

    admits_private_ranges: bool = False
    admits_machine_network: bool = False


#: The open default: a private-range address is not a server unless the operator
#: named it.
OPEN_REACH_POLICY = ConnectionReachPolicy()

#: A self-hosted install's box (:func:`reaches_local_network`): the people who open
#: its connections run the machine, so any host the box can reach is a server, its
#: own network included. Metadata and the chat sandboxes stay refused.
SELF_HOSTED_REACH_POLICY = ConnectionReachPolicy(
    admits_private_ranges=True, admits_machine_network=True
)

#: The reach policy a distribution registers. At most one registers.
REACH_POLICIES: ExtensionPoint[ConnectionReachPolicy] = ExtensionPoint("connectors.reach_policy")


def reach_policy(
    point: ExtensionPoint[ConnectionReachPolicy] = REACH_POLICIES,
) -> ConnectionReachPolicy:
    """:data:`SELF_HOSTED_REACH_POLICY` on a self-hosted install
    (:func:`reaches_local_network`), else the registered reach policy, or
    :data:`OPEN_REACH_POLICY`. Freezes the point."""
    registered = point.items()
    if len(registered) > 1:
        raise ExtensionError(
            f"{len(registered)} reach policies registered on {point.name!r}; "
            "a connection is judged by exactly one"
        )
    if reaches_local_network():
        return SELF_HOSTED_REACH_POLICY
    return registered[0] if registered else OPEN_REACH_POLICY


#: Present in the environment of a box that serves chats on a platform machine
#: credential (a pool, dedicated or personal box): provisioning writes it into the
#: node environment the daemon runs under.
MACHINE_CREDENTIAL_ENV = "ALKERA_MACHINE_CREDENTIAL"


def reaches_local_network(env: Mapping[str, str] | None = None) -> bool:
    """Whether this is a self-hosted install's machine, whose connections reach any
    host it can: ``CONNECTIONS_REACH_LOCAL_NETWORK``, or an explicit ``SELF_HOSTED=true``
    when that is unset (the one-machine compose stack sets ``SELF_HOSTED``). Never on a
    box serving chats on a platform machine credential, whatever its settings say:
    there the "local network" is every org's box. Settings that cannot be read say
    no."""
    source = os.environ if env is None else env
    if source.get(MACHINE_CREDENTIAL_ENV, "").strip():
        return False
    try:
        from alkera_core.config import settings

        return settings.connections_reach_local_network_on
    except Exception:
        return False


def operator_policy(env: Mapping[str, str] | None = None) -> EgressPolicy:
    """The deployment's explicit opt-in (``EGRESS_PRIVATE_ALLOWLIST``): the hosts and
    networks an operator named as theirs to reach, the same switch that admits a
    self-hosted install's own network to every other guarded fetch. Never a metadata
    address, and a listed network never this machine (:func:`_operator_admits`).

    It is honoured by a machine registered with a person's credential: a laptop, a
    dev or test deployment opening its own database. A box serving chats on a platform
    machine credential admits nothing through it, whatever its settings say, because
    there the "own network" is every org's box. A deployment whose settings cannot be
    read admits nothing either."""
    source = os.environ if env is None else env
    if source.get(MACHINE_CREDENTIAL_ENV, "").strip():
        return EgressPolicy()
    try:
        return EgressPolicy.from_settings()
    except Exception:
        return EgressPolicy()


def _operator_admits(
    policy: EgressPolicy, host: str, address: IPAddress | None, *, on_machine: bool
) -> bool:
    """Whether the operator's list admits ``host`` at ``address``. A host the operator
    NAMED is theirs at any address it has except a metadata one: on a one-machine
    install the data server's name resolves into the same bridge the box sits on. A
    network (or a single address) the operator listed holds servers, never this
    machine: ``on_machine`` (loopback, link-local, the box's own interfaces, bridges
    and chat namespaces) is refused whatever range was listed, so ``10.0.0.0/8`` cannot
    open another chat's sandbox or the box's own services. Nothing that is never a
    server (:func:`address_is_never_a_server`) is admitted by any entry."""
    if address is None:
        return host in policy.allowed_hosts
    if address_is_never_a_server(address):
        return False
    if host in policy.allowed_hosts:
        return policy.admits(host, address)
    return not on_machine and policy.admits(host, address)


class ReachRefusedError(PermissionError):
    """A connection that would reach the machine opening it. ``str()`` is for a person."""


def parse_host_address(host: str) -> IPAddress | None:
    """``host`` as an IP address, read the way the shared egress guard reads one: the
    shorthand IPv4 spellings a C resolver accepts (``127.1``, ``0x7f000001``,
    ``2130706433``, ``0177.0.0.1``) included; ``None`` for a name. Raises
    :class:`EgressRefusedError` for a host that ends in a number but is no valid
    address, since another parser might still find one in it."""
    try:
        return parse_address(host)
    except ValueError:
        pass
    return parse_ipv4_spelling(host)


def address_reaches_machine(address: IPAddress) -> bool:
    """Whether a connection to ``address`` stays on (or next to) the machine that dials:
    loopback, link-local, the unspecified address, multicast, or an instance-metadata
    address. An IPv6 address that carries an IPv4 one (mapped, compatible, 6to4,
    NAT64, Teredo) is judged by every address it carries."""
    if not _SHARED_POLICY.admits("", address):
        return True
    return any(
        candidate.is_loopback
        or candidate.is_link_local
        or candidate.is_unspecified
        or candidate.is_multicast
        for candidate in embedded_addresses(address)
    )


def address_is_private_range(address: IPAddress) -> bool:
    """Whether ``address`` (or an address it carries) is outside the public internet
    without being the machine itself: RFC 1918, unique-local, shared address space and
    the other ranges Python does not count as global."""
    return any(not candidate.is_global for candidate in embedded_addresses(address))


def address_is_never_a_server(address: IPAddress) -> bool:
    """Whether ``address`` (or one it carries) is link-local, unspecified, multicast or
    an instance-metadata address: no reach policy admits one."""
    if not _SHARED_POLICY.admits("", address):
        return True
    return any(
        candidate.is_link_local or candidate.is_unspecified or candidate.is_multicast
        for candidate in embedded_addresses(address)
    )


_MACHINE = "machine"
_PRIVATE = "private"


def _refused_address(
    address: IPAddress, reach: ConnectionReachPolicy, *, on_machine: bool
) -> str | None:
    """Why ``address`` is no server under ``reach`` (:data:`_MACHINE` or
    :data:`_PRIVATE`), or ``None``."""
    if address_is_never_a_server(address):
        return _MACHINE
    if on_machine:
        return None if reach.admits_machine_network else _MACHINE
    if reach.admits_private_ranges or not address_is_private_range(address):
        return None
    return _PRIVATE


def _bare(host: str) -> str:
    return unquote(host).strip().strip("[]").split("%", 1)[0].rstrip(".").lower()


def host_reach_refusal(
    host: str,
    *,
    policy: EgressPolicy | None = None,
    reach: ConnectionReachPolicy | None = None,
) -> str | None:
    """Why ``host`` names the machine that would dial it, or a private-range address
    the reach policy (:func:`reach_policy`) does not admit, rather than a server; or
    ``None``. Reads the string only; what a name RESOLVES to is :func:`vet_endpoints`'s
    question, asked where a machine connects. A host the operator opted in
    (:func:`operator_policy`) is admitted."""
    name = _bare(host)
    if not name:
        return "the connection names no host, so the driver would connect on the machine itself"
    try:
        address = parse_host_address(name)
    except EgressRefusedError:
        return f"the host {host!r} is not a valid address"
    judged = reach if reach is not None else reach_policy()
    admitted = policy if policy is not None else operator_policy()
    if name in _METADATA_NAMES:
        return f"the host {host!r} is a metadata address"
    if address is None:
        loopback_name = name in _LOOPBACK_NAMES or name.endswith(".localhost")
        if not loopback_name or judged.admits_machine_network:
            return None
        if _operator_admits(admitted, name, None, on_machine=True):
            return None
        return f"the host {host!r} is the machine itself"
    on_machine = address_reaches_machine(address)
    reason = _refused_address(address, judged, on_machine=on_machine)
    if reason is None or _operator_admits(admitted, name, address, on_machine=on_machine):
        return None
    if reason == _PRIVATE:
        return (
            f"the host {host!r} is a private network address; the operator admits one "
            "through EGRESS_PRIVATE_ALLOWLIST"
        )
    return f"the host {host!r} is a loopback, link-local or metadata address"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One host a connector's driver dials. ``optional`` is a vendor companion (an
    OCSP responder, a regional endpoint) the connection does not depend on."""

    host: str
    optional: bool = False


#: ``(plugin, built attributes) -> every endpoint its driver dials``, or ``None`` when
#: the source does not know ``plugin``. Raises ``ValueError`` when it knows the plugin
#: and the attributes name no endpoint it accepts.
EndpointSource = Callable[[str, Mapping[str, Any]], "list[Endpoint] | None"]

#: Where a connector's endpoints are read from, asked in registration order. A
#: distribution registers the parser its connection probe dials by, so the reach check
#: and the probe cannot disagree about a vendor's hosts.
ENDPOINT_SOURCES: ExtensionPoint[EndpointSource] = ExtensionPoint("connectors.endpoint_sources")

#: The generic SQL connector's plugin name, whose URL this module reads itself.
GENERIC_SQL = "generic_sql"


def endpoints(
    plugin: str,
    attributes: Mapping[str, Any],
    *,
    sources: ExtensionPoint[EndpointSource] = ENDPOINT_SOURCES,
) -> list[Endpoint]:
    """Every host ``plugin``'s driver dials for these built attributes. Raises
    ``ValueError`` when the attributes name no endpoint, or when nothing knows how
    ``plugin`` names its endpoints (fail closed).

    A generic SQL URL naming several hosts (libpq's ``host=a,b``) is read here; a
    registered source answers next, and a generic SQL URL's single host last."""
    if plugin == GENERIC_SQL:
        hosts = _generic_sql_hosts(attributes)
        if len(hosts) > 1:
            return [Endpoint(host) for host in hosts]
    for source in sources.items():
        found = source(plugin, attributes)
        if found is not None:
            return found
    if plugin == GENERIC_SQL:
        if hosts == [""]:
            raise ValueError("the database URL names no host")
        return [Endpoint(host) for host in hosts]
    raise ValueError(f"no connector {plugin!r} names the endpoints its driver dials")


def _generic_sql_hosts(attributes: Mapping[str, Any]) -> list[str]:
    """Every host a generic SQL URL names (libpq takes a comma-separated list);
    ``[""]`` for none. Raises ``ValueError`` when the URL does not parse."""
    from sqlalchemy.engine import make_url

    try:
        host = make_url(str(attributes.get("url") or "").strip()).host or ""
    except Exception:
        raise ValueError("the database URL could not be parsed") from None
    return host.split(",")


def endpoint_refusal(plugin: str) -> Callable[[Mapping[str, Any]], str | None]:
    """The catalog registration for a connector whose endpoints are hosts it reads from
    its own attributes: the refusal of the first endpoint that is the machine itself."""

    def refusal(attributes: Mapping[str, Any]) -> str | None:
        try:
            found = endpoints(plugin, attributes)
        except ValueError as exc:
            return str(exc).rstrip(".")
        for endpoint in found:
            if endpoint.optional:
                continue
            reason = host_reach_refusal(endpoint.host)
            if reason is not None:
                return reason
        return None

    return refusal


def no_dialed_endpoint(attributes: Mapping[str, Any]) -> str | None:
    """The catalog registration for a connector whose driver dials only its vendor's
    fixed API hosts (BigQuery's googleapis.com): nothing in a record moves the dial."""
    return None


@dataclass(frozen=True, slots=True)
class VettedEndpoint:
    """A host and the exact addresses it resolved to when it was vetted. A caller pins
    the dial to these; a second DNS answer is never asked for."""

    host: str
    addresses: tuple[str, ...]


def vet_endpoints(
    plugin: str,
    attributes: Mapping[str, Any],
    *,
    resolve: Resolve,
    is_local: Callable[[IPAddress], bool] = lambda _address: False,
    is_sandbox: Callable[[IPAddress], bool] = lambda _address: False,
    policy: EgressPolicy | None = None,
    reach: ConnectionReachPolicy | None = None,
) -> list[VettedEndpoint]:
    """Resolve every endpoint once and vet every answer.

    Raises :class:`ReachRefusedError` when a host is the machine on its face, when any
    address it resolves to reaches the machine (:func:`address_reaches_machine`) or is
    local by the caller's own account (``is_local``: its interfaces, its bridges) and
    the reach policy does not admit the machine's network, when any address is in a
    chat sandbox network on the machine (``is_sandbox``: refused under every policy
    and every operator list), or when a host the connection depends on does not
    resolve at all: a name that cannot
    be vetted cannot be pinned, and an unpinned name is the one a rebinding resolver
    answers differently at the dial. A private-range answer is refused unless the
    reach policy (:func:`reach_policy`) admits private ranges. An optional companion
    that does not resolve is skipped. An address the operator opted in
    (:func:`operator_policy`) is admitted, and pinned like any other."""
    try:
        found = endpoints(plugin, attributes)
    except ValueError as exc:
        raise ReachRefusedError(str(exc)) from None
    admitted = policy if policy is not None else operator_policy()
    judged = reach if reach is not None else reach_policy()
    vetted: list[VettedEndpoint] = []
    for endpoint in found:
        face = host_reach_refusal(endpoint.host, policy=admitted, reach=judged)
        if face is not None:
            if endpoint.optional:
                continue
            raise ReachRefusedError(face)
        name = _bare(endpoint.host)
        literal = parse_host_address(name)
        if literal is not None:
            answers = [str(literal)]
        else:
            try:
                answers = list(dict.fromkeys(resolve(name)))
            except OSError:
                if endpoint.optional:
                    continue
                raise ReachRefusedError(f"the host {endpoint.host!r} does not resolve") from None
        for raw in answers:
            try:
                address = parse_address(raw)
            except ValueError:
                raise ReachRefusedError(
                    f"the host {endpoint.host!r} resolved to an unreadable address ({raw})"
                ) from None
            if is_sandbox(address):
                raise ReachRefusedError(
                    f"the host {endpoint.host!r} resolves to a chat sandbox network on "
                    f"the machine itself ({raw})"
                )
            on_machine = address_reaches_machine(address) or is_local(address)
            reason = _refused_address(address, judged, on_machine=on_machine)
            if reason is None or _operator_admits(admitted, name, address, on_machine=on_machine):
                continue
            if reason == _MACHINE:
                raise ReachRefusedError(
                    f"the host {endpoint.host!r} resolves to the machine itself ({raw})"
                )
            if reason == _PRIVATE:
                raise ReachRefusedError(
                    f"the host {endpoint.host!r} resolves to a private network address "
                    f"({raw}); the operator admits one through EGRESS_PRIVATE_ALLOWLIST"
                )
        if answers:
            vetted.append(VettedEndpoint(name, tuple(answers)))
    return vetted


__all__ = [
    "ENDPOINT_SOURCES",
    "GENERIC_SQL",
    "MACHINE_CREDENTIAL_ENV",
    "OPEN_REACH_POLICY",
    "REACH_POLICIES",
    "SELF_HOSTED_REACH_POLICY",
    "ConnectionReachPolicy",
    "Endpoint",
    "EndpointSource",
    "ReachRefusedError",
    "Resolve",
    "VettedEndpoint",
    "address_is_never_a_server",
    "address_is_private_range",
    "address_reaches_machine",
    "endpoint_refusal",
    "endpoints",
    "host_reach_refusal",
    "no_dialed_endpoint",
    "operator_policy",
    "parse_host_address",
    "reach_policy",
    "reaches_local_network",
    "vet_endpoints",
]
