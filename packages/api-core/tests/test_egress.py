"""`alkera_core.egress`: one strict URL reading, every address classified, and a
connection that goes where the check looked.

The real-wire proof for `web.fetch` lives with the tool
(`apps/cli/tests/plugins/test_web_fetch_floor.py`); this file pins the shared
pieces every caller relies on, and the httpx client the backend uses.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import threading
import time
from collections.abc import Callable

import httpx
import pytest
from alkera_core.config import EGRESS_MAX_REDIRECTS_CEILING, Settings
from alkera_core.egress import (
    FORWARD_PROXY_ENV,
    HTTPX_PROXY_ENV,
    PRIMP_PROXY_ENV,
    EgressPolicy,
    EgressRefusedError,
    PinnedAsyncTransport,
    PinnedTunnel,
    address_refusal,
    canonicalize_url,
    configured_proxy_env,
    parse_allowlist,
    proxy_refusal,
    redirect_target,
    vet_url,
)
from alkera_core.http import async_client

PUBLIC = "93.184.216.34"


@pytest.mark.parametrize(
    ("raw", "rebuilt"),
    [
        pytest.param("https://example.com", "https://example.com/", id="bare-origin"),
        pytest.param(" https://example.com/page \n", "https://example.com/page", id="outer-space"),
        pytest.param(
            "HTTPS://EXAMPLE.COM:443/A", "https://example.com/A", id="case-and-default-port"
        ),
        pytest.param("http://example.com:8080/x", "http://example.com:8080/x", id="explicit-port"),
        pytest.param("http://example.com./x", "http://example.com/x", id="trailing-dot"),
        pytest.param("http://example.com/a/./b/../c", "http://example.com/a/c", id="dot-segments"),
        pytest.param(
            "http://example.com/../../x", "http://example.com/x", id="dot-segments-past-root"
        ),
        pytest.param(
            "http://example.com/a?b=c&d=%20e", "http://example.com/a?b=c&d=%20e", id="query"
        ),
        pytest.param("http://example.com/a?", "http://example.com/a?", id="empty-query"),
        pytest.param(
            "http://example.com/a#@10.0.0.1/", "http://example.com/a", id="fragment-dropped"
        ),
        pytest.param(
            "http://example.com/ü?q=ü", "http://example.com/%C3%BC?q=%C3%BC", id="non-ascii-path"
        ),
        pytest.param("http://example.com/100%", "http://example.com/100%25", id="stray-percent"),
        pytest.param("http://bücher.example/", "http://xn--bcher-kva.example/", id="idn"),
        pytest.param("http://0300.0250.0.1/", "http://192.168.0.1/", id="octal-normalised"),
        pytest.param("http://0x5db8d822/", "http://93.184.216.34/", id="hex-normalised"),
        pytest.param("http://[2606:2800:0220:0001::1]/", "http://[2606:2800:220:1::1]/", id="ipv6"),
        pytest.param("http://[::1]:8080/", "http://[::1]:8080/", id="ipv6-port"),
    ],
)
def test_a_url_is_rebuilt_into_one_reading(raw: str, rebuilt: str) -> None:
    assert canonicalize_url(raw).url == rebuilt
    # Rebuilding is a fixed point: the client is handed a string that reads the same.
    assert canonicalize_url(rebuilt).url == rebuilt


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        pytest.param("ftp://example.com/", "scheme", id="ftp"),
        pytest.param("file:///etc/passwd", "scheme", id="file"),
        pytest.param("example.com/x", "shape", id="no-scheme"),
        pytest.param("http:/example.com", "shape", id="one-slash"),
        pytest.param("http://", "host", id="empty-authority"),
        pytest.param("http://:80/", "host", id="port-only"),
        pytest.param("http://./", "host", id="dot-only"),
        pytest.param("http://user@example.com/", "userinfo", id="user"),
        pytest.param("http://user:pw@example.com/", "userinfo", id="user-password"),
        pytest.param("http://@example.com/", "userinfo", id="empty-userinfo"),
        pytest.param("http://10.0.0.1\\@example.com/", "characters", id="backslash"),
        pytest.param("http://example.com/a\\b", "characters", id="backslash-in-path"),
        pytest.param("http://exa mple.com/", "characters", id="space"),
        pytest.param("http://example.com/a\r\nHost: x", "characters", id="crlf"),
        pytest.param("http://example.com/\x00", "characters", id="nul"),
        pytest.param("http://example.com/\x7f", "characters", id="del"),
        pytest.param("http://ex%61mple.com/", "authority", id="percent-in-host"),
        pytest.param("http://example.com%2f@10.0.0.1/", "userinfo", id="percent-delimiter"),
        pytest.param("http://example.com:/", "port", id="empty-port"),
        pytest.param("http://example.com:0/", "port", id="port-zero"),
        pytest.param("http://example.com:65536/", "port", id="port-too-large"),
        pytest.param("http://example.com:8o/", "port", id="port-not-digits"),
        pytest.param("http://example.com:80:80/", "port", id="two-ports"),
        pytest.param("http://[::1/", "host", id="unclosed-bracket"),
        pytest.param("http://[::1]x/", "host", id="junk-after-bracket"),
        pytest.param("http://[fe80::1%25en0]/", "authority", id="zone-id"),
        pytest.param("http://[1.2.3.4]/", "host", id="v4-in-brackets"),
        pytest.param("http://1.2.3.4.5/", "host", id="five-part-number"),
        pytest.param("http://1.2.3.256/", "host", id="octet-overflow"),
        pytest.param("http://08.0.0.1/", "host", id="bad-octal"),
        pytest.param("http://4294967296/", "host", id="number-overflow"),
        pytest.param("http://a.b.0x/x", "host", id="name-ending-in-a-number"),
        pytest.param("http://exa_mple.com/", "host", id="underscore"),
        pytest.param("http://-example.com/", "host", id="leading-hyphen"),
        pytest.param("http://a..b/", "host", id="empty-label"),
        pytest.param("http://" + "a" * 64 + ".com/", "host", id="label-too-long"),
        pytest.param("http://⒈com/", "host", id="idna-disallowed"),
        pytest.param("http://example.com/%2e%2e/etc", "path", id="encoded-dot-dot"),
        pytest.param("http://example.com/a/%2E/b", "path", id="encoded-dot"),
        pytest.param("http://example.com/a/.%2e/b", "path", id="half-encoded-dot-dot"),
    ],
)
def test_an_ambiguous_url_is_refused(raw: str, code: str) -> None:
    with pytest.raises(EgressRefusedError) as refused:
        canonicalize_url(raw)
    assert refused.value.code == code


@pytest.mark.parametrize(
    ("address", "public"),
    [
        pytest.param(PUBLIC, True, id="public-v4"),
        pytest.param("2606:2800:220:1::1", True, id="public-v6"),
        pytest.param("64:ff9b::5db8:d822", True, id="nat64-of-a-public-address"),
        # 6to4 is a retired relay scheme: not routable whatever it wraps.
        pytest.param("2002:5db8:d822::1", False, id="6to4-of-a-public-address"),
        pytest.param("0.0.0.0", False, id="unspecified-v4"),
        pytest.param("0.1.2.3", False, id="this-network"),
        pytest.param("10.1.2.3", False, id="rfc1918-10"),
        pytest.param("172.31.255.255", False, id="rfc1918-172"),
        pytest.param("192.168.0.1", False, id="rfc1918-192"),
        pytest.param("100.64.0.1", False, id="cgnat"),
        pytest.param("127.0.0.1", False, id="loopback"),
        pytest.param("127.255.255.254", False, id="loopback-high"),
        pytest.param("169.254.169.254", False, id="metadata"),
        pytest.param("192.0.0.8", False, id="ietf-protocol"),
        pytest.param("192.0.2.1", False, id="documentation-1"),
        pytest.param("198.51.100.1", False, id="documentation-2"),
        pytest.param("203.0.113.1", False, id="documentation-3"),
        pytest.param("198.18.0.1", False, id="benchmark"),
        pytest.param("224.0.0.1", False, id="multicast"),
        pytest.param("240.0.0.1", False, id="reserved"),
        pytest.param("255.255.255.255", False, id="broadcast"),
        pytest.param("::", False, id="unspecified-v6"),
        pytest.param("::1", False, id="loopback-v6"),
        pytest.param("fe80::1", False, id="link-local-v6"),
        pytest.param("fc00::1", False, id="unique-local"),
        pytest.param("fd00:ec2::254", False, id="metadata-v6"),
        pytest.param("168.63.129.16", False, id="azure-wire-server"),
        pytest.param("100.100.100.200", False, id="alibaba-metadata"),
        pytest.param("192.0.0.192", False, id="oracle-metadata"),
        pytest.param("ff02::1", False, id="multicast-v6"),
        pytest.param("2001:db8::1", False, id="documentation-v6"),
        pytest.param("::ffff:10.0.0.1", False, id="mapped-private"),
        pytest.param("::ffff:169.254.169.254", False, id="mapped-metadata"),
        pytest.param("::10.0.0.1", False, id="compatible-private"),
        pytest.param("64:ff9b::a9fe:a9fe", False, id="nat64-metadata"),
        pytest.param("64:ff9b::7f00:1", False, id="nat64-loopback"),
        pytest.param("2002:7f00:1::", False, id="6to4-loopback"),
        pytest.param("2002:a00:1::1", False, id="6to4-private"),
        pytest.param("2001:0:7f00:1::", False, id="teredo"),
    ],
)
def test_every_address_family_is_classified(address: str, public: bool) -> None:
    assert (address_refusal(ipaddress.ip_address(address)) is None) is public


def _resolver(*answers: str):  # type: ignore[no-untyped-def]
    return lambda host, port: list(answers)


def test_a_name_is_refused_when_any_record_is_private() -> None:
    assert vet_url("http://ok.example/", resolver=_resolver(PUBLIC)).pinned == ipaddress.ip_address(
        PUBLIC
    )
    for answers in ((PUBLIC, "10.0.0.1"), ("10.0.0.1", PUBLIC), (PUBLIC, "fe80::1%eth0")):
        with pytest.raises(EgressRefusedError) as refused:
            vet_url("http://split.example/", resolver=_resolver(*answers))
        assert refused.value.code == "private"


@pytest.mark.parametrize(
    "host", ["localhost", "LOCALHOST.", "api.localhost", "nas.local", "db.internal"]
)
def test_a_local_name_is_refused_without_a_lookup(host: str) -> None:
    def never(host: str, port: int) -> list[str]:
        raise AssertionError("a local name must not be resolved")

    with pytest.raises(EgressRefusedError) as refused:
        vet_url(f"http://{host}/", resolver=never)
    assert refused.value.code == "local_name"


def test_a_redirect_is_joined_against_the_canonical_url_and_stays_untrusted() -> None:
    current = canonicalize_url("https://site.example/a/b?x=1")
    assert redirect_target(current, "/moved") == "https://site.example/moved"
    assert redirect_target(current, "c") == "https://site.example/a/c"
    assert redirect_target(current, "//other.example/x") == "https://other.example/x"
    with pytest.raises(EgressRefusedError):
        redirect_target(current, "http://10.0.0.1\\@site.example/")
    # A joined URL is only a string: it goes back through the floor.
    with pytest.raises(EgressRefusedError):
        vet_url(redirect_target(current, "http://169.254.169.254/latest/"))


# --- the operator opt-out -----------------------------------------------------


def test_the_allowlist_admits_exactly_what_the_operator_wrote() -> None:
    policy = EgressPolicy.from_entries(["10.20.0.0/16", "192.168.5.5", "Wiki.Corp.Example."])
    assert vet_url("http://10.20.3.4/", policy=policy).pinned == ipaddress.ip_address("10.20.3.4")
    assert vet_url("http://192.168.5.5/", policy=policy)
    assert vet_url("http://wiki.corp.example/", policy=policy, resolver=_resolver("172.16.0.9"))
    for url, resolver in (
        ("http://10.21.0.1/", None),
        ("http://192.168.5.6/", None),
        ("http://127.0.0.1/", None),
        ("http://other.corp.example/", _resolver("172.16.0.9")),
    ):
        with pytest.raises(EgressRefusedError):
            vet_url(url, policy=policy, resolver=resolver)


@pytest.mark.parametrize(
    "policy",
    [
        pytest.param(EgressPolicy(allow_private=True), id="allow-private"),
        pytest.param(EgressPolicy(allowed_hosts=frozenset({"meta.example"})), id="allowed-host"),
    ],
)
@pytest.mark.parametrize(
    "address",
    [
        pytest.param("169.254.169.254", id="link-local"),
        pytest.param("fe80::1", id="link-local-v6"),
        pytest.param("fd00:ec2::254", id="aws-v6"),
        pytest.param("168.63.129.16", id="azure-wire-server"),
        pytest.param("100.100.100.200", id="alibaba"),
        pytest.param("192.0.0.192", id="oracle"),
        # The same service under an IPv6 spelling that delivers over IPv4.
        pytest.param("::ffff:169.254.169.254", id="mapped"),
        pytest.param("::ffff:a9fe:a9fe", id="mapped-hex"),
        pytest.param("::169.254.169.254", id="compatible"),
        pytest.param("64:ff9b::169.254.169.254", id="nat64"),
        pytest.param("2002:a9fe:a9fe::", id="6to4"),
        pytest.param("2001:0:a9fe:a9fe::", id="teredo"),
        pytest.param("::ffff:168.63.129.16", id="mapped-azure"),
    ],
)
def test_no_policy_opens_the_metadata_range(policy: EgressPolicy, address: str) -> None:
    with pytest.raises(EgressRefusedError):
        vet_url("http://meta.example/", policy=policy, resolver=_resolver(address))
    literal = f"[{address}]" if ":" in address else address
    with pytest.raises(EgressRefusedError):
        vet_url(f"http://{literal}/latest/meta-data/", policy=policy)


def test_allow_private_reaches_a_private_network_but_not_a_bad_spelling() -> None:
    policy = EgressPolicy(allow_private=True)
    assert vet_url("https://sso.corp.internal/", policy=policy, resolver=_resolver("10.0.1.5"))
    with pytest.raises(EgressRefusedError):
        vet_url("https://10.0.1.5\\@idp.example/", policy=policy)


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param("0.0.0.0/0", id="everything-v4"),
        pytest.param("::/0", id="everything-v6"),
        pytest.param("169.254.169.254", id="metadata-address"),
        pytest.param("169.254.0.0/16", id="link-local"),
        pytest.param("128.0.0.0/1", id="covers-link-local"),
        pytest.param("fd00:ec2::/32", id="metadata-v6"),
        pytest.param("168.63.129.16", id="azure-wire-server"),
        pytest.param("100.100.100.0/24", id="covers-alibaba"),
        pytest.param("192.0.0.192/32", id="oracle"),
        pytest.param("::ffff:169.254.0.0/112", id="mapped-link-local"),
        pytest.param("::ffff:a9fe:a9fe/128", id="mapped-metadata"),
        pytest.param("::ffff:10.0.0.0/104", id="mapped-private-is-an-ipv4-range"),
        pytest.param("64:ff9b::a9fe:0/112", id="nat64-link-local"),
        pytest.param("64:ff9b::/96", id="nat64-everything"),
        pytest.param("::a9fe:a9fe/128", id="compatible-metadata"),
        pytest.param("2002:a9fe::/32", id="6to4-link-local"),
        pytest.param("2001:0:a9fe::/48", id="teredo-link-local"),
        pytest.param("http://wiki.corp/", id="a-url"),
        pytest.param("wiki_corp", id="not-a-hostname"),
        pytest.param("10.0.0.0/33", id="bad-prefix"),
    ],
)
def test_a_dangerous_or_malformed_allowlist_entry_is_refused_at_boot(entry: str) -> None:
    with pytest.raises(ValueError):
        parse_allowlist([entry])
    with pytest.raises(ValueError, match="EGRESS_PRIVATE_ALLOWLIST"):
        Settings(egress_private_allowlist=f"10.0.0.0/8, {entry}")


def test_the_allowlist_setting_defaults_empty_and_parses_a_list() -> None:
    assert Settings().egress_private_allowlist_entries == []
    configured = Settings(egress_private_allowlist="10.0.0.0/8, wiki.corp.example ,")
    assert configured.egress_private_allowlist_entries == ["10.0.0.0/8", "wiki.corp.example"]
    for out_of_range in (-1, EGRESS_MAX_REDIRECTS_CEILING + 1):
        with pytest.raises(ValueError, match="EGRESS_MAX_REDIRECTS"):
            Settings(egress_max_redirects=out_of_range)
    assert Settings(egress_max_redirects=0).egress_max_redirects == 0
    assert (
        Settings(egress_max_redirects=EGRESS_MAX_REDIRECTS_CEILING).egress_max_redirects
        == EGRESS_MAX_REDIRECTS_CEILING
    )


# --- the guarded httpx client ---------------------------------------------------


class _Upstream:
    """Stands where the network would: records what the guarded client dialed."""

    def __init__(self, redirects: dict[str, str] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.redirects = redirects or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        location = self.redirects.get(request.headers["host"] + request.url.path)
        if location:
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, json={"ok": True})


class _Dns:
    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self.answers = answers
        self.lookups: list[str] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.lookups.append(host)
        queue = self.answers[host]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _client(upstream: _Upstream, dns: _Dns, **kwargs: object) -> httpx.AsyncClient:
    options: dict[str, object] = {"egress_policy": EgressPolicy(), "trust_env": False}
    options.update(kwargs)
    return async_client(
        transport=httpx.MockTransport(upstream),
        egress_resolver=dns,
        **options,  # type: ignore[arg-type]
    )


async def test_the_guarded_client_dials_the_vetted_address_as_the_original_host() -> None:
    upstream, dns = _Upstream(), _Dns({"idp.example": [[PUBLIC], ["127.0.0.1"]]})
    async with _client(upstream, dns) as client:
        response = await client.get("https://IDP.example/.well-known/openid-configuration?x=1")
    assert response.status_code == 200
    (sent,) = upstream.requests
    assert str(sent.url) == f"https://{PUBLIC}/.well-known/openid-configuration?x=1"
    assert sent.headers["host"] == "idp.example"
    assert sent.extensions["sni_hostname"] == "idp.example"
    # One lookup: the later (loopback) answer is never asked for, let alone dialed.
    assert dns.lookups == ["idp.example"]
    # The caller still sees the URL it asked for, not the pin.
    assert response.request.url.host == "idp.example"


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://inward.example/x", id="name-resolving-inward"),
        pytest.param("https://split.example/x", id="one-private-record"),
        pytest.param("https://10.0.0.5/x", id="private-literal"),
        pytest.param("https://169.254.169.254/latest/", id="metadata"),
        pytest.param("https://[::ffff:10.0.0.5]/x", id="mapped"),
        pytest.param("https://0x7f.1/x", id="hex-loopback"),
        pytest.param("https://localhost/x", id="localhost"),
        pytest.param("https://user@idp.example/x", id="userinfo"),
    ],
)
async def test_the_guarded_client_refuses_before_anything_is_sent(url: str) -> None:
    upstream = _Upstream()
    dns = _Dns(
        {
            "idp.example": [[PUBLIC]],
            "inward.example": [["10.0.0.5"]],
            "split.example": [[PUBLIC, "10.0.0.5"]],
        }
    )
    async with _client(upstream, dns) as client:
        with pytest.raises(httpx.ConnectError, match="refused"):
            await client.get(url)
    assert upstream.requests == []


async def test_a_scheme_outside_the_callers_list_is_refused() -> None:
    upstream, dns = _Upstream(), _Dns({"idp.example": [[PUBLIC]]})
    async with _client(upstream, dns, egress_schemes=("https",)) as client:
        with pytest.raises(httpx.ConnectError, match="refused"):
            await client.get("http://idp.example/x")
        assert (await client.get("https://idp.example/x")).status_code == 200
    assert [r.url.scheme for r in upstream.requests] == ["https"]


async def test_a_followed_redirect_is_vetted_at_the_hop() -> None:
    upstream = _Upstream(
        {
            "start.example/a": "https://next.example/b",
            "next.example/b": "https://inward.example/secret",
        }
    )
    dns = _Dns(
        {
            "start.example": [[PUBLIC]],
            "next.example": [["151.101.1.69"]],
            "inward.example": [["10.0.0.5"]],
        }
    )
    async with _client(upstream, dns, follow_redirects=True) as client:
        with pytest.raises(httpx.ConnectError, match="refused"):
            await client.get("https://start.example/a")
    assert [r.headers["host"] for r in upstream.requests] == ["start.example", "next.example"]
    assert [r.url.host for r in upstream.requests] == [PUBLIC, "151.101.1.69"]


async def test_allow_private_pins_a_private_idp_too() -> None:
    upstream, dns = _Upstream(), _Dns({"sso.corp.example": [["10.0.1.5"]]})
    async with _client(upstream, dns, egress_policy=EgressPolicy(allow_private=True)) as client:
        assert (await client.get("https://sso.corp.example/x")).status_code == 200
    assert upstream.requests[0].url.host == "10.0.1.5"


async def test_a_vetted_address_that_does_not_answer_is_skipped_for_the_next() -> None:
    dead, live = "93.184.216.34", "151.101.1.69"
    attempted: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        attempted.append(request.url.host)
        if request.url.host == dead:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200)

    dns = _Dns({"multi.example": [[dead, live]], "down.example": [[dead]]})
    transport = PinnedAsyncTransport(resolver=dns, inner=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get("https://multi.example/x")).status_code == 200
        assert attempted == [dead, live]
        # Only vetted addresses are tried, and the last failure is the caller's.
        with pytest.raises(httpx.ConnectError, match="connection refused"):
            await client.get("https://down.example/x")
    assert attempted == [dead, live, dead]


def test_an_unguarded_client_is_unchanged() -> None:
    client = async_client(transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    assert type(client._transport) is httpx.MockTransport


# --- the tunnel -----------------------------------------------------------------


def _socks_connect(proxy_port: int, host: bytes, port: int) -> tuple[socket.socket, bytes]:
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    sock.sendall(b"\x05\x01\x00")
    assert sock.recv(2) == b"\x05\x00"
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + port.to_bytes(2, "big"))
    return sock, sock.recv(10)


def test_the_tunnel_dials_only_the_vetted_address_and_port() -> None:
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen()
    dialed: list[tuple[str, int]] = []

    def dial(address: str, port: int, timeout: float) -> socket.socket:
        dialed.append((address, port))
        return socket.create_connection(echo.getsockname(), timeout=timeout)

    target = vet_url("http://site.example:8443/", resolver=_resolver(PUBLIC))
    with PinnedTunnel(target, timeout=5, dial=dial) as tunnel:
        proxy_port = int(tunnel.proxy_url.rsplit(":", 1)[1])
        # Whatever name the client asks for, the vetted address is what is dialed.
        sock, reply = _socks_connect(proxy_port, b"127.0.0.1", 8443)
        assert reply[1] == 0
        upstream, _ = echo.accept()
        sock.sendall(b"ping")
        assert upstream.recv(4) == b"ping"
        upstream.sendall(b"pong")
        assert sock.recv(4) == b"pong"
        sock.close()
        upstream.close()
        # A different port is not the thing that was vetted.
        other, refused = _socks_connect(proxy_port, b"site.example", 22)
        assert refused[1] != 0
        other.close()
    assert dialed == [(PUBLIC, 8443)]
    echo.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", proxy_port), timeout=1)


def _settled(condition: Callable[[], bool], *, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def _threads_started_since(baseline: set[threading.Thread]) -> set[threading.Thread]:
    """The threads this test is responsible for.

    A count would not do: an unrelated thread left by an earlier test in the same
    worker can exit at any moment, which moves a total in the one direction a
    lower bound cannot tolerate. A set difference only ever sees threads that
    appeared AFTER the baseline was taken, so both bounds are load-bearing."""
    return set(threading.enumerate()) - baseline


@pytest.mark.parametrize("connected", [False, True], ids=["idle", "established"])
def test_closing_the_tunnel_releases_its_threads_and_port(connected: bool) -> None:
    """After ``close()`` nothing of the tunnel survives: not the accept thread,
    not the pumps, not the bound port. Linux does not wake a thread blocked in
    ``accept``/``recv`` on a merely closed socket, so this is the platform-neutral
    statement of the teardown contract."""
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen()
    baseline = set(threading.enumerate())
    target = vet_url("http://site.example:8443/", resolver=_resolver(PUBLIC))
    tunnel = PinnedTunnel(
        target,
        timeout=5,
        dial=lambda a, p, t: socket.create_connection(echo.getsockname(), timeout=t),
    )
    proxy_port = int(tunnel.proxy_url.rsplit(":", 1)[1])
    client = upstream = None
    if connected:
        client, reply = _socks_connect(proxy_port, b"site.example", 8443)
        assert reply[1] == 0
        upstream, _ = echo.accept()
        client.sendall(b"ping")
        assert upstream.recv(4) == b"ping"
        # The accept thread plus BOTH pumps are running — an upper bound alone
        # would pass just as well if none of them had started.
        assert _settled(lambda: len(_threads_started_since(baseline)) == 3)
    else:
        assert len(_threads_started_since(baseline)) == 1
    # Neither peer closes: the upstream lingers the way a keep-alive server does.
    tunnel.close()
    assert _settled(lambda: not _threads_started_since(baseline)), threading.enumerate()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", proxy_port), timeout=1)
    if connected:
        assert client is not None and upstream is not None
        # Both ends of the pumped connection were ended, not left to the peers.
        assert client.recv(1) == b""
        assert upstream.recv(1) == b""
        client.close()
        upstream.close()
    echo.close()


def test_the_tunnel_moves_to_the_next_vetted_address_when_one_refuses() -> None:
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen()
    dead, live = "93.184.216.34", "151.101.1.69"
    dialed: list[str] = []

    def dial(address: str, port: int, timeout: float) -> socket.socket:
        dialed.append(address)
        if address == dead:
            raise ConnectionRefusedError(address)
        return socket.create_connection(echo.getsockname(), timeout=timeout)

    target = vet_url("http://multi.example:8443/", resolver=_resolver(dead, live))
    with PinnedTunnel(target, timeout=5, dial=dial) as tunnel:
        proxy_port = int(tunnel.proxy_url.rsplit(":", 1)[1])
        client, reply = _socks_connect(proxy_port, b"multi.example", 8443)
        assert reply[1] == 0
        upstream, _ = echo.accept()
        client.close()
        upstream.close()
    assert dialed == [dead, live]
    only_dead = vet_url("http://down.example:8443/", resolver=_resolver(dead))
    with PinnedTunnel(only_dead, timeout=5, dial=dial) as tunnel:
        proxy_port = int(tunnel.proxy_url.rsplit(":", 1)[1])
        client, reply = _socks_connect(proxy_port, b"down.example", 8443)
        assert reply[1] != 0
        client.close()
    assert dialed == [dead, live, dead]
    echo.close()


def test_a_peer_closing_ends_the_whole_pumped_connection() -> None:
    """The upstream going away is passed on to the client, and the connection is
    not left holding threads on the chance that a gone peer says more."""
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen()
    baseline = set(threading.enumerate())
    target = vet_url("http://site.example:8443/", resolver=_resolver(PUBLIC))
    with PinnedTunnel(
        target,
        timeout=5,
        half_close_grace=0.2,
        dial=lambda a, p, t: socket.create_connection(echo.getsockname(), timeout=t),
    ) as tunnel:
        proxy_port = int(tunnel.proxy_url.rsplit(":", 1)[1])
        client, _ = _socks_connect(proxy_port, b"site.example", 8443)
        upstream, _ = echo.accept()
        upstream.close()
        assert client.recv(1) == b""
        # The client never speaks again, so the other direction is reaped once
        # the grace runs out: only the accept thread is left.
        assert _settled(lambda: len(_threads_started_since(baseline)) == 1), threading.enumerate()
        client.close()
    echo.close()


def test_a_client_that_half_closes_still_receives_its_response() -> None:
    """``shutdown(SHUT_WR)`` after the request is the HTTP/1.0 shape (and what
    ``curl --http1.0`` does). It says "I am done asking", not "hang up" — a proxy
    that tears the connection down there loses the response the client asked for.
    """
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen()
    target = vet_url("http://site.example:8443/", resolver=_resolver(PUBLIC))
    with PinnedTunnel(
        target,
        timeout=5,
        dial=lambda a, p, t: socket.create_connection(echo.getsockname(), timeout=t),
    ) as tunnel:
        proxy_port = int(tunnel.proxy_url.rsplit(":", 1)[1])
        client, _ = _socks_connect(proxy_port, b"site.example", 8443)
        upstream, _ = echo.accept()
        client.sendall(b"GET / HTTP/1.0\r\n\r\n")
        client.shutdown(socket.SHUT_WR)
        # The half-close reaches the upstream as EOF, not as a dead connection.
        assert upstream.recv(64) == b"GET / HTTP/1.0\r\n\r\n"
        assert upstream.recv(1) == b""
        upstream.sendall(b"HTTP/1.0 200 OK\r\n\r\nhi")
        upstream.close()
        client.settimeout(5)
        received = b""
        while chunk := client.recv(4096):
            received += chunk
        assert received == b"HTTP/1.0 200 OK\r\n\r\nhi"
        client.close()
    echo.close()


def test_a_half_closed_connection_is_not_held_open_forever() -> None:
    """The other side of the half-close: a peer that never answers must not keep
    a thread and two sockets for the life of the process."""
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen()
    baseline = set(threading.enumerate())
    target = vet_url("http://site.example:8443/", resolver=_resolver(PUBLIC))
    with PinnedTunnel(
        target,
        timeout=30,
        half_close_grace=0.2,
        dial=lambda a, p, t: socket.create_connection(echo.getsockname(), timeout=t),
    ) as tunnel:
        proxy_port = int(tunnel.proxy_url.rsplit(":", 1)[1])
        client, _ = _socks_connect(proxy_port, b"site.example", 8443)
        upstream, _ = echo.accept()
        client.shutdown(socket.SHUT_WR)
        # The upstream says nothing at all. The grace expires and both pumps go.
        assert _settled(lambda: len(_threads_started_since(baseline)) == 1), threading.enumerate()
        assert upstream.recv(1) == b""
        upstream.close()
        client.close()
    echo.close()


# --- the name lookup has a deadline of its own --------------------------------


def test_a_resolver_that_never_answers_does_not_hold_the_fetch() -> None:
    """No HTTP timeout covers the lookup — the client's clock starts at connect —
    so without a deadline here one unresponsive authoritative server holds the
    request for the OS resolver's own bound (minutes)."""
    started = threading.Event()
    release = threading.Event()

    def stalls(host: str, port: int) -> list[str]:
        started.set()
        release.wait(30)
        return [PUBLIC]

    began = time.monotonic()
    with pytest.raises(EgressRefusedError) as refused:
        vet_url("http://slow.example/", resolver=stalls, dns_timeout=0.2)
    elapsed = time.monotonic() - began
    release.set()
    assert started.is_set()
    assert refused.value.code == "resolve"
    # The refusal is the lookup's, not the resolver's: it came back long before
    # the resolver did.
    assert elapsed < 5


def test_the_deadline_does_not_shorten_a_lookup_that_answers() -> None:
    """The asymmetric case: a resolver that is merely slow, not hung, still
    decides the fetch."""

    def slow_but_fine(host: str, port: int) -> list[str]:
        time.sleep(0.05)
        return [PUBLIC, "10.0.0.7"]

    with pytest.raises(EgressRefusedError) as refused:
        vet_url("http://mixed.example/", resolver=slow_but_fine, dns_timeout=5)
    assert refused.value.code == "private"


def test_a_resolver_failure_is_still_the_resolver_failure() -> None:
    """The deadline wraps the lookup; it must not swallow what the lookup said."""

    def broken(host: str, port: int) -> list[str]:
        raise OSError("nodename nor servname provided")

    with pytest.raises(EgressRefusedError) as refused:
        vet_url("http://gone.example/", resolver=broken, dns_timeout=5)
    assert refused.value.code == "resolve"
    assert "nodename" in str(refused.value)


def test_the_dns_deadline_comes_from_settings_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that passes nothing gets the deployment's number, not a constant
    buried in the guard."""
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "egress_dns_timeout_seconds", 0.1)
    release = threading.Event()

    def stalls(host: str, port: int) -> list[str]:
        release.wait(30)
        return [PUBLIC]

    began = time.monotonic()
    with pytest.raises(EgressRefusedError):
        vet_url("http://slow.example/", resolver=stalls)
    elapsed = time.monotonic() - began
    release.set()
    assert elapsed < 5


# --- a fetch that cannot be pinned --------------------------------------------

#: A lowercase-only spelling is a distinct variable on POSIX and cannot exist on
#: Windows, whose environment is case-insensitive — ``os.environ`` upper-cases
#: every key, so setting ``http_proxy`` there IS setting ``HTTP_PROXY``.
_POSIX_ONLY_SPELLING = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows' environment is case-insensitive and os.environ upper-cases its keys, "
        "so a lowercase-only spelling cannot exist there"
    ),
)


def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both spellings of every proxy variable gone: the guard reads the
    environment case-insensitively, so one the runner inherited in the other
    case would still be a proxy standing in front of the test."""
    for env in FORWARD_PROXY_ENV:
        monkeypatch.delenv(env, raising=False)
        monkeypatch.delenv(env.lower(), raising=False)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("HTTPS_PROXY", id="https-proxy"),
        pytest.param("http_proxy", id="lowercase-http-proxy", marks=_POSIX_ONLY_SPELLING),
        pytest.param("Http_Proxy", id="mixed-case-http-proxy"),
        pytest.param("ALL_PROXY", id="all-proxy"),
        pytest.param("PRIMP_PROXY", id="primp-proxy"),
    ],
)
def test_a_forward_proxy_is_refused_unless_the_deployment_allowed_it(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind a proxy the pin is gone: the proxy does its own lookup, so a record
    that answers public to us and private to it walks through. That is the
    deployment's trade to make, in writing.

    Whatever the operator spelled it, the guard finds it and the surfaces name it
    canonically — a client honours either spelling, so one the guard missed would
    be a proxy it let through.
    """
    from alkera_core.config import settings

    canonical = name.upper()
    _no_proxy(monkeypatch)
    assert proxy_refusal() is None

    monkeypatch.setenv(name, "http://proxy.corp.internal:3128")
    if name != canonical and sys.platform != "win32":
        # On POSIX the guard's case-insensitive read is what finds it: this is a
        # distinct variable, and the canonical name names nothing.
        assert os.environ.get(name) and canonical not in os.environ
    monkeypatch.setattr(settings, "egress_allow_proxy", False)
    refusal = proxy_refusal()
    assert refusal is not None
    assert refusal.code == "proxy"
    assert canonical in str(refusal)
    assert configured_proxy_env() == (canonical,)

    monkeypatch.setattr(settings, "egress_allow_proxy", True)
    assert proxy_refusal() is None


def test_an_empty_proxy_variable_is_not_a_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HTTPS_PROXY=`` is how a container image turns the inherited proxy OFF;
    reading it as "a proxy is configured" would refuse every guarded fetch on a
    deployment that has none."""
    _no_proxy(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "")
    assert configured_proxy_env() == ()
    assert proxy_refusal() is None


@pytest.mark.asyncio
async def test_the_unpinned_transport_refuses_before_it_reaches_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is the transport's, so it reaches a caller as the unreachable
    endpoint it is — and the request never leaves this process."""
    from alkera_core.config import settings

    _no_proxy(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.internal:3128")
    monkeypatch.setattr(settings, "egress_allow_proxy", False)
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    transport = PinnedAsyncTransport(
        inner=httpx.MockTransport(record), resolver=_resolver(PUBLIC), pin=False
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError) as failed:
            await client.get("https://site.example/x")
    assert "EGRESS_ALLOW_PROXY" in str(failed.value)
    assert seen == []

    # Allowed: the URL is still canonicalised and vetted, only the pin is gone.
    monkeypatch.setattr(settings, "egress_allow_proxy", True)
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get("https://site.example/x")).status_code == 200
    assert [str(r.url) for r in seen] == ["https://site.example/x"]


@pytest.mark.parametrize(
    ("name", "stands_httpx_down", "stands_primp_down"),
    [
        pytest.param("HTTPS_PROXY", True, True, id="https-proxy-both"),
        pytest.param(
            "http_proxy", True, True, id="lowercase-http-proxy-both", marks=_POSIX_ONLY_SPELLING
        ),
        # Each client is judged by the variables it actually reads. primp does
        # not honour ALL_PROXY, and httpx does not read PRIMP_PROXY.
        pytest.param("ALL_PROXY", True, False, id="all-proxy-is-httpx-only"),
        pytest.param(
            "all_proxy",
            True,
            False,
            id="lowercase-all-proxy-is-httpx-only",
            marks=_POSIX_ONLY_SPELLING,
        ),
        # The table is keyed by the canonical name, not by how it was spelled.
        pytest.param("All_Proxy", True, False, id="mixed-case-all-proxy-is-httpx-only"),
        pytest.param("PRIMP_PROXY", False, True, id="primp-proxy-is-primp-only"),
    ],
)
def test_a_proxy_stands_down_only_the_client_that_reads_it(
    name: str,
    stands_httpx_down: bool,
    stands_primp_down: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Judging one client by the other's list is wrong in both directions.

    Counting ALL_PROXY against primp refuses a fetch that was perfectly pinnable
    — and, once the deployment allows proxies, makes the tunnel stand down so the
    fetch goes out DIRECT and unpinned through a proxy that was never used.
    Counting PRIMP_PROXY against httpx refuses every backend fetch on a
    deployment that only pointed the page reader at a proxy.
    """
    from alkera_core.config import settings

    _no_proxy(monkeypatch)
    monkeypatch.setattr(settings, "egress_allow_proxy", False)
    monkeypatch.setenv(name, "http://proxy.corp.internal:3128")

    assert bool(configured_proxy_env(HTTPX_PROXY_ENV)) is stands_httpx_down
    assert bool(configured_proxy_env(PRIMP_PROXY_ENV)) is stands_primp_down
    assert (proxy_refusal(HTTPX_PROXY_ENV) is not None) is stands_httpx_down
    assert (proxy_refusal(PRIMP_PROXY_ENV) is not None) is stands_primp_down
    # Whichever client it belongs to, the operator surfaces see it: the boot line
    # and the production refusal speak for the whole process.
    assert configured_proxy_env() == (name.upper(),)

    monkeypatch.setattr(settings, "egress_allow_proxy", True)
    assert proxy_refusal(HTTPX_PROXY_ENV) is None
    assert proxy_refusal(PRIMP_PROXY_ENV) is None
