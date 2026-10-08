"""A team record's SQL connection opens only to the server it names, pinned there.

A connection materialized from a team record was typed on another machine. On a cloud
box it opens beside other orgs' files, the box's own services and every chat's
network namespace, and the address a name answers with is up to whoever controls its
DNS. So every such connection, whatever engine, is opened through :func:`guard_connect`
(``SqlEngineSpec`` wraps its ``connect`` and ``admin_connect`` by construction):

1. the catalog's distribution refusal is asked again (the record may predate it);
2. every endpoint host is resolved ONCE and every answer is vetted: never a link-local
   or metadata address (in any spelling) and never the block chat network namespaces
   are carved from; and, unless this is a self-hosted install's machine
   (``reaches_local_network``), nothing that is the machine (loopback) or THIS machine
   (every address on its interfaces, the whole subnet of each virtual or bridge
   interface);
3. the dial is pinned to the vetted addresses: the process resolver answers those
   hosts with exactly those addresses from then on, so the driver, which still dials
   the hostname (TLS and SNI keep verifying the name), never asks DNS a second time.
   Every driver this connects through resolves in Python (psycopg 3 resolves itself
   and hands libpq ``hostaddr``; PyMySQL, clickhouse-connect, trino, the Snowflake and
   Databricks connectors dial through ``socket``). A generic SQL URL naming a C
   default (plain ``postgresql://``, ``mysql://``) is opened with the Python-resolving
   driver for the same wire; one naming a driver with no such twin (ODBC) opens only
   when its host is already an address.

A connection the person added on their own machine opens as it always did.
"""

from __future__ import annotations

import functools
import ipaddress
import os
import re
import socket
import threading
import weakref
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from alkera_core.connectors.reach import (
    ReachRefusedError,
    Resolve,
    VettedEndpoint,
    parse_host_address,
    vet_endpoints,
)
from alkera_core.egress import IPAddress

from alkera_cli.plugins.plugin_base.connection import Connection

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

#: The block a box carves chat network namespaces from, and where it reads an override.
#: The sandbox reads the same name and default (a test pins the two together).
CHAT_NET_ENV = "ALKERA_SANDBOX_NET"
DEFAULT_CHAT_NET = "10.200.0.0/14"

#: Interfaces that are a real network card: only their own address is this machine.
#: Every other interface (loopback, a docker or libvirt bridge, a veth into a chat's
#: namespace, a CNI bridge) puts its whole subnet on this machine.
_PHYSICAL_INTERFACE = re.compile(r"^(eth|en|wl|ww|ib)[0-9a-z]*$")

#: Generic SQL drivers that resolve hostnames through Python's resolver, which the pin
#: answers. Keyed by the URL's ``drivername``.
PINNABLE_GENERIC_DRIVERS = frozenset(
    {
        "postgresql+psycopg",
        "mysql+pymysql",
        "mariadb+pymysql",
        "clickhouse",
        "clickhouse+native",
        "clickhouse+http",
        "clickhousedb",
        "clickhousedb+connect",
        "snowflake",
        "trino",
        "oracle+oracledb",
    }
)

#: A driver that resolves in C, and the Python-resolving driver for the same engine
#: and wire that a team record is opened with instead. A plain ``postgresql://`` or
#: ``mysql://`` names SQLAlchemy's C default; the record keeps working, pinned.
PINNABLE_SUBSTITUTES: dict[str, str] = {
    "postgresql": "postgresql+psycopg",
    "postgresql+psycopg2": "postgresql+psycopg",
    "postgresql+psycopg2cffi": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mysql+mysqldb": "mysql+pymysql",
    "mariadb": "mariadb+pymysql",
    "mariadb+mysqldb": "mariadb+pymysql",
    "oracle": "oracle+oracledb",
    "oracle+cx_oracle": "oracle+oracledb",
}


@dataclass(frozen=True)
class MachineAddresses:
    """The addresses that are this machine: its own, and whole local subnets.
    ``sandboxes`` is the block chat network namespaces are carved from, which no
    connection may reach on any install."""

    addresses: frozenset[IPAddress]
    networks: tuple[IPNetwork, ...]
    sandboxes: tuple[IPNetwork, ...] = ()

    def in_sandbox(self, address: IPAddress) -> bool:
        return any(
            address.version == network.version and address in network for network in self.sandboxes
        )

    def holds(self, address: IPAddress) -> bool:
        if address in self.addresses:
            return True
        return any(
            address.version == network.version and address in network for network in self.networks
        )

    @classmethod
    def from_interfaces(
        cls, interfaces: Iterable[tuple[str, str, str | None]], *, chat_net: str
    ) -> MachineAddresses:
        """Build from ``(interface name, address, netmask)`` triples."""
        addresses: set[IPAddress] = set()
        networks: list[IPNetwork] = []
        for name, raw, netmask in interfaces:
            try:
                address = ipaddress.ip_address(raw.split("%", 1)[0])
            except ValueError:
                continue
            addresses.add(address)
            if netmask and not _PHYSICAL_INTERFACE.match(name):
                try:
                    networks.append(ipaddress.ip_network(f"{address}/{netmask}", strict=False))
                except ValueError:
                    pass
        try:
            sandbox: IPNetwork = ipaddress.ip_network(chat_net.strip(), strict=True)
        except ValueError:
            sandbox = ipaddress.ip_network(DEFAULT_CHAT_NET)
        networks.append(sandbox)
        return cls(frozenset(addresses), tuple(networks), (sandbox,))

    @classmethod
    def current(cls) -> MachineAddresses:
        """This machine now, read from its interfaces."""
        import psutil

        triples = [
            (name, entry.address, entry.netmask)
            for name, entries in psutil.net_if_addrs().items()
            for entry in entries
            if entry.family in (socket.AF_INET, socket.AF_INET6)
        ]
        return cls.from_interfaces(
            triples, chat_net=os.environ.get(CHAT_NET_ENV, "") or DEFAULT_CHAT_NET
        )


# -- the pin ---------------------------------------------------------------

_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_PINS: dict[str, tuple[str, ...]] = {}
_PIN_LOCK = threading.Lock()
_INSTALLED = False


def _pin_key(host: object) -> str | None:
    # anyio (and so every async httpx client) hands the resolver the host already
    # IDNA-encoded as bytes. The idna codec takes no error handler, so a lenient
    # decode raised for every such lookup once the pin was installed.
    if isinstance(host, bytes):
        try:
            host = host.decode("idna")
        except UnicodeError:
            return None
    if not isinstance(host, str):
        return None
    return host.strip().strip("[]").rstrip(".").lower()


def _pinned_getaddrinfo(
    host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
) -> list[Any]:
    key = _pin_key(host)
    pinned = _PINS.get(key) if key is not None else None
    if pinned is None:
        return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)
    answers: list[Any] = []
    for address in pinned:
        try:
            answers.extend(
                _ORIGINAL_GETADDRINFO(
                    address, port, family, type, proto, flags | socket.AI_NUMERICHOST
                )
            )
        except socket.gaierror:
            continue  # an address of the other family than the one asked for
    if not answers:
        raise socket.gaierror(socket.EAI_NONAME, f"{host!r} has no vetted address of that family")
    return answers


def _install_pin() -> None:
    global _INSTALLED
    with _PIN_LOCK:
        if not _INSTALLED:
            # Every module that resolves reads ``socket.getaddrinfo`` at call time, so
            # rebinding the module attribute reaches them all.
            setattr(socket, "getaddrinfo", _pinned_getaddrinfo)  # noqa: B010
            _INSTALLED = True


def pin(vetted: Iterable[VettedEndpoint]) -> None:
    """Answer each vetted host with exactly its vetted addresses from now on. A later
    vet of the same host replaces its pin, so a warehouse that moves is followed on
    the next open, never between the check and the dial."""
    _install_pin()
    with _PIN_LOCK:
        for endpoint in vetted:
            _PINS[endpoint.host] = endpoint.addresses


def pinned_addresses(host: str) -> tuple[str, ...] | None:
    """The addresses ``host`` is pinned to, or ``None``."""
    key = _pin_key(host)
    return _PINS.get(key) if key is not None else None


def system_resolve(host: str) -> list[str]:
    """Every address ``host`` resolves to, asked of DNS (never of the pin)."""
    infos = _ORIGINAL_GETADDRINFO(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


# -- the guard -------------------------------------------------------------


def pinnable_connection(conn: Connection) -> Connection:
    """``conn`` as it can be opened with its dial pinned: unchanged when its driver
    resolves through Python (or every host is already an address), with the driver
    swapped for its Python-resolving twin when it names a C default with one
    (:data:`PINNABLE_SUBSTITUTES`), and refused when it names a driver that resolves
    in C and has no such twin (an ODBC driver, FreeTDS)."""
    if conn.plugin != "generic_sql":
        return conn
    from sqlalchemy.engine import make_url

    url = make_url(str(conn.attributes.get("url") or ""))
    driver = url.drivername.lower()
    if driver in PINNABLE_GENERIC_DRIVERS:
        return conn
    substitute = PINNABLE_SUBSTITUTES.get(driver)
    if substitute is not None:
        rewritten = url.set(drivername=substitute).render_as_string(hide_password=False)
        return conn.model_copy(update={"attributes": {**conn.attributes, "url": rewritten}})
    hosts = (url.host or "").split(",")
    if all(parse_host_address(host.strip().strip("[]")) is not None for host in hosts):
        return conn
    raise ReachRefusedError(
        f"connection {conn.handle!r} refused: the {url.drivername!r} driver resolves host "
        "names itself, where the checked address cannot be held. Name the server "
        "by its IP address, or use a driver that resolves in Python ("
        + ", ".join(sorted(PINNABLE_GENERIC_DRIVERS))
        + ")"
    )


def vet_shared_connection(
    conn: Connection,
    *,
    resolve: Resolve | None = None,
    machine: MachineAddresses | None = None,
) -> Connection:
    """Refuse a team record's connection that would reach this machine, pin the rest,
    and return the connection to dial (:func:`pinnable_connection`). Raises
    :class:`ReachRefusedError` naming why."""
    from alkera_core.connectors.catalog import distribution_refusal, get_descriptor

    try:
        descriptor = get_descriptor(conn.plugin)
    except KeyError:
        raise ReachRefusedError(
            f"connection {conn.handle!r} refused: no connector {conn.plugin!r} to vet it"
        ) from None
    refusal = distribution_refusal(descriptor, dict(conn.attributes))
    if refusal is not None:
        raise ReachRefusedError(f"connection {conn.handle!r} refused: {refusal}")
    dialed = pinnable_connection(conn)
    if descriptor.distribution_refusal is None:
        return dialed
    local = machine if machine is not None else MachineAddresses.current()
    try:
        vetted = vet_endpoints(
            dialed.plugin,
            dict(dialed.attributes),
            resolve=resolve or system_resolve,
            is_local=local.holds,
            is_sandbox=local.in_sandbox,
        )
    except ReachRefusedError as exc:
        raise ReachRefusedError(f"connection {conn.handle!r} refused: {exc}") from None
    pin(vetted)
    return dialed


_F = TypeVar("_F", bound=Callable[..., Any])

#: Each raw connect callable's guarded twin, and every guarded callable. One raw
#: callable always gets the same twin, so two specs built from it still compare equal.
_TWINS: weakref.WeakKeyDictionary[Callable[..., Any], Callable[..., Any]] = (
    weakref.WeakKeyDictionary()
)
_GUARDED: weakref.WeakSet[Callable[..., Any]] = weakref.WeakSet()


def guard_connect(connect: _F) -> _F:
    """``connect`` with a team record's connection vetted and pinned first. Idempotent:
    a callable already guarded is returned as it is, and one raw callable always gets
    the same guarded twin."""
    if connect in _GUARDED:
        return connect
    twin = _TWINS.get(connect)
    if twin is not None:
        return cast(_F, twin)

    @functools.wraps(connect)
    def guarded(conn: Connection, *args: Any, **kwargs: Any) -> Any:
        if conn._runtime_bindings.get("team_record_id"):
            conn = vet_shared_connection(conn)
        return connect(conn, *args, **kwargs)

    _GUARDED.add(guarded)
    _TWINS[connect] = guarded
    return cast(_F, guarded)


__all__ = [
    "CHAT_NET_ENV",
    "DEFAULT_CHAT_NET",
    "PINNABLE_GENERIC_DRIVERS",
    "PINNABLE_SUBSTITUTES",
    "MachineAddresses",
    "guard_connect",
    "pin",
    "pinnable_connection",
    "pinned_addresses",
    "system_resolve",
    "vet_shared_connection",
]
