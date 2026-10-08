"""Shared construction for server-side OUTBOUND HTTP clients.

Every outbound call a self-hosted deployment makes — the model gateway's provider /
proxy-upstream traffic and the backend's OAuth/OIDC discovery, token-exchange, JWKS,
and captcha fetches — goes through :func:`async_client` so a locked-down VPC's egress
controls apply uniformly:

- **Corporate / forward proxy**: honored automatically from the standard
  ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY`` environment variables. httpx reads
  them when ``trust_env`` is left at its default ``True`` — which we never disable —
  so no application config is needed. (``ALL_PROXY`` and ``SSL_CERT_FILE`` are honored
  the same way.)
- **Custom / internal CA**: set ``OUTBOUND_CA_BUNDLE`` to a PEM file *path* or to
  inline PEM *text* (the latter so a secrets manager can inject the cert as a value).
  It is added to the system trust store, so public provider CAs still verify while an
  internal MITM/proxy CA is also trusted.
- **Connection pool**: capped at :data:`UPSTREAM_POOL_LIMITS` — a ceiling we declare
  rather than inherit from httpx, because the gateway refuses to boot with a
  backpressure gate above it.
- **TCP keepalive**: every connection is opened with keepalive probes armed, so a peer
  that disappears without closing the socket is detected in minutes rather than at the
  read timeout. The gateway's silence bounds are hours (a model step may legitimately
  think for that long), so without the probes a crashed provider would pin a stream,
  a gate slot and a credit hold for the whole bound.

Keeping this in one place means the egress story is "set two well-known env vars (or
one app setting) and everything obeys it", and a single guard test proves no client
silently bypasses it.
"""

from __future__ import annotations

import socket
import ssl
from typing import Any

import httpx

from alkera_core.ca_bundle import ca_bundle_ssl_context
from alkera_core.config import UPSTREAM_POOL_MAX_CONNECTIONS, settings
from alkera_core.db.locking import io_outside_locks
from alkera_core.egress import (
    EgressPolicy,
    PinnedAsyncTransport,
    Resolver,
    configured_proxy_env,
    proxy_egress_allowed,
)
from alkera_core.logging import get_logger

log = get_logger(__name__)

#: The connection pool every client from :func:`async_client` gets. Stated here
#: rather than left to httpx's own default because the gateway's backpressure gate
#: (``GATEWAY_MAX_CONCURRENT_STREAMS``, refused at boot above
#: ``UPSTREAM_POOL_MAX_CONNECTIONS``) is only meaningful if the ceiling it is
#: validated against is the one actually applied — an httpx release that moved its
#: default would otherwise silently re-open the gap between the shed threshold and
#: real capacity, and saturation would surface as retried pool timeouts instead of
#: a 429. The values are httpx's historical defaults, so this changes no behavior.
UPSTREAM_POOL_LIMITS = httpx.Limits(
    max_connections=UPSTREAM_POOL_MAX_CONNECTIONS, max_keepalive_connections=20
)


#: Idle time before the first TCP keepalive probe, the gap between probes, and how
#: many go unanswered before the kernel declares the connection dead — so a peer that
#: vanishes is reported in about
#: ``KEEPALIVE_IDLE_SECONDS + KEEPALIVE_INTERVAL_SECONDS * KEEPALIVE_PROBE_COUNT``
#: seconds (three minutes), independent of any application read timeout. Deliberately
#: unhurried: probes are what an idle pooled connection costs, and a provider that
#: answers nothing for a minute while it thinks is normal, not dead.
KEEPALIVE_IDLE_SECONDS = 60
KEEPALIVE_INTERVAL_SECONDS = 30
KEEPALIVE_PROBE_COUNT = 4


def keepalive_socket_options() -> list[tuple[int, int, int]]:
    """Socket options arming TCP keepalive on an outbound connection.

    ``SO_KEEPALIVE`` alone enables the probes but leaves the schedule at the
    kernel's default, which on Linux and macOS is two hours of idle before the
    FIRST probe — useless against a bound measured in hours. The three tuning
    options set the schedule; their names differ per platform (Linux spells the
    idle time ``TCP_KEEPIDLE``, macOS ``TCP_KEEPALIVE``) and a platform missing one
    simply keeps its default for that knob rather than failing to connect.
    """
    options: list[tuple[int, int, int]] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    if idle_opt is not None:
        options.append((socket.IPPROTO_TCP, idle_opt, KEEPALIVE_IDLE_SECONDS))
    interval_opt = getattr(socket, "TCP_KEEPINTVL", None)
    if interval_opt is not None:
        options.append((socket.IPPROTO_TCP, interval_opt, KEEPALIVE_INTERVAL_SECONDS))
    count_opt = getattr(socket, "TCP_KEEPCNT", None)
    if count_opt is not None:
        options.append((socket.IPPROTO_TCP, count_opt, KEEPALIVE_PROBE_COUNT))
    return options


class KeepaliveAsyncClient(httpx.AsyncClient):
    """An :class:`httpx.AsyncClient` whose connection pools open sockets with TCP
    keepalive armed — the direct pool AND every proxy pool.

    httpx takes socket options on a transport, not on a client, and handing it a
    ready-made ``transport=`` would switch off env-proxy discovery (it only reads
    ``HTTPS_PROXY`` and friends when it builds the transport itself). So the two
    hooks it builds them in are overridden instead, which keeps the proxy map,
    the CA and the pool limits entirely httpx's business. ``test_http.py`` asserts
    a real client built here carries the options, so an httpx release that renames
    either hook fails loudly rather than silently dropping keepalive.
    """

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        """Send ``request``, never while this task holds a row lock: the wait
        for somebody else's server would be every queued writer's wait too."""
        with io_outside_locks(f"http {request.method} {request.url.host}"):
            return await super().send(request, **kwargs)

    def _init_transport(  # mirrors httpx's own hook signature
        self,
        verify: ssl.SSLContext | str | bool = True,
        cert: Any = None,
        trust_env: bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: httpx.Limits = UPSTREAM_POOL_LIMITS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> httpx.AsyncBaseTransport:
        if transport is not None:
            return transport
        return httpx.AsyncHTTPTransport(
            verify=verify,
            cert=cert,
            trust_env=trust_env,
            http1=http1,
            http2=http2,
            limits=limits,
            socket_options=keepalive_socket_options(),
        )

    def _init_proxy_transport(  # mirrors httpx's own hook signature
        self,
        proxy: httpx.Proxy,
        verify: ssl.SSLContext | str | bool = True,
        cert: Any = None,
        trust_env: bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: httpx.Limits = UPSTREAM_POOL_LIMITS,
    ) -> httpx.AsyncBaseTransport:
        return httpx.AsyncHTTPTransport(
            verify=verify,
            cert=cert,
            trust_env=trust_env,
            http1=http1,
            http2=http2,
            limits=limits,
            proxy=proxy,
            socket_options=keepalive_socket_options(),
        )


class GuardedAsyncClient(KeepaliveAsyncClient):
    """A :class:`KeepaliveAsyncClient` for URLs somebody else supplied (an org
    admin's OIDC issuer, a document that issuer served): every request — each
    redirect hop included — is canonicalised, resolved and vetted by
    :mod:`alkera_core.egress`, and the direct transport dials the vetted address
    rather than resolving the name a second time.

    The guard wraps the transports httpx builds instead of replacing them, so the
    env proxy map, the CA, the pool limits and keepalive stay exactly as
    :class:`KeepaliveAsyncClient` has them. A proxy transport vets without
    pinning, because the proxy — not this process — makes that connection.
    """

    def __init__(
        self,
        *,
        egress_policy: EgressPolicy,
        egress_schemes: tuple[str, ...] = ("http", "https"),
        egress_resolver: Resolver | None = None,
        **kwargs: Any,
    ) -> None:
        # Set before ``super().__init__``: httpx builds its transports inside it.
        self._egress_policy = egress_policy
        self._egress_schemes = egress_schemes
        self._egress_resolver = egress_resolver
        super().__init__(**kwargs)

    def _guard(self, inner: httpx.AsyncBaseTransport, *, pin: bool) -> httpx.AsyncBaseTransport:
        return PinnedAsyncTransport(
            policy=self._egress_policy,
            resolver=self._egress_resolver,
            schemes=self._egress_schemes,
            inner=inner,
            pin=pin,
        )

    def _init_transport(self, *args: Any, **kwargs: Any) -> httpx.AsyncBaseTransport:
        return self._guard(super()._init_transport(*args, **kwargs), pin=True)

    def _init_proxy_transport(self, *args: Any, **kwargs: Any) -> httpx.AsyncBaseTransport:
        return self._guard(super()._init_proxy_transport(*args, **kwargs), pin=False)


def log_egress_status(component: str) -> None:
    """Say at boot whether guarded fetches are pinned, and why not when they
    are not. Called once per process, before the first request, so the answer is
    in the log a deployment already collects instead of only in an incident."""
    names = configured_proxy_env()
    if not names:
        log.info("egress.pinned", component=component)
        return
    log.warning(
        "egress.unpinned_via_proxy" if proxy_egress_allowed() else "egress.proxy_refused",
        component=component,
        proxy_env=list(names),
        allowed=proxy_egress_allowed(),
    )


def outbound_ssl_context() -> ssl.SSLContext | None:
    """Build an :class:`ssl.SSLContext` trusting the configured custom CA *in addition
    to* the system store, or ``None`` when none is configured.

    ``None`` means "use httpx's default verification" — which still honors the
    standard ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` env vars via ``trust_env``.
    """
    return ca_bundle_ssl_context(settings.outbound_ca_bundle)


def async_client(
    *,
    timeout: httpx.Timeout | float | None = None,
    egress_policy: EgressPolicy | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """An :class:`httpx.AsyncClient` with the shared egress policy applied.

    Pass ``egress_policy`` whenever the URL comes from anyone but the operator:
    the client is then a :class:`GuardedAsyncClient` (``egress_schemes`` and
    ``egress_resolver`` pass through to it).

    ``trust_env`` is left at its default ``True`` (so the proxy + ``SSL_CERT_FILE`` env
    vars are honored), the custom CA bundle — if ``OUTBOUND_CA_BUNDLE`` is set — is
    used for verification, the pool is capped at :data:`UPSTREAM_POOL_LIMITS`, and
    connections are opened with TCP keepalive armed. Any extra keyword args pass
    straight through to httpx, and a caller-supplied ``verify`` / ``trust_env`` /
    ``limits`` wins over the defaults here.
    """
    ctx = outbound_ssl_context()
    if ctx is not None:
        kwargs.setdefault("verify", ctx)
    if timeout is not None:
        kwargs.setdefault("timeout", timeout)
    kwargs.setdefault("limits", UPSTREAM_POOL_LIMITS)
    if egress_policy is not None:
        return GuardedAsyncClient(egress_policy=egress_policy, **kwargs)
    return KeepaliveAsyncClient(**kwargs)
