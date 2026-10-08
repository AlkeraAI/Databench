"""Response security headers for the HTTP APIs.

A hosted SPA usually gets these from its CDN. The API hosts often sit behind a
plain load balancer that adds none of them, so the API sets them itself.

Pure ASGI, not ``BaseHTTPMiddleware``, for the same reason as
``RequestContextMiddleware``: the gateway streams SSE and BaseHTTPMiddleware
interferes with streaming responses. This only rewrites the header block on
``http.response.start`` and never touches the body.

Two response shapes, because one policy cannot serve both:

* **JSON/other** — the whole API. Locked down to nothing: a JSON endpoint loads
  no scripts, embeds no frames, and submits no forms, so ``default-src 'none'``
  costs nothing and makes any future reflected-HTML bug inert.
* **HTML** — only ``/docs`` and ``/redoc`` (local dev) and the mock OAuth consent
  screen (dev). They legitimately load scripts and styles, so they get a
  relaxed policy rather than a broken page.

``Cache-Control: no-store`` is applied where a route did not already choose one.
API responses are per-session by construction and must not be reused by a shared
proxy or left in the disk cache of a shared machine.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from alkera_core.config import settings

#: Where the Files content app is mounted. User bytes are served from their own
#: registrable domain under their own, stricter policy, so this
#: middleware must neither stamp the API recipe over that mount nor let it be
#: reached from the API's own hostname.
CONTENT_MOUNT_PREFIX: Final = "/c"

#: Two years, matching the SPA's CloudFront policy. Not `preload` — committing a
#: domain to the browser preload list is a separate, hard-to-reverse decision.
DEFAULT_HSTS_MAX_AGE = 63_072_000

#: A JSON API is allowed to do nothing at all in a browser context. `frame-ancestors`
#: is the modern X-Frame-Options; both are sent, since some scanners and older
#: browsers only read the header.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

#: Swagger UI / ReDoc / the dev OAuth consent screen need their own scripts,
#: styles, and inline bootstrap. Still no framing and no arbitrary form target.
HTML_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)

#: Deny every powerful browser feature. An API needs none of them, and a blanket
#: denial is what a scanner looks for.
PERMISSIONS_POLICY = (
    "accelerometer=(), autoplay=(), camera=(), display-capture=(), encrypted-media=(), "
    "fullscreen=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), "
    "midi=(), payment=(), picture-in-picture=(), usb=(), xr-spatial-tracking=()"
)


class SecurityHeadersMiddleware:
    """Stamp the standard security headers onto every response.

    ``hsts`` is off in local dev: the dev server is plain HTTP, and a browser
    that has once seen HSTS for ``localhost`` will refuse http on *every*
    localhost port afterwards — a genuinely painful thing to do to a developer's
    machine, and it is not the browser's to undo per-port.

    ``cross_origin_resource_policy`` defaults to ``same-site`` because the SPA
    (``app.example.com``) and the API (``api.example.com``) share a registrable
    domain. A self-hosted deployment that splits them across unrelated domains
    sets ``cross-origin`` instead. CORP governs no-cors subresource loads only,
    so neither value affects the SPA's CORS ``fetch`` traffic.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        hsts: bool = True,
        hsts_max_age: int = DEFAULT_HSTS_MAX_AGE,
        cross_origin_resource_policy: str = "same-site",
        content_mount_prefix: str | None = CONTENT_MOUNT_PREFIX,
    ) -> None:
        self.app = app
        self.hsts = hsts
        self.hsts_max_age = hsts_max_age
        self.corp = cross_origin_resource_policy
        self.content_mount_prefix = content_mount_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                self._apply(MutableHeaders(scope=message))
            await send(message)

        if self.content_mount_prefix is not None:
            on_content_path = _is_content_path(scope.get("path", ""), self.content_mount_prefix)
            on_content_host = _on_content_host(scope)
            if on_content_path != on_content_host:
                # The path and the host must travel together. A content path
                # reached under the API host would serve user bytes from the
                # origin that carries the session cookie; an API path reached
                # under the content host would put the API surface on an origin
                # whose whole design is that nothing served there is trusted.
                # Either way the answer is the opaque 404, so a probe learns
                # nothing about which half exists. It carries the API's
                # recipe: a refusal is a response a browser can be pointed at
                # like any other.
                await _refuse(send_wrapper)
                return
            if on_content_path:
                # The mount stamps its own, stricter recipe. Stamping the API's
                # over it would relax the sandbox CSP that is the control making
                # an unscanned object safe to serve.
                await self.app(scope, receive, send)
                return

        await self.app(scope, receive, send_wrapper)

    def _apply(self, headers: MutableHeaders) -> None:
        is_html = headers.get("content-type", "").lower().startswith("text/html")
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        headers["Permissions-Policy"] = PERMISSIONS_POLICY
        headers["Content-Security-Policy"] = HTML_CSP if is_html else API_CSP
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        headers["Cross-Origin-Resource-Policy"] = self.corp
        if self.hsts:
            headers["Strict-Transport-Security"] = f"max-age={self.hsts_max_age}; includeSubDomains"
        # Only when the route did not choose its own policy: the audit-log CSV
        # export and any future cacheable asset set one deliberately, and a
        # blanket overwrite here would silently undo that.
        if "cache-control" not in headers:
            headers["Cache-Control"] = "no-store"
            # Pragma is HTTP/1.0 and redundant next to Cache-Control for any
            # modern client, but it is what compliance scanners look for on a
            # sensitive response, and it costs 22 bytes.
            headers["Pragma"] = "no-cache"


def _is_content_path(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def _on_content_host(scope: Scope) -> bool:
    """Whether this request arrived under the configured content origin.

    An unconfigured content domain matches nothing, so a deployment that has not
    set one refuses every content path rather than serving it from the API.
    """
    expected = settings.files_content_host
    if expected is None:
        return False
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", ())
    for key, value in headers:
        if key == b"host":
            return value.decode("latin-1").lower() == expected
    return False


async def _refuse(send: Send) -> None:
    async def receive() -> Message:  # pragma: no cover - a refusal reads no body
        return {"type": "http.disconnect"}

    await JSONResponse({"code": "not_found"}, status_code=404)({"type": "http"}, receive, send)


__all__ = [
    "API_CSP",
    "CONTENT_MOUNT_PREFIX",
    "DEFAULT_HSTS_MAX_AGE",
    "HTML_CSP",
    "PERMISSIONS_POLICY",
    "SecurityHeadersMiddleware",
]
