"""Origin guard for cookie-authenticated state changes.

A session cookie is an AMBIENT credential: the browser attaches it to every
request that reaches this API, including one that a page the user never chose
to trust caused. ``SameSite=Lax`` withholds it from a cross-*site* form POST,
but a "site" is the registrable domain — another port on the same host, a
sibling subdomain, a staging hostname, or the content origin of a single-domain
self-hosted install are all SAME-site and still cross-*origin*, and the cookie
travels to every one of them. Without this guard the only thing standing
between a page on such an origin and ``POST /auth/logout-all`` (or any other
mutation) is that nobody has tried.

So: a non-safe request that carries a session cookie must also present browser
evidence that it came from an origin this deployment serves the portal from.
The evidence a browser always supplies on a non-safe request is ``Origin``,
and an origin on the deployment's allowlist is admitted on it. Anything else
that ``Sec-Fetch-Site`` marks ``same-site`` or ``cross-site`` is refused, and
``Referer`` is the fallback only for the handful of navigations that omit
``Origin`` without saying they came from elsewhere.

Credentials that are NOT ambient — ``Authorization: Bearer`` (the CLI, the
daemon, the VS Code webview), CI tokens, proxy tokens, personal access tokens,
machine credentials — are never checked here: a page cannot make a browser
attach one, so there is no request to forge.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Final
from urllib.parse import urlsplit

from alkera_core.config import settings
from alkera_core.logging import get_logger
from alkera_core.observability.context import get_trace_id
from alkera_core.observability.envelope import build_error_body
from alkera_core.observability.errors import ErrorCode
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.api.route_path import routed_path
from backend.content_app import CONTENT_MOUNT_PATH

log = get_logger(__name__)

#: Methods that by contract change nothing, so a forged one buys an attacker
#: nothing it could not have read from its own origin anyway. ``OPTIONS`` is
#: here so a CORS preflight reaches the CORS middleware unchanged.
SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Route prefixes whose non-safe requests are legitimately cross-origin, and so
#: cannot be bound to a portal origin. Every one of them authenticates by
#: something that is NOT the session cookie — a provider signature, a bearer
#: token, a signed ticket — so exempting them gives up nothing: an attacker who
#: could forge the request could equally send it from their own server.
#:
#: This is an allowlist of PREFIXES, deliberately spelled out one machine
#: surface at a time rather than by router or by method: ``/api/v1/gate`` and
#: ``/api/v1/auth/device`` each mix machine routes with org-admin cookie routes
#: on the same prefix, and a coarser entry would hand the cookie routes away.
EXEMPT_PREFIXES: Final[tuple[str, ...]] = (
    # Stripe + GitHub App webhooks: posted by the provider, trusted by signature.
    "/api/v1/webhooks",
    # Slack Events + Interactivity: same trust model. `/api/v1/slack/link` is a
    # cookie surface and is deliberately NOT exempt.
    "/api/v1/slack/events",
    "/api/v1/slack/interactivity",
    # SCIM: the customer's IdP, bearer-authed.
    "/api/v1/scim/v2",
    # The device grant's machine half — the CLI asks for a code and polls for a
    # token with no browser and no session. `/approve` and `/deny` are the SPA's
    # cookie-authed consent routes and stay guarded.
    "/api/v1/auth/device/code",
    "/api/v1/auth/device/token",
    # SAML: the IdP POSTs its assertion to the ACS endpoint from its own origin.
    # The whole router is public (login / callback / discover / acs) — the org's
    # SSO administration lives under `/api/v1/org/sso`, which is not exempt.
    "/api/v1/auth/sso",
    # The CI gate's ingest + lookup + lease surface, authenticated by a CI token.
    # `/api/v1/gate/tokens`, `/api/v1/gate/waivers` (minus the machine lookup)
    # and the portal routes are org-admin cookie surfaces and stay guarded.
    "/api/v1/gate/snapshots",
    "/api/v1/gate/reports",
    "/api/v1/gate/receipts",
    "/api/v1/gate/leases",
    "/api/v1/gate/sandboxes",
    "/api/v1/gate/waivers/lookup",
    # User bytes: a second app on a hostname a session cookie must never reach.
    CONTENT_MOUNT_PATH,
)


def normalize_origin(value: str) -> str | None:
    """``scheme://host[:port]`` lowercased with a default port dropped, or None
    when the value is not an origin a browser could have sent.

    ``null`` — what a sandboxed iframe, a ``data:`` document or a redirected
    cross-origin request sends — has no host and so normalizes to None, which
    the guard refuses."""
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    try:
        host, port = parsed.hostname, parsed.port
    except ValueError:  # a malformed port in the authority
        return None
    if not host:
        return None
    default = 443 if parsed.scheme == "https" else 80
    if port is None or port == default:
        return f"{parsed.scheme}://{host.lower()}"
    return f"{parsed.scheme}://{host.lower()}:{port}"


def trusted_origins() -> frozenset[str]:
    """Every origin allowed to carry a cookie-authenticated state change.

    Read from settings on each call — a deployment is configured, not compiled,
    and the suite overrides these per test. The portal's own origin comes from
    ``frontend_base_url``; ``api_cors_origins`` is included because an origin the
    deployment already lets read cross-origin with credentials can forge nothing
    new by also writing; ``auth_csrf_trusted_origins`` is the seam for a layout
    neither of those names."""
    raw = [
        settings.frontend_base_url,
        # The API's own origin: a page it serves (the Slack link page) posts
        # back to itself, which is same-origin by definition.
        settings.api_public_base_url,
        *settings.cors_origins_list,
        *settings.csrf_trusted_origins_list,
    ]
    return frozenset(o for o in (normalize_origin(v) for v in raw) if o is not None)


def is_exempt(path: str) -> bool:
    """Whether ``path`` is one of the machine surfaces the guard does not bind.

    Matched on a segment boundary so a future ``/api/v1/webhooks-admin`` cannot
    inherit ``/api/v1/webhooks``'s exemption by sharing its spelling."""
    return any(path == p or path.startswith(f"{p}/") for p in EXEMPT_PREFIXES)


def carries_session_cookie(cookie_header: str) -> bool:
    """Whether the request presents one of the ambient browser credentials.

    Both cookies count: the refresh cookie alone authenticates
    ``POST /auth/refresh``, which mints a new session — forging it is the same
    win as forging anything the access cookie carries."""
    names = {settings.auth_cookie_name, settings.auth_refresh_cookie_name}
    return any(part.partition("=")[0].strip() in names for part in cookie_header.split(";"))


def _header(scope: Scope, name: bytes) -> str | None:
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", ())
    for key, value in headers:
        if key == name:
            return value.decode("latin-1")
    return None


def _refusal_reason(scope: Scope) -> str | None:
    """Why this request must be refused, or None when it may proceed.

    The returned string is for the server's log only — the client is told the
    same thing in every case, because which of these tripped is itself a hint
    about how the deployment is wired."""
    if scope["method"].upper() in SAFE_METHODS:
        return None
    cookies = _header(scope, b"cookie")
    if not cookies or not carries_session_cookie(cookies):
        return None
    if is_exempt(routed_path(scope)):
        return None

    allowed = trusted_origins()
    fetch_site = _header(scope, b"sec-fetch-site")
    stated = _header(scope, b"origin")
    if stated is not None:
        # The allowlist is consulted first. A browser sets `Origin` itself and
        # a page cannot forge it, and every browser marks a request from a
        # different origin `same-site` or `cross-site` — so a portal served
        # from an origin other than the API's (a self-hosted install with the
        # SPA and the API on two hostnames) is admitted only by name, here.
        # Exact origins only: `null` and anything that is not an origin
        # normalize to None and are never on the list.
        origin = normalize_origin(stated)
        if origin is not None and origin in allowed:
            return None
        if fetch_site in {"cross-site", "same-site"}:
            return f"sec_fetch_site:{fetch_site}"
        return "origin_not_trusted"
    # No `Origin`. The browser's own statement that the initiator was another
    # origin settles it: the weaker evidence below is not asked to overrule it.
    if fetch_site in {"cross-site", "same-site"}:
        return f"sec_fetch_site:{fetch_site}"
    # A few navigations (a form POST following a 302, historically) omit
    # `Origin` but still carry `Referer`; its origin answers the same question.
    referer = _header(scope, b"referer")
    if referer is None:
        # Every browser in support sends `Origin` on a non-safe request, so a
        # cookie-authed one without it is not a browser doing what a browser
        # does. Refusing is the fail-closed reading, and it leaves a
        # non-browser client one honest fix: send the origin it means.
        return "no_origin"
    origin = normalize_origin(referer)
    if origin is None or origin not in allowed:
        return "origin_not_trusted"
    return None


class CsrfOriginMiddleware:
    """Refuse a non-safe, cookie-authenticated request that cannot show it came
    from an origin this deployment serves the portal from."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        reason = _refusal_reason(scope)
        if reason is None:
            await self.app(scope, receive, send)
            return
        log.warning(
            "csrf.refused",
            reason=reason,
            method=scope["method"],
            path=routed_path(scope),
            origin=_header(scope, b"origin"),
        )
        body = json.dumps(
            build_error_body(
                code=ErrorCode.csrf_origin_mismatch,
                message="This request did not come from an allowed origin.",
                trace_id=get_trace_id() or "",
                status=403,
            )
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
