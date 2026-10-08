"""The one guard for a server-side fetch of a URL somebody else supplied.

A model, an org admin or a web page hands this process a URL and the process
dials it from inside its own network. Three things go wrong when the guard is
"parse the string, look at the host, then give the string to an HTTP client":

- the checker and the client are two parsers and disagree about which bytes are
  the host (``http://127.0.0.1\\@example.com/`` is ``example.com`` to one and
  loopback to the other; ``0177.0.0.1`` is a name, ``177.0.0.1`` or ``127.0.0.1``
  depending on who reads it);
- the checker resolves the name and the client resolves it AGAIN, so a resolver
  that answers public first and private second walks straight through;
- a redirect re-enters the client with a URL nobody vetted.

So this module does each of those exactly once and hands the client nothing it
can reinterpret:

1. :func:`canonicalize_url` parses strictly, refuses every ambiguous spelling,
   and REBUILDS the URL from the parsed parts. Only the rebuilt string is ever
   fetched, so the client cannot see a byte the guard did not.
2. :func:`vet_url` resolves the host itself and classifies EVERY address (after
   unwrapping the IPv6 forms that carry an IPv4 address inside them). One
   non-public answer refuses the whole name.
3. The connection is PINNED to a vetted address: :class:`PinnedAsyncTransport`
   for httpx, :class:`PinnedTunnel` for a client that cannot be told where to
   connect. A second DNS answer is never asked for.
4. Redirects are followed by the caller, one hop at a time, through
   :func:`redirect_target` and the same three steps.

Operators of a self-hosted install legitimately fetch from their own network.
The opt-out is explicit and narrow: ``EGRESS_PRIVATE_ALLOWLIST`` names the hosts
and networks that may be private (default empty, validated at boot, never the
link-local metadata range), and a caller whose endpoint is operator-owned by
deployment rule passes ``allow_private=True``. Either way the URL is still
canonicalised and the connection still pinned.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import socket
import struct
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Final
from urllib.parse import quote, urljoin

import httpx
import idna

from alkera_core.net_ranges import DENIED, NEVER_ALLOWED

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

#: ``(host, port) -> every address the host resolves to``. Injectable so a test
#: can script the answers; the default asks the system resolver.
Resolver = Callable[[str, int], Sequence[str]]

#: ``(address, port, timeout) -> connected socket``. Injectable so a test can
#: observe which address was dialed and route it to a local listener.
Dialer = Callable[[str, int, float], socket.socket]


class EgressRefusedError(ValueError):
    """The URL may not be fetched. ``code`` is stable; ``str()`` is for a person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- 1. canonicalise ----------------------------------------------------------

_ALLOWED_SCHEMES: Final = ("http", "https")
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_MAX_PORT: Final = 65535
_MAX_HOSTNAME_LENGTH: Final = 253

_URL_SHAPE = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://"
    r"(?P<authority>[^/?#]*)"
    r"(?P<path>[^?#]*)"
    r"(?:\?(?P<query>[^#]*))?"
    r"(?:#.*)?$",
    re.DOTALL,
)
_LDH_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_NUMERIC_LABEL = re.compile(r"^([0-9]+|0[xX][0-9A-Fa-f]*)$")
_STRAY_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PATH_SAFE: Final = "/%:@!$&'()*+,;=~-._"
_QUERY_SAFE: Final = _PATH_SAFE + "?"

#: Names that never leave the machine or the network, whatever they resolve to.
_PRIVATE_SUFFIXES: Final = (".localhost", ".local", ".internal")


@dataclass(frozen=True, slots=True)
class CanonicalUrl:
    """A URL rebuilt from strictly parsed parts. ``host`` is lower-case ASCII (an
    IDNA A-label name) or a canonical IP literal without brackets."""

    scheme: str
    host: str
    port: int
    path: str
    query: str | None
    address: IPAddress | None = None

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if isinstance(self.address, ipaddress.IPv6Address) else self.host
        return host if self.port == _DEFAULT_PORTS[self.scheme] else f"{host}:{self.port}"

    @property
    def url(self) -> str:
        query = "" if self.query is None else f"?{self.query}"
        return f"{self.scheme}://{self.authority}{self.path}{query}"

    def __str__(self) -> str:
        return self.url


def parse_ipv4_spelling(host: str) -> ipaddress.IPv4Address | None:
    """Read ``host`` the way a browser-grade client reads a numeric host.

    ``0177.0.0.1``, ``0x7f.1``, ``2130706433`` and ``127.1`` are all
    ``127.0.0.1`` to a WHATWG parser (octal, hex, a single 32-bit number, short
    forms whose last part fills the remaining bytes). Returns ``None`` when the
    host does not end in a number (it is a name); raises :class:`EgressRefusedError`
    when it ends in a number but is not a valid address in any spelling, because
    the next parser might still find one in it.
    """
    parts = host.split(".")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if not parts or not _NUMERIC_LABEL.match(parts[-1]):
        return None
    refused = EgressRefusedError("host", f"{host!r} is not a valid address")
    if len(parts) > 4:
        raise refused
    numbers: list[int] = []
    for part in parts:
        if not _NUMERIC_LABEL.match(part):
            raise refused
        if part[:2].lower() == "0x":
            numbers.append(int(part[2:] or "0", 16))
        elif len(part) > 1 and part[0] == "0":
            if not re.fullmatch(r"[0-7]+", part):
                raise refused
            numbers.append(int(part, 8))
        else:
            numbers.append(int(part, 10))
    if any(n > 0xFF for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        raise refused
    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value += number << (8 * (3 - index))
    return ipaddress.IPv4Address(value)


def _canonical_host(raw: str) -> tuple[str, IPAddress | None]:
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise EgressRefusedError("host", "the URL has a malformed IPv6 host")
        try:
            v6 = ipaddress.IPv6Address(raw[1:-1])
        except ValueError:
            raise EgressRefusedError("host", "the URL has a malformed IPv6 host") from None
        return v6.compressed, v6
    host = raw.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host:
        raise EgressRefusedError("host", "the URL has no host")
    if not host.isascii():
        try:
            host = idna.encode(host, uts46=True, std3_rules=True).decode("ascii").lower()
        except idna.IDNAError:
            raise EgressRefusedError("host", "the URL's host is not a valid domain name") from None
    v4 = parse_ipv4_spelling(host)
    if v4 is not None:
        return str(v4), v4
    labels = host.split(".")
    if len(host) > _MAX_HOSTNAME_LENGTH or not all(_LDH_LABEL.match(label) for label in labels):
        raise EgressRefusedError("host", "the URL's host is not a valid domain name")
    return host, None


def _split_authority(authority: str, scheme: str) -> tuple[str, int]:
    if not authority:
        raise EgressRefusedError("host", "the URL has no host")
    if "@" in authority:
        raise EgressRefusedError("userinfo", "the URL must not embed credentials")
    if "%" in authority:
        raise EgressRefusedError("authority", "the URL's host must not be percent-encoded")
    host, port_text = authority, None
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            raise EgressRefusedError("host", "the URL has a malformed IPv6 host")
        host, rest = authority[: end + 1], authority[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                raise EgressRefusedError("host", "the URL has a malformed IPv6 host")
            port_text = rest[1:]
    elif ":" in authority:
        host, port_text = authority.split(":", 1)
    if port_text is None:
        return host, _DEFAULT_PORTS[scheme]
    if not re.fullmatch(r"[0-9]{1,5}", port_text) or not 0 < int(port_text) <= _MAX_PORT:
        raise EgressRefusedError("port", "the URL has an invalid port")
    return host, int(port_text)


def _remove_dot_segments(path: str) -> str:
    out: list[str] = []
    segments = path.split("/")
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == ".":
            if last:
                out.append("")
        elif segment == "..":
            if len(out) > 1:
                out.pop()
            if last:
                out.append("")
        else:
            out.append(segment)
    return "/".join(out) or "/"


def canonicalize_url(raw: str, *, schemes: Iterable[str] = _ALLOWED_SCHEMES) -> CanonicalUrl:
    """Parse ``raw`` strictly and rebuild it. Raises :class:`EgressRefusedError`.

    Refused outright, because each is a byte two parsers read differently: a
    backslash, an ASCII control character or whitespace anywhere; userinfo; a
    percent sign in the authority; a percent-encoded dot segment; a numeric host
    that is not an address; a name that is not LDH after IDNA; an empty host; a
    port outside 1-65535. The fragment is dropped (it is never sent).
    """
    text = raw.strip(" \t\r\n")
    if any(ch == "\\" or ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in text):
        raise EgressRefusedError(
            "characters", "the URL contains a backslash, whitespace or a control character"
        )
    match = _URL_SHAPE.match(text)
    if match is None:
        raise EgressRefusedError("shape", "an absolute http(s) URL is required")
    scheme = match["scheme"].lower()
    if scheme not in tuple(schemes) or scheme not in _DEFAULT_PORTS:
        raise EgressRefusedError("scheme", f"only http(s) URLs are fetched, got {scheme!r}")
    raw_host, port = _split_authority(match["authority"], scheme)
    host, address = _canonical_host(raw_host)
    path = match["path"] or "/"
    for segment in path.split("/"):
        if "%" in segment and segment.lower().replace("%2e", ".") in (".", ".."):
            raise EgressRefusedError("path", "the URL's path has a percent-encoded dot segment")
    path = quote(_STRAY_PERCENT.sub("%25", _remove_dot_segments(path)), safe=_PATH_SAFE)
    query = match["query"]
    if query is not None:
        query = quote(_STRAY_PERCENT.sub("%25", query), safe=_QUERY_SAFE)
    return CanonicalUrl(scheme, host, port, path, query, address)


def redirect_target(current: CanonicalUrl, location: str) -> str:
    """The absolute URL a ``Location`` header points at, resolved against the
    CANONICAL current URL. The result is untrusted: it goes back through
    :func:`vet_url` like any first-hop URL."""
    location = location.strip(" \t\r\n")
    if "\\" in location:
        # A joiner would happily carry the backslash into the next authority.
        raise EgressRefusedError(
            "characters", "the redirect contains a backslash, whitespace or a control character"
        )
    return urljoin(current.url, location)


# --- 2. classify --------------------------------------------------------------

_NAT64: Final = ipaddress.IPv6Network("64:ff9b::/96")
_V4_COMPATIBLE: Final = ipaddress.IPv6Network("::/96")

#: Names a cloud hands its instance-metadata service out under. The ADDRESSES are
#: in :data:`~alkera_core.net_ranges.NEVER_ALLOWED`, which covers any fetch that
#: resolves; these are for
#: the checks that only ever see a string — a request schema validating what an
#: org admin typed, before anything is dialed.
METADATA_HOSTNAMES: Final[frozenset[str]] = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

#: IPv6 ranges that carry an IPv4 address inside them. An allowlist entry here
#: would be a second spelling of an IPv4 range, one the metadata check could not
#: read through, so the operator lists the IPv4 range itself instead.
_EMBEDDING_RANGES: Final[tuple[IPNetwork, ...]] = (
    ipaddress.ip_network("::ffff:0:0/96"),
    _V4_COMPATIBLE,
    _NAT64,
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("2001::/32"),
)


def embedded_addresses(address: IPAddress) -> list[IPAddress]:
    """``address`` plus every IPv4 address it carries inside it: IPv4-mapped,
    IPv4-compatible, NAT64, 6to4 and Teredo all deliver to the embedded v4
    target on a network that routes them."""
    found: list[IPAddress] = [address]
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            found.append(address.ipv4_mapped)
        if address.sixtofour is not None:
            found.append(address.sixtofour)
        if address.teredo is not None:
            found.extend(address.teredo)
        low = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if address in _NAT64 or (address in _V4_COMPATIBLE and int(address) > 1):
            found.append(low)
    return found


def address_refusal(address: IPAddress) -> str | None:
    """Why ``address`` is not a public destination, or ``None`` when it is."""
    for candidate in embedded_addresses(address):
        for network, label in DENIED:
            if candidate.version == network.version and candidate in network:
                return label
        if not candidate.is_global:
            return "not publicly routable"
    return None


def parse_address(value: str) -> IPAddress:
    """An address string from a resolver. A zone id (``fe80::1%en0``) is dropped
    before parsing — the scope does not change what the address is."""
    return ipaddress.ip_address(value.split("%", 1)[0])


def address_is_public(value: str) -> bool:
    """Whether one address STRING — a resolver answer, a configured literal — is
    a public destination.

    The string form exists because callers hold resolver output, not parsed
    addresses. Anything unparseable is NOT public: a spelling this module cannot
    classify is one it cannot vouch for, and a guard that passes what it does not
    understand is not a guard.
    """
    try:
        address = parse_address(value)
    except ValueError:
        return False
    return address_refusal(address) is None


# --- the operator opt-out -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """What may be private. The default admits nothing private at all."""

    allow_private: bool = False
    allowed_networks: tuple[IPNetwork, ...] = ()
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_entries(cls, entries: Iterable[str], *, allow_private: bool = False) -> EgressPolicy:
        networks, hosts = parse_allowlist(entries)
        return cls(allow_private, networks, hosts)

    @classmethod
    def from_settings(cls, *, allow_private: bool = False) -> EgressPolicy:
        from alkera_core.config import settings

        return cls.from_entries(
            settings.egress_private_allowlist_entries, allow_private=allow_private
        )

    def admits(self, host: str, address: IPAddress) -> bool:
        # The address AND every IPv4 address it embeds: a mapped or NAT64
        # spelling of the metadata service is delivered to the metadata service.
        if any(_in_any(candidate, NEVER_ALLOWED) for candidate in embedded_addresses(address)):
            return False
        if self.allow_private or host in self.allowed_hosts:
            return True
        return any(
            address in network
            for network in self.allowed_networks
            if network.version == address.version
        )


def _in_any(address: IPAddress, networks: Iterable[IPNetwork]) -> bool:
    return any(address in network for network in networks if network.version == address.version)


#: The proxy variables the STANDARD pair of names covers — what every HTTP
#: client here reads.
#:
#: Every list below names a variable ONCE, in its canonical upper case, and the
#: lookup matches any spelling: ``http_proxy`` and ``HTTP_PROXY`` are the same
#: variable on Windows (its environment is case-insensitive and ``os.environ``
#: upper-cases the keys), and on POSIX a client honours either spelling anyway —
#: urllib, which httpx asks for the environment's proxies, lower-cases every
#: name it walks. A spelling this guard did not match would be a proxy it let
#: through.
_STANDARD_PROXY_ENV: Final = ("HTTPS_PROXY", "HTTP_PROXY")

#: What puts a proxy in front of **httpx** (``trust_env``), including ``ALL_PROXY``.
HTTPX_PROXY_ENV: Final = ("ALL_PROXY", *_STANDARD_PROXY_ENV)

#: What puts a proxy in front of **primp**, the browser-impersonating client
#: behind ``web.fetch``. It reads its own ``PRIMP_PROXY`` and the standard pair,
#: and — measured, not assumed — it does NOT read ``ALL_PROXY``.
#:
#: The two lists are deliberately separate. Judging primp by httpx's list turns
#: an ``ALL_PROXY``-only deployment into the worst of both: a fetch that could
#: have been pinned is refused, and allowing the proxy makes the tunnel stand
#: down so the fetch goes out DIRECT and unpinned through no proxy at all.
PRIMP_PROXY_ENV: Final = ("PRIMP_PROXY", *_STANDARD_PROXY_ENV)

#: Every variable either client reads — what an operator has configured, for the
#: boot log line and the production refusal, which speak for the whole process.
FORWARD_PROXY_ENV: Final = ("PRIMP_PROXY", *HTTPX_PROXY_ENV)


def configured_proxy_env(
    names: Sequence[str] = FORWARD_PROXY_ENV, environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Which of ``names`` this process is configured with, in the order given.

    Pass the list belonging to the client that is about to make the request —
    a variable a client does not read is not a proxy in front of that client.

    The environment is read case-insensitively and each variable is reported
    once, under the canonical name it was asked for: spelling is not what
    decides whether a proxy stands in front of the fetch. Matching a spelling a
    given client happens not to honour only ever refuses a fetch that was
    pinnable, which is the safe direction to be wrong in."""
    env = os.environ if environ is None else environ
    configured = {name.upper() for name, value in env.items() if value}
    return tuple(name for name in names if name.upper() in configured)


def proxy_egress_allowed() -> bool:
    """Whether a guarded fetch may go out through a forward proxy.

    Behind a proxy the pin is gone: the proxy resolves the name and makes the
    connection, so the address this process vetted is not necessarily the one
    dialed, and a record that answers public to us and private to the proxy walks
    through. Canonicalisation and address vetting still happen — this is the
    rebinding half only — but "vetted, not pinned" is a weaker promise than the
    one the rest of this module makes, so a deployment opts into it explicitly.
    """
    from alkera_core.config import settings

    return bool(settings.egress_allow_proxy)


def proxy_refusal(names: Sequence[str] = FORWARD_PROXY_ENV) -> EgressRefusedError | None:
    """The refusal for an unpinnable fetch, or ``None`` when no proxy stands in
    front of the client about to make it (or the deployment has allowed one).

    ``names`` is that client's own list — :data:`HTTPX_PROXY_ENV` or
    :data:`PRIMP_PROXY_ENV`."""
    names = configured_proxy_env(names)
    if not names or proxy_egress_allowed():
        return None
    return EgressRefusedError(
        "proxy",
        f"{', '.join(names)} puts a forward proxy in front of this fetch, so the "
        "vetted address cannot be pinned; set EGRESS_ALLOW_PROXY=true to accept that",
    )


def parse_allowlist(entries: Iterable[str]) -> tuple[tuple[IPNetwork, ...], frozenset[str]]:
    """Validate ``EGRESS_PRIVATE_ALLOWLIST`` entries: each is an IP, a CIDR or an
    exact hostname. A zero-length prefix would switch the guard off in one line,
    a metadata range would open the metadata service, and an IPv6 range that
    embeds IPv4 addresses would be a spelling of an IPv4 range this check cannot
    read through, so all three are refused rather than honoured."""
    networks: list[IPNetwork] = []
    hosts: set[str] = set()
    for entry in (e.strip() for e in entries):
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            name = entry.lower().rstrip(".")
            if not all(_LDH_LABEL.match(label) for label in name.split(".")):
                raise ValueError(f"{entry!r} is not an IP address, a CIDR or a hostname") from None
            hosts.add(name)
            continue
        if network.prefixlen == 0:
            raise ValueError(f"{entry!r} would allow every address")
        if any(network.overlaps(n) for n in NEVER_ALLOWED if n.version == network.version):
            raise ValueError(f"{entry!r} covers a cloud metadata address")
        if network.version == 6 and any(network.overlaps(n) for n in _EMBEDDING_RANGES):
            raise ValueError(
                f"{entry!r} is an IPv6 spelling of an IPv4 range; list the IPv4 range instead"
            )
        networks.append(network)
    return tuple(networks), frozenset(hosts)


# --- 2b. resolve + vet --------------------------------------------------------


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def _dns_timeout_seconds(given: float | None) -> float:
    if given is not None:
        return given
    from alkera_core.config import settings

    return settings.egress_dns_timeout_seconds


def _resolve_within(resolver: Resolver, host: str, port: int, timeout: float) -> Sequence[str]:
    """Resolve with a deadline of our own.

    A name lookup is not bounded by any HTTP timeout: the client timeout starts
    at connect, and the OS resolver's own bound is minutes on a resolver that
    accepts the query and never answers. So one slow authoritative server would
    hold the request — and a thread — for far longer than the caller asked for.

    ``getaddrinfo`` cannot be cancelled, so a lookup that outruns the deadline is
    ABANDONED: the request refuses immediately and the worker thread exits
    whenever the OS finally answers. That leaks a thread per timed-out lookup for
    the resolver's own bound and no longer; holding the request instead would
    leak the same thread AND the request.
    """
    answer: list[Sequence[str] | BaseException] = []
    done = threading.Event()

    def run() -> None:
        try:
            answer.append(resolver(host, port))
        except BaseException as exc:  # re-raised on the calling thread below
            answer.append(exc)
        finally:
            done.set()

    threading.Thread(target=run, name=f"egress-resolve-{host}", daemon=True).start()
    if not done.wait(timeout):
        # An OSError, so it joins the resolver's own failures under one refusal
        # code — a caller learns "this name did not resolve", never which of the
        # two it was, which is the same non-answer a rebinding probe gets.
        raise TimeoutError(f"the lookup took longer than {timeout:g}s")
    result = answer[0]
    if isinstance(result, BaseException):
        raise result
    return result


@dataclass(frozen=True, slots=True)
class VettedTarget:
    """A canonical URL and the addresses it was vetted against. The connection
    goes to :attr:`pinned` and nowhere else."""

    url: CanonicalUrl
    addresses: tuple[IPAddress, ...]

    @property
    def pinned(self) -> IPAddress:
        return self.addresses[0]


_DEFAULT_POLICY: Final = EgressPolicy()


def vet_url(
    raw: str,
    *,
    policy: EgressPolicy = _DEFAULT_POLICY,
    resolver: Resolver | None = None,
    schemes: Iterable[str] = _ALLOWED_SCHEMES,
    dns_timeout: float | None = None,
) -> VettedTarget:
    """Canonicalise ``raw``, resolve its host once, and refuse unless EVERY
    address is public (or admitted by ``policy``). Blocking: call it from a
    worker thread, or use :func:`vet_url_async`.

    ``dns_timeout`` bounds the name lookup; left unset it is
    ``EGRESS_DNS_TIMEOUT_SECONDS``."""
    url = canonicalize_url(raw, schemes=schemes)
    private_name = (
        url.address is None
        # A single label is never a public destination: it is completed by the
        # machine's own search domains, so `wiki` or `instance-data` means
        # whatever this network says it means.
        and (url.host == "localhost" or url.host.endswith(_PRIVATE_SUFFIXES) or "." not in url.host)
        and not (policy.allow_private or url.host in policy.allowed_hosts)
    )
    if private_name:
        raise EgressRefusedError("local_name", f"{url.host} is a local or internal name")
    if url.address is not None:
        addresses: tuple[IPAddress, ...] = (url.address,)
    else:
        try:
            # ``socket`` is looked up at call time so a test that patches
            # ``socket.getaddrinfo`` scripts this resolver too.
            answers = _resolve_within(
                resolver or system_resolver,
                url.host,
                url.port,
                _dns_timeout_seconds(dns_timeout),
            )
            addresses = tuple(dict.fromkeys(parse_address(answer) for answer in answers))
        except (OSError, ValueError) as exc:
            raise EgressRefusedError("resolve", f"could not resolve {url.host!r}: {exc}") from exc
        if not addresses:
            raise EgressRefusedError("resolve", f"could not resolve {url.host!r}: no addresses")
    for address in addresses:
        reason = address_refusal(address)
        if reason is not None and not policy.admits(url.host, address):
            raise EgressRefusedError(
                "private", f"{url.host} resolves to a non-public address ({address}: {reason})"
            )
    return VettedTarget(url, addresses)


async def vet_url_async(
    raw: str,
    *,
    policy: EgressPolicy = _DEFAULT_POLICY,
    resolver: Resolver | None = None,
    schemes: Iterable[str] = _ALLOWED_SCHEMES,
    dns_timeout: float | None = None,
) -> VettedTarget:
    import asyncio

    return await asyncio.to_thread(
        vet_url,
        raw,
        policy=policy,
        resolver=resolver,
        schemes=schemes,
        dns_timeout=dns_timeout,
    )


# --- 3a. pin an httpx client --------------------------------------------------


class PinnedAsyncTransport(httpx.AsyncBaseTransport):
    """Vets every request an httpx client sends — redirect hops included, since
    each hop is a new request through the transport — and dials the vetted
    address with the original ``Host`` and TLS server name. A refusal surfaces as
    :class:`httpx.ConnectError`, so callers' existing error handling reports it
    as the unreachable endpoint it is.

    ``pin=False`` is for the transport that talks to a forward proxy: the proxy
    makes the connection, so there is no address of ours to pin, but the URL is
    still canonicalised, resolved and vetted here before the proxy hears of it."""

    def __init__(
        self,
        *,
        policy: EgressPolicy = _DEFAULT_POLICY,
        resolver: Resolver | None = None,
        schemes: Iterable[str] = _ALLOWED_SCHEMES,
        inner: httpx.AsyncBaseTransport | None = None,
        pin: bool = True,
    ) -> None:
        self._pin = pin
        self._policy = policy
        self._resolver = resolver
        self._schemes = tuple(schemes)
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            if not self._pin:
                refusal = proxy_refusal(HTTPX_PROXY_ENV)
                if refusal is not None:
                    raise refusal
            target = await vet_url_async(
                str(request.url),
                policy=self._policy,
                resolver=self._resolver,
                schemes=self._schemes,
            )
        except EgressRefusedError as exc:
            raise httpx.ConnectError(f"refused: {exc}", request=request) from exc
        canonical = target.url
        headers = httpx.Headers(request.headers)
        headers["Host"] = canonical.authority
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = canonical.host
        raw_path = (
            canonical.path + ("" if canonical.query is None else f"?{canonical.query}")
        ).encode("ascii")
        # Every vetted address is a legitimate destination; one that does not
        # answer is skipped, the way a resolver-driven client would move on.
        hosts = [str(a) for a in target.addresses] if self._pin else [canonical.host]
        for index, host in enumerate(hosts):
            pinned = httpx.URL(
                scheme=canonical.scheme, host=host, port=canonical.port, raw_path=raw_path
            )
            try:
                return await self._inner.handle_async_request(
                    httpx.Request(
                        request.method,
                        pinned,
                        headers=headers,
                        stream=request.stream,
                        extensions=extensions,
                    )
                )
            except httpx.ConnectError:
                if index == len(hosts) - 1:
                    raise
        raise AssertionError("unreachable: a vetted target has at least one address")

    async def aclose(self) -> None:
        await self._inner.aclose()


# --- 3b. pin a client that cannot be told where to connect ----------------------

_SOCKS_VERSION: Final = 5
_SOCKS_CONNECT: Final = 1
_SOCKS_ATYP_LENGTHS: Final = {1: 4, 4: 16}
_SOCKS_ATYP_DOMAIN: Final = 3
_SOCKS_OK: Final = b"\x05\x00\x00\x01" + bytes(6)
_SOCKS_REFUSED: Final = b"\x05\x02\x00\x01" + bytes(6)
_TUNNEL_CHUNK: Final = 65536


def _dial(address: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((address, port), timeout=timeout)


def _read_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise OSError("peer closed during the SOCKS handshake")
        data += chunk
    return data


class PinnedTunnel:
    """A loopback SOCKS5 listener that connects to ONE vetted address.

    For an HTTP client with no "connect here, but speak as that host" knob (a
    browser-impersonating client owns its own TLS stack, so an httpx transport is
    no substitute). The client is pointed at :attr:`proxy_url`; whatever host it
    asks the proxy for — its own reading of the URL, its own DNS answer — the
    tunnel dials the address :func:`vet_url` approved, on the vetted port, and a
    request for any other port is refused. TLS and ``Host`` stay end to end, so
    the certificate is still checked against the real name.
    """

    def __init__(
        self,
        target: VettedTarget,
        *,
        timeout: float,
        dial: Dialer | None = None,
        half_close_grace: float | None = None,
    ) -> None:
        self._addresses = [str(address) for address in target.addresses]
        self._port = target.url.port
        self._timeout = timeout
        # How long a half-closed connection may go on receiving. The client's own
        # timeout is what bounds a response, so the deadline this tunnel dials
        # with is the same deadline its peers get to answer within.
        self._grace = timeout if half_close_grace is None else half_close_grace
        self._dial = dial or _dial
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        self._closed = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

    @property
    def proxy_url(self) -> str:
        # socks5h: the client sends the NAME, so it performs no lookup of its own.
        return f"socks5h://127.0.0.1:{self._listener.getsockname()[1]}"

    def __enter__(self) -> PinnedTunnel:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Stop listening and tear down every connection. ``shutdown`` first: on
        Linux a bare ``close`` does not wake a thread blocked in ``accept`` or
        ``recv`` on that socket, so the listener would go on accepting and the
        pump threads would outlive the fetch."""
        with self._lock:
            self._closed = True
            sockets, self._sockets = self._sockets, []
        for sock in (self._listener, *sockets):
            _shutdown_and_close(sock)

    def _dial_any(self) -> socket.socket:
        """The first vetted address that answers, in resolver order."""
        for index, address in enumerate(self._addresses):
            try:
                return self._dial(address, self._port, self._timeout)
            except OSError:
                if index == len(self._addresses) - 1:
                    raise
        raise AssertionError("unreachable: a vetted target has at least one address")

    def _track(self, sock: socket.socket) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._sockets.append(sock)
            return True

    def _accept_loop(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            if not self._track(client):
                _shutdown_and_close(client)
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        try:
            client.settimeout(self._timeout)
            version, method_count = _read_exact(client, 2)
            _read_exact(client, method_count)
            client.sendall(bytes((_SOCKS_VERSION, 0)))
            version, command, _reserved, address_type = _read_exact(client, 4)
            if address_type == _SOCKS_ATYP_DOMAIN:
                _read_exact(client, _read_exact(client, 1)[0])
            else:
                _read_exact(client, _SOCKS_ATYP_LENGTHS.get(address_type, 0))
            (port,) = struct.unpack("!H", _read_exact(client, 2))
            if version != _SOCKS_VERSION or command != _SOCKS_CONNECT or port != self._port:
                client.sendall(_SOCKS_REFUSED)
                _shutdown_and_close(client)
                return
            upstream = self._dial_any()
        except OSError:
            with contextlib.suppress(OSError):
                client.sendall(_SOCKS_REFUSED)
            _shutdown_and_close(client)
            return
        if not self._track(upstream):
            _shutdown_and_close(upstream)
            _shutdown_and_close(client)
            return
        client.settimeout(None)
        upstream.settimeout(None)
        client.sendall(_SOCKS_OK)
        conduit = _Conduit(client, upstream, grace=self._grace)
        threading.Thread(target=_pump, args=(upstream, client, conduit), daemon=True).start()
        _pump(client, upstream, conduit)


def _shutdown_and_close(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


class _Conduit:
    """The two directions of one tunnelled connection, and when they end.

    A direction that reaches EOF has only learned that ITS peer stopped writing.
    Ending both ends there would cut off a client that half-closes after its
    request — the HTTP/1.0 shape, and what ``curl`` does with ``--http1.0`` —
    before its response arrives. So a clean EOF is passed on as a half-close and
    the other direction goes on delivering.

    The other direction does not get forever: a peer that never answers must not
    hold two sockets and a thread, so the first direction to finish waits out
    ``grace`` — the same deadline the tunnel dialled with — and then ends
    everything. An error (including the teardown ``close()`` performs) ends both
    at once, since there is no half-open connection left to drain.
    """

    def __init__(self, client: socket.socket, upstream: socket.socket, *, grace: float) -> None:
        self._sockets = (client, upstream)
        self._grace = grace
        self._settled = threading.Event()
        self._lock = threading.Lock()
        self._remaining = 2

    def finished(self, sink: socket.socket, *, aborted: bool) -> None:
        with self._lock:
            self._remaining -= 1
            last = self._remaining <= 0
        if aborted or last:
            self._settled.set()
            self._close()
            return
        with contextlib.suppress(OSError):
            sink.shutdown(socket.SHUT_WR)
        if not self._settled.wait(self._grace):
            self._close()

    def _close(self) -> None:
        for sock in self._sockets:
            _shutdown_and_close(sock)


def _pump(source: socket.socket, sink: socket.socket, conduit: _Conduit) -> None:
    """Copy ``source`` into ``sink`` until ``source`` ends, then hand the ending
    to :class:`_Conduit`, which decides whether it is a half-close to pass on or
    the end of the connection."""
    aborted = False
    try:
        while data := source.recv(_TUNNEL_CHUNK):
            sink.sendall(data)
    except OSError:
        aborted = True
    finally:
        conduit.finished(sink, aborted=aborted)


__all__ = [
    "FORWARD_PROXY_ENV",
    "HTTPX_PROXY_ENV",
    "METADATA_HOSTNAMES",
    "PRIMP_PROXY_ENV",
    "CanonicalUrl",
    "Dialer",
    "EgressPolicy",
    "EgressRefusedError",
    "PinnedAsyncTransport",
    "PinnedTunnel",
    "Resolver",
    "VettedTarget",
    "address_is_public",
    "address_refusal",
    "canonicalize_url",
    "configured_proxy_env",
    "embedded_addresses",
    "parse_address",
    "parse_allowlist",
    "parse_ipv4_spelling",
    "proxy_egress_allowed",
    "proxy_refusal",
    "redirect_target",
    "system_resolver",
    "vet_url",
    "vet_url_async",
]
