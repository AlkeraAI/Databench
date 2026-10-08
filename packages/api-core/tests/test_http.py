"""Outbound HTTP egress policy: custom-CA threading, proxy-env honoring, pool cap.

These pin the IP-protection contract for a locked-down VPC — a custom CA reaches
every outbound client, and we never disable httpx's `trust_env` (so HTTP(S)_PROXY /
NO_PROXY / SSL_CERT_FILE are always honored) — plus the connection ceiling the
gateway's backpressure gate is validated against at boot.
"""

from __future__ import annotations

import socket
import ssl
from pathlib import Path

import certifi
import httpx
import pytest
from alkera_core import http
from alkera_core.ca_bundle import OutboundTlsConfigError
from alkera_core.config import UPSTREAM_POOL_MAX_CONNECTIONS


@pytest.fixture
def valid_pem(tmp_path: Path) -> Path:
    """A real, parseable CA bundle on disk (certifi's), for the file-path case."""
    p = tmp_path / "ca.pem"
    p.write_text(Path(certifi.where()).read_text())
    return p


def test_no_bundle_means_no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http.settings, "outbound_ca_bundle", None)
    assert http.outbound_ssl_context() is None


def test_ca_bundle_file_path_builds_context(
    monkeypatch: pytest.MonkeyPatch, valid_pem: Path
) -> None:
    monkeypatch.setattr(http.settings, "outbound_ca_bundle", str(valid_pem))
    ctx = http.outbound_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)


def test_ca_bundle_inline_pem_builds_context(monkeypatch: pytest.MonkeyPatch) -> None:
    pem = Path(certifi.where()).read_text()
    assert "BEGIN CERTIFICATE" in pem
    monkeypatch.setattr(http.settings, "outbound_ca_bundle", pem)
    ctx = http.outbound_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)


def test_missing_file_path_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http.settings, "outbound_ca_bundle", "/no/such/ca.pem")
    with pytest.raises(OutboundTlsConfigError):
        http.outbound_ssl_context()


def test_garbage_inline_pem_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Looks like PEM (has the marker) but isn't decodable.
    monkeypatch.setattr(
        http.settings,
        "outbound_ca_bundle",
        "-----BEGIN CERTIFICATE-----\nnot-base64!!!\n-----END CERTIFICATE-----\n",
    )
    with pytest.raises(OutboundTlsConfigError):
        http.outbound_ssl_context()


def test_async_client_threads_ca_when_configured(
    monkeypatch: pytest.MonkeyPatch, valid_pem: Path
) -> None:
    captured: dict[str, object] = {}

    class _Stub:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(http, "KeepaliveAsyncClient", _Stub)

    # No CA → no explicit verify (httpx default + SSL_CERT_FILE via trust_env apply).
    monkeypatch.setattr(http.settings, "outbound_ca_bundle", None)
    http.async_client(timeout=1.0)
    assert "verify" not in captured

    # CA configured → our SSLContext is passed as verify.
    captured.clear()
    monkeypatch.setattr(http.settings, "outbound_ca_bundle", str(valid_pem))
    http.async_client()
    assert isinstance(captured.get("verify"), ssl.SSLContext)


def test_async_client_never_disables_trust_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The guarantee that corporate-proxy / SSL_CERT_FILE env vars keep working: the
    # factory must never pass trust_env (leaving httpx's default True in force).
    captured: dict[str, object] = {}

    class _Stub:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(http, "KeepaliveAsyncClient", _Stub)
    monkeypatch.setattr(http.settings, "outbound_ca_bundle", None)
    http.async_client(timeout=1.0)
    assert "trust_env" not in captured


def test_async_client_declares_the_pool_ceiling_rather_than_inheriting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory must PASS an explicit `limits=`. Left to httpx's own default the
    ceiling is a third party's number, and the gateway's boot check
    (GATEWAY_MAX_CONCURRENT_STREAMS <= UPSTREAM_POOL_MAX_CONNECTIONS) would guard a
    capacity that nothing we own states — an httpx release moving its default would
    silently re-open the gap and saturation would surface as retried pool timeouts
    instead of a 429."""
    captured: dict[str, object] = {}

    class _Stub:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(http, "KeepaliveAsyncClient", _Stub)
    monkeypatch.setattr(http.settings, "outbound_ca_bundle", None)
    http.async_client(timeout=1.0)

    assert captured["limits"] is http.UPSTREAM_POOL_LIMITS
    assert http.UPSTREAM_POOL_LIMITS.max_connections == UPSTREAM_POOL_MAX_CONNECTIONS


def test_a_caller_supplied_pool_limit_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The asymmetric case: the default must stay a `setdefault`, like `verify`. A
    caller that deliberately sizes its own pool (a low-concurrency background poller)
    keeps it — the factory adds a floor of policy, it does not override callers."""
    captured: dict[str, object] = {}

    class _Stub:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(http, "KeepaliveAsyncClient", _Stub)
    monkeypatch.setattr(http.settings, "outbound_ca_bundle", None)
    mine = httpx.Limits(max_connections=3)
    http.async_client(limits=mine)

    assert captured["limits"] is mine


def test_keepalive_options_arm_the_probes_and_set_their_schedule() -> None:
    """SO_KEEPALIVE alone leaves the kernel's two-hour idle default in force, which
    is useless against a silence bound measured in hours — the schedule options are
    the whole point, so the enable flag is not enough on its own."""
    options = http.keepalive_socket_options()

    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in options
    values = {name: value for _level, name, value in options}
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    assert idle_opt is not None, "no idle-time keepalive option on this platform"
    assert values[idle_opt] == http.KEEPALIVE_IDLE_SECONDS
    assert values[socket.TCP_KEEPINTVL] == http.KEEPALIVE_INTERVAL_SECONDS
    assert values[socket.TCP_KEEPCNT] == http.KEEPALIVE_PROBE_COUNT
    # A dead peer must be reported in minutes, not inside the hours-long read bound.
    detection = http.KEEPALIVE_IDLE_SECONDS + (
        http.KEEPALIVE_INTERVAL_SECONDS * http.KEEPALIVE_PROBE_COUNT
    )
    assert 60 <= detection <= 600


async def test_the_built_client_really_opens_connections_with_keepalive() -> None:
    """End-to-end on a REAL client: the options reach the connection pool that
    actually dials. httpx takes them on a transport and offers no client-level
    argument, so this is the only proof the override still lands."""
    client = http.async_client()
    try:
        pool_options = client._transport._pool._socket_options
    finally:
        await client.aclose()

    assert pool_options is not None, "the pool dials with no socket options at all"
    assert list(pool_options) == http.keepalive_socket_options()


async def test_a_proxied_connection_gets_keepalive_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The asymmetric case: a locked-down VPC reaches providers THROUGH a forward
    proxy, so the proxy pool is the one that holds the long silent stream. Arming
    only the direct pool would leave exactly those deployments undefended."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    client = http.async_client()
    try:
        mounts = [t for t in client._mounts.values() if t is not None]
        assert mounts, "no proxy mount was built from HTTPS_PROXY"
        for transport in mounts:
            options = transport._pool._socket_options
            assert options is not None, "the proxy pool dials with no socket options"
            assert list(options) == http.keepalive_socket_options()
    finally:
        await client.aclose()


def test_a_caller_supplied_transport_is_left_alone() -> None:
    """A test installing its own MockTransport must keep it — the keepalive hook
    only applies to pools the factory itself opens."""
    mock = httpx.MockTransport(lambda _request: httpx.Response(204))

    client = http.KeepaliveAsyncClient(transport=mock)

    assert client._transport is mock


async def test_the_built_client_really_enforces_the_declared_ceiling() -> None:
    """End-to-end on a REAL client: the ceiling we declare is the one the transport's
    connection pool ends up with. httpx exposes no public accessor for it, hence the
    private hop — the value itself is now ours, so this only fails if the factory
    stopped applying it (or httpx stopped honoring `limits=`), both of which must be
    loud."""
    client = http.async_client()
    try:
        assert client._transport._pool._max_connections == UPSTREAM_POOL_MAX_CONNECTIONS
    finally:
        await client.aclose()
