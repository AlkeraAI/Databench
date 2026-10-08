"""`web.fetch` on a real wire: no spelling of a URL reaches an internal listener.

A loopback HTTP server stands in for "a service on the machine's own network"
(the metadata service, the box's daemon, a neighbour). Every row is a URL some
parser reads as that server; the assertion that matters is the hit counter —
the listener must never see a request — not merely that an error was raised.
The real `primp` client is used throughout, because the hole this file pins was
a disagreement between the floor's parser and that client's.

Names resolve through an injected resolver and connections through an injected
dialer (public test addresses routed to the local listener), so nothing here
touches the internet.
"""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from alkera_cli.plugins.plugin_base.tool import ToolError
from alkera_cli.plugins.plugin_base.web_tools import WebFetchInput, WebFetchResult, _run_fetch
from alkera_core.config import settings

PUBLIC_A = "93.184.216.34"
PUBLIC_B = "151.101.1.69"


@dataclass
class Listener:
    port: int
    hits: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    redirects: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def internal() -> Iterator[Listener]:
    """A loopback server recording every request line it receives."""
    state = Listener(port=0)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.hits.append(self.path)
            state.hosts.append(self.headers.get("Host", ""))
            location = state.redirects.get(self.path)
            body = f"INTERNAL-SECRET path={self.path}".encode()
            self.send_response(302 if location else 200)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def _getaddrinfo(*addresses: str) -> object:
    return patch(
        "socket.getaddrinfo",
        return_value=[(socket.AF_INET, 1, 6, "", (address, 0)) for address in addresses],
    )


BYPASSES = [
    pytest.param(
        "http://127.0.0.1:{port}\\@example.com/../latest/meta-data/", id="backslash-userinfo"
    ),
    pytest.param("http://127.0.0.1:{port}\\\\@example.com/x", id="double-backslash"),
    pytest.param("http:\\\\127.0.0.1:{port}/x", id="backslash-slashes"),
    pytest.param("http://example.com@127.0.0.1:{port}/x", id="at-userinfo"),
    pytest.param("http://example.com:80@127.0.0.1:{port}/x", id="at-userinfo-with-port"),
    pytest.param("http://127.0.0.1:{port}#@example.com/x", id="fragment-at"),
    pytest.param("http://127.0.0.1:{port}?@example.com/x", id="query-at"),
    pytest.param("http://0177.0.0.1:{port}/octal", id="octal-ipv4"),
    pytest.param("http://0x7f.0.0.1:{port}/hex", id="hex-ipv4"),
    pytest.param("http://0x7f000001:{port}/hex32", id="hex-32bit-ipv4"),
    pytest.param("http://2130706433:{port}/decimal", id="decimal-ipv4"),
    pytest.param("http://127.1:{port}/short", id="short-ipv4"),
    pytest.param("http://0177.1:{port}/short-octal", id="short-octal-ipv4"),
    pytest.param("http://[::ffff:127.0.0.1]:{port}/mapped", id="ipv4-mapped-ipv6"),
    pytest.param("http://[::ffff:7f00:1]:{port}/mapped-hex", id="ipv4-mapped-ipv6-hex"),
    pytest.param("http://[::127.0.0.1]:{port}/compat", id="ipv4-compatible-ipv6"),
    pytest.param("http://[64:ff9b::7f00:1]:{port}/nat64", id="nat64"),
    pytest.param("http://[2002:7f00:1::]:{port}/6to4", id="6to4"),
    pytest.param("http://[2001:0:7f00:1::]:{port}/teredo", id="teredo"),
    pytest.param("http://[fe80::1%25lo0]:{port}/zone", id="ipv6-zone-id"),
    pytest.param("http://0.0.0.0:{port}/unspecified", id="unspecified-v4"),
    pytest.param("http://0:{port}/zero", id="zero"),
    pytest.param("http://[::]:{port}/unspecified6", id="unspecified-v6"),
    pytest.param("http://[::1]:{port}/loop6", id="loopback-v6"),
    pytest.param("http://localhost.:{port}/dot", id="localhost-trailing-dot"),
    pytest.param("http://LOCALHOST:{port}/upper", id="localhost-upper"),
    pytest.param("HtTp://127.0.0.1:{port}/scheme", id="mixed-case-scheme"),
    pytest.param("http://127.0.0.1.:{port}/dotted", id="ip-trailing-dot"),
    pytest.param("http://%31%32%37.0.0.1:{port}/pct", id="percent-encoded-host"),
    pytest.param("http://127.0.0.1%3a{port}/pct-port", id="percent-encoded-delimiter"),
    pytest.param("http://127.0.0.1:{port}/a\r\nX-Injected: 1", id="crlf-in-path"),
    pytest.param("http://127.0.0.1:{port}/a\tb", id="tab-in-path"),
    pytest.param("http://127.0.0.1\t:{port}/x", id="tab-in-authority"),
    # Fullwidth digits and the ideographic full stop: IDNA maps both to ASCII.
    pytest.param("http://\uff11\uff12\uff17.0.0.1:{port}/fullwidth", id="fullwidth-digits"),
    pytest.param(
        "http://127\u30020\u30020\u30021:{port}/ideographic-dot", id="ideographic-full-stop"
    ),
    pytest.param("http://169.254.169.254/latest/meta-data/", id="metadata-v4"),
    pytest.param("http://[fd00:ec2::254]/latest/meta-data/", id="metadata-v6"),
    pytest.param("http://100.64.0.1/cgnat", id="cgnat"),
    pytest.param("http://255.255.255.255/broadcast", id="broadcast"),
    pytest.param("http://192.0.2.1/doc", id="documentation"),
    pytest.param("http://198.18.0.1/bench", id="benchmark"),
    pytest.param("http://224.0.0.1/multicast", id="multicast"),
    pytest.param("http://127.0.0.1:0/x", id="port-zero"),
    pytest.param("http://127.0.0.1:99999/x", id="port-out-of-range"),
    pytest.param("http:///x", id="empty-host"),
    pytest.param("file:///etc/passwd", id="file-scheme"),
    pytest.param("gopher://127.0.0.1:{port}/x", id="gopher-scheme"),
]


@pytest.mark.parametrize("template", BYPASSES)
def test_no_spelling_of_an_internal_url_reaches_the_listener(
    template: str, internal: Listener
) -> None:
    url = template.format(port=internal.port)
    # `example.com` resolves publicly so a row that hides the target behind it
    # is refused for the right reason, not because the decoy failed to resolve.
    with _getaddrinfo(PUBLIC_A), pytest.raises(ToolError):
        _run_fetch(WebFetchInput(url=url))
    assert internal.hits == []


# ---------------------------------------------------------------------------
# Names, redirects and rebinding: a scripted resolver and an observed dialer
# ---------------------------------------------------------------------------


@dataclass
class Wire:
    """The two seams `_run_fetch` exposes. `answers` maps a name to the queue of
    resolver answers it gives (the last one repeats); `dialed` records every
    address the fetch actually connected to. Whatever address is dialed, the
    connection lands on the local listener — which is how a "public" address can
    be served by a test."""

    listener: Listener
    answers: dict[str, list[list[str]]]
    lookups: list[str] = field(default_factory=list)
    dialed: list[tuple[str, int]] = field(default_factory=list)

    def resolve(self, host: str, port: int) -> list[str]:
        self.lookups.append(host)
        queue = self.answers.get(host)
        if not queue:
            raise OSError(f"NXDOMAIN {host}")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def dial(self, address: str, port: int, timeout: float) -> socket.socket:
        self.dialed.append((address, port))
        return socket.create_connection(("127.0.0.1", self.listener.port), timeout=timeout)

    def fetch(self, url: str) -> WebFetchResult:
        return _run_fetch(WebFetchInput(url=url), resolver=self.resolve, dial=self.dial)


def test_a_public_page_is_fetched_through_the_vetted_address(internal: Listener) -> None:
    wire = Wire(internal, {"site.example": [[PUBLIC_A]]})
    out = wire.fetch("HTTP://Site.Example:8080/a/../page?q=1#frag")
    assert out.status == 200
    assert out.content == "INTERNAL-SECRET path=/page?q=1"
    assert out.url == "http://site.example:8080/page?q=1"
    assert wire.dialed == [(PUBLIC_A, 8080)]
    # The client still speaks AS the name: virtual hosts and TLS names survive the pin.
    assert internal.hosts == ["site.example:8080"]


def test_a_fragment_that_looks_like_an_authority_goes_to_the_real_host(
    internal: Listener,
) -> None:
    wire = Wire(internal, {"example.com": [[PUBLIC_A]]})
    out = wire.fetch("http://example.com#@127.0.0.1/")
    assert out.url == "http://example.com/"
    assert wire.dialed == [(PUBLIC_A, 80)]
    assert internal.hosts == ["example.com"]


def test_a_public_redirect_chain_is_followed_hop_by_hop(internal: Listener) -> None:
    internal.redirects["/start"] = "http://next.example/middle"
    internal.redirects["/middle"] = "/end"
    wire = Wire(internal, {"site.example": [[PUBLIC_A]], "next.example": [[PUBLIC_B]]})
    out = wire.fetch("http://site.example/start")
    assert out.content == "INTERNAL-SECRET path=/end"
    assert out.url == "http://next.example/end"
    assert internal.hits == ["/start", "/middle", "/end"]
    assert wire.dialed == [(PUBLIC_A, 80), (PUBLIC_B, 80), (PUBLIC_B, 80)]


def test_an_idn_host_is_fetched_under_its_a_label(internal: Listener) -> None:
    wire = Wire(internal, {"xn--bcher-kva.example": [[PUBLIC_A]]})
    out = wire.fetch("http://bücher.example/katalog")
    assert out.url == "http://xn--bcher-kva.example/katalog"
    assert wire.lookups == ["xn--bcher-kva.example"]
    assert internal.hosts == ["xn--bcher-kva.example"]


@pytest.mark.parametrize(
    "answers",
    [
        pytest.param([PUBLIC_A, "10.0.0.5"], id="public-then-private"),
        pytest.param(["192.168.1.9", PUBLIC_A], id="private-then-public"),
        pytest.param([PUBLIC_A, "::ffff:127.0.0.1"], id="public-and-mapped-loopback"),
        pytest.param([PUBLIC_A, "fe80::1%en0"], id="public-and-scoped-link-local"),
        pytest.param([PUBLIC_A, "64:ff9b::a00:5"], id="public-and-nat64-private"),
        pytest.param(["not-an-address"], id="unparseable-answer"),
        pytest.param([], id="no-answer"),
    ],
)
def test_one_non_public_record_refuses_the_whole_name(
    answers: list[str], internal: Listener
) -> None:
    wire = Wire(internal, {"split.example": [answers]})
    with pytest.raises(ToolError):
        wire.fetch("http://split.example/x")
    assert wire.dialed == []
    assert internal.hits == []


@pytest.mark.parametrize(
    "location",
    [
        pytest.param("http://10.0.0.5/admin", id="private-literal"),
        pytest.param("http://169.254.169.254/latest/meta-data/", id="metadata"),
        pytest.param("http://inward.example/x", id="name-resolving-inward"),
        pytest.param("http://127.0.0.1:{port}\\@site.example/x", id="backslash-userinfo"),
        pytest.param("//0177.0.0.1:{port}/x", id="scheme-relative-octal"),
        pytest.param("http://[::ffff:127.0.0.1]:{port}/x", id="mapped-loopback"),
        pytest.param("file:///etc/passwd", id="file-scheme"),
    ],
)
def test_a_redirect_inward_at_the_second_hop_is_refused_before_any_request(
    location: str, internal: Listener
) -> None:
    internal.redirects["/hop1"] = "http://next.example/hop2"
    internal.redirects["/hop2"] = location.format(port=internal.port)
    wire = Wire(
        internal,
        {
            "site.example": [[PUBLIC_A]],
            "next.example": [[PUBLIC_B]],
            "inward.example": [["10.1.2.3"]],
        },
    )
    with pytest.raises(ToolError):
        wire.fetch("http://site.example/hop1")
    # The two public hops were made; the inward one was never requested or dialed.
    assert internal.hits == ["/hop1", "/hop2"]
    assert wire.dialed == [(PUBLIC_A, 80), (PUBLIC_B, 80)]


def test_a_rebinding_resolver_cannot_move_the_connection(internal: Listener) -> None:
    # The first answer is public and is the one vetted; every later answer is
    # loopback. The connection is made to the vetted address without asking again.
    wire = Wire(internal, {"rebind.example": [[PUBLIC_A], ["127.0.0.1"]]})
    out = wire.fetch("http://rebind.example/x")
    assert out.status == 200
    assert wire.lookups == ["rebind.example"]
    assert wire.dialed == [(PUBLIC_A, 80)]


def test_a_rebinding_redirect_back_to_the_same_name_is_refused(internal: Listener) -> None:
    # Each hop is vetted afresh, so the second answer IS consulted for the second
    # hop — and refused there rather than dialed.
    internal.redirects["/x"] = "/y"
    wire = Wire(internal, {"rebind.example": [[PUBLIC_A], ["127.0.0.1"]]})
    with pytest.raises(ToolError, match="non-public"):
        wire.fetch("http://rebind.example/x")
    assert internal.hits == ["/x"]
    assert wire.dialed == [(PUBLIC_A, 80)]


def test_the_hop_limit_comes_from_settings(
    internal: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    internal.redirects.update({"/1": "/2", "/2": "/3", "/3": "/4"})
    monkeypatch.setattr(settings, "egress_max_redirects", 2)
    wire = Wire(internal, {"site.example": [[PUBLIC_A]]})
    with pytest.raises(ToolError, match="too many redirects"):
        wire.fetch("http://site.example/1")
    assert internal.hits == ["/1", "/2", "/3"]
    monkeypatch.setattr(settings, "egress_max_redirects", 3)
    assert wire.fetch("http://site.example/1").content == "INTERNAL-SECRET path=/4"


def test_only_the_operator_allowlist_admits_a_private_address(
    internal: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = Wire(internal, {"wiki.corp.example": [["10.20.0.7"]], "other.example": [["10.99.0.1"]]})
    with pytest.raises(ToolError, match="non-public"):
        wire.fetch("http://wiki.corp.example/page")
    assert wire.dialed == []
    monkeypatch.setattr(settings, "egress_private_allowlist", "10.20.0.0/16")
    assert wire.fetch("http://wiki.corp.example/page").status == 200
    assert wire.dialed == [("10.20.0.7", 80)]
    # The opt-out is as wide as the operator wrote it and no wider.
    with pytest.raises(ToolError, match="non-public"):
        wire.fetch("http://other.example/page")
    with pytest.raises(ToolError):
        wire.fetch(f"http://127.0.0.1:{internal.port}/x")
    assert wire.dialed == [("10.20.0.7", 80)]


def test_a_proxy_this_client_does_not_read_leaves_the_fetch_pinned(
    internal: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`web.fetch` is judged by the variables `primp` actually reads.

    ALL_PROXY puts a proxy in front of httpx but not in front of this client, so
    the fetch is still perfectly pinnable and the tunnel still makes the
    connection. Counting it would be wrong twice over: refused while pinnable,
    and — once the deployment allows proxies — sent out direct AND unpinned
    through a proxy that was never going to be used.
    """
    from alkera_core.egress import FORWARD_PROXY_ENV

    for env in FORWARD_PROXY_ENV:
        # Both spellings: the guard reads the environment case-insensitively.
        monkeypatch.delenv(env, raising=False)
        monkeypatch.delenv(env.lower(), raising=False)
    monkeypatch.setattr(settings, "egress_allow_proxy", False)
    monkeypatch.setenv("ALL_PROXY", "http://proxy.corp.internal:3128")

    wire = Wire(internal, {"site.example": [[PUBLIC_A]]})
    assert wire.fetch("http://site.example/page").status == 200
    # Dialed by the tunnel, at the vetted address: the pin is intact.
    assert wire.dialed == [(PUBLIC_A, 80)]


def test_a_proxy_this_client_does_read_refuses_until_the_deployment_allows_it(
    internal: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half. HTTPS_PROXY is one primp honours, so the tunnel cannot
    pin — and a fetch that cannot be pinned is refused rather than quietly
    downgraded."""
    from alkera_core.egress import FORWARD_PROXY_ENV

    for env in FORWARD_PROXY_ENV:
        # Both spellings: the guard reads the environment case-insensitively.
        monkeypatch.delenv(env, raising=False)
        monkeypatch.delenv(env.lower(), raising=False)
    monkeypatch.setattr(settings, "egress_allow_proxy", False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.internal:3128")

    wire = Wire(internal, {"site.example": [[PUBLIC_A]]})
    with pytest.raises(ToolError, match="EGRESS_ALLOW_PROXY"):
        wire.fetch("http://site.example/page")
    # Refused before anything was looked up or dialed.
    assert wire.lookups == []
    assert wire.dialed == []
