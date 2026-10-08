"""The content domain: a second ASGI app that serves user bytes and nothing else.

User content may never be served from an origin that holds a session cookie, so
it is not a router on the API app. It is its own application, mounted at ``/c``
and reachable only under the hostname in ``files_content_base_url``. The two are
kept from bleeding into each other from both sides:

* :class:`HostGate` here answers the opaque 404 for anything arriving under a
  Host that is not the content origin, so a request the edge misroutes onto the
  API host never reaches a byte;
* the API's ``SecurityHeadersMiddleware`` refuses the mirror case — an API path
  under the content Host, and ``/c/*`` under the API Host — and skips stamping
  its own CSP on this mount, whose policy is stricter and differently shaped.

Every response leaves here with the full A5 recipe, including the refusals: a
404 with a relaxed CSP would be a hole exactly where a probe looks. The recipe
is stamped in one place rather than per route, and the headers are asserted on
every response shape in ``test_files_content_headers.py``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from typing import Final
from urllib.parse import parse_qsl, quote, urlencode

import structlog
from alkera_core.config import settings
from alkera_core.files.content import disposition_for
from fastapi import APIRouter, Depends, FastAPI
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.api.deps.files_errors import register_content_error_handlers
from backend.api.route_path import routed_path
from backend.api.routes.files import discovered_content_routers, require_files_enabled

log = structlog.get_logger(__name__)

#: Where the content app is mounted on the API app. The path is part of the
#: contract the edge routes on, so it is spelled once and imported.
CONTENT_MOUNT_PATH: Final = "/c"

#: Where a page grant is redeemed, relative to the mount. Under it a response
#: is one file OF a folder the caller was granted rather than a file on its
#: own, and the two differ in exactly two headers: what it may load beside
#: itself, and who may frame it.
PAGE_PATH_PREFIX: Final = "/p/"

#: The A5 recipe, minus the CORS headers (which depend on the request's Origin)
#: and ``Content-Disposition`` (which depends on the node). ``sandbox`` with no
#: allowances drops the response into an opaque origin with no scripts, no
#: forms and no plugins: it is the control that makes serving an unscanned
#: object safe, so it ships enabled and is never staged as Report-Only.
CONTENT_HEADERS: Final[dict[str, str]] = {
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-store",
}

#: The default policy, as a value the per-type table is compared against.
DEFAULT_CSP: Final = CONTENT_HEADERS["Content-Security-Policy"]

# A PDF cannot render under ``sandbox``. A sandboxed document has the
# "sandboxed plugins" flag set unconditionally, and the browser's PDF viewer is
# exactly that — so a PDF served with the default recipe is a blank frame, and
# "open the report" stops working for the one artefact format that exists to be
# opened.
#
# The exchange is narrow and it is worth stating plainly. ``sandbox`` is there
# so that a browsing context over UNSCANNED bytes cannot run script, submit a
# form, or reach this origin's storage. A PDF has no DOM: it cannot read a
# cookie, cannot script this origin, and cannot navigate it. What it can do is
# exercise the viewer, which is the browser's own attack surface and is not
# reduced by any header we could send. So ``sandbox`` is dropped for exactly
# this type and every other restriction is kept — nothing may be fetched,
# framed, submitted, or used as a base.
#
# This is keyed on the type the SERVER sniffed (it is the response's own
# ``Content-Type``, set from ``mime_sniffed``, never from a client's claim),
# and ``nosniff`` rides alongside, so an HTML document cannot arrive here by
# being called a PDF.
#
# ``text/html`` is the other artefact format the product asks an agent for, and
# it takes the opposite trade: it KEEPS ``sandbox``, because a page does have a
# DOM and must never get one on this origin. The sandbox allows scripts and
# nothing else, so the page sits in an opaque origin with no forms, no popups
# and no top-level navigation, and no network (see ``_html_csp``): a script can
# render a chart but there is nothing for stored XSS to read or send. What it
# buys back is rendering: ``default-src`` stays ``'none'`` — no URL on this host
# or any other may be fetched — and the only additions are the
# inline CSS and ``data:`` assets that a self-contained page carries inside
# itself. A page needing more than that could not have rendered here anyway.
#
# Two things separate a page redeemed through a grant from a single file. It
# may load what sits beside it — the grant covers a folder, and a page written
# to be read names its images, fonts and clips by relative path, which the
# browser fetches from this same origin — so ``'self'`` joins the asset
# directives, and only those: never ``script-src``, never ``style-src``, so
# nothing new becomes executable. And it must be framable, because the whole
# point of the preview is a panel beside the chat; ``frame-ancestors`` names
# the app's own origins rather than ``'none'``, which is the one directive
# here that describes who may embed the response instead of what it may fetch.


def _html_csp(page: bool) -> str:
    """An HTML report runs its own scripts, in a box that can reach nothing.

    ``sandbox allow-scripts`` WITHOUT ``allow-same-origin`` puts the document in
    an opaque origin: it has no cookies, no storage and no DOM access to this
    origin or the app, and the sandbox still withholds forms, popups, modals and
    top-level navigation. ``default-src 'none'`` leaves it no network at all --
    no ``connect-src``, no ``worker-src``, no frames -- so a script can draw a
    chart from the data in the page but cannot send anything anywhere. Scripts
    are the page's own: inline, or (under a grant) the files beside it by
    relative path; never another host, never ``eval``.
    """
    assets = "'self' data:" if page else "data:"
    scripts = "'self' 'unsafe-inline'" if page else "'unsafe-inline'"
    return (
        f"default-src 'none'; script-src {scripts}; object-src 'none'; "
        f"style-src 'unsafe-inline'; img-src {assets}; font-src {assets}; "
        f"media-src {assets}; frame-ancestors {frame_ancestors()}; "
        "base-uri 'none'; form-action 'none'; sandbox allow-scripts"
    )


def _pdf_csp(page: bool) -> str:
    return (
        "default-src 'none'; object-src 'none'; script-src 'none'; style-src 'none'; "
        f"frame-ancestors {frame_ancestors()}; base-uri 'none'; form-action 'none'"
    )


def _svg_csp(page: bool) -> str:
    """An SVG is a document with a DOM, so it keeps the sandbox a page keeps.

    It gains no ``'self'``: an SVG the product renders is one image, never a
    folder of them, and a relative reference out of one is a fetch nobody
    asked for.
    """
    return (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
        f"frame-ancestors {frame_ancestors()}; base-uri 'none'; form-action 'none'; sandbox"
    )


def _media_csp(page: bool) -> str:
    """A clip is its own response: it fetches nothing, so only framing matters."""
    return f"default-src 'none'; frame-ancestors {frame_ancestors()}; sandbox"


CSP_BY_SNIFFED_TYPE: Final[dict[str, Callable[[bool], str]]] = {
    "application/pdf": _pdf_csp,
    "text/html": _html_csp,
    "image/svg+xml": _svg_csp,
    "video/mp4": _media_csp,
    "video/webm": _media_csp,
    "audio/mpeg": _media_csp,
    "audio/wav": _media_csp,
}


def frame_ancestors() -> str:
    """The ``frame-ancestors`` source list, as the app's own origins.

    A preview is rendered in a frame on the app origin, so the documents that
    may embed a content response are exactly the ones the deployment already
    named as its front ends — no wildcard (which is every page on the web) and
    no bare scheme. ``vscode-webview:`` is granted by SCHEME for READING bytes,
    because the webview's origin carries a per-window uuid nobody can pin, and
    that reasoning does not carry over: as a framing source it would admit
    every webview of every extension the user has installed.

    An empty list closes the door rather than opening it: a ``frame-ancestors``
    with no sources is a directive a browser cannot parse, so the fallback is
    the literal ``'none'``.
    """
    sources = [origin for origin in allowed_origins() if _framable(origin)]
    return " ".join(sources) if sources else "'none'"


def _framable(origin: str) -> bool:
    """Whether a configured origin may be named as a framing source.

    The CORS list is a deployment-time string. A wildcard in it is a plausible
    thing for an operator to write and a reasonable thing for CORS to honour;
    it may never become the list of documents allowed to frame a user's bytes.
    Whitespace and ``;`` would end the directive — or the whole policy — early.
    """
    return (
        bool(origin)
        and "*" not in origin
        and ";" not in origin
        and not any(map(str.isspace, origin))
    )


def csp_for(content_type: str, *, page: bool) -> str:
    """The ``Content-Security-Policy`` a response of this type is served under.

    Allowlisted and defaulting to the strict recipe, the same way the
    disposition table is: a type nobody has reasoned about gets ``sandbox`` and
    nothing else, on either path — a grant's path relaxes what a REASONED type
    may load, never what an unreasoned one may. Parameters are stripped
    (``application/pdf; charset=binary``) so a media type cannot dodge — or
    reach — a policy by carrying one.
    """
    sniffed = content_type.split(";", 1)[0].strip().lower()
    build = CSP_BY_SNIFFED_TYPE.get(sniffed)
    return build(page) if build is not None else DEFAULT_CSP


def corp_for(*, page: bool) -> str:
    """The ``Cross-Origin-Resource-Policy`` for a response on this path.

    ``same-origin`` is what stops a third-party page pulling a victim's object
    into its own document, and it is the answer for every read the product
    makes cross-origin through CORS, which this header does not govern. A
    grant's page is the exception: it is loaded by NAVIGATING a frame on the
    app origin, which ``same-origin`` refuses outright, so the page path — and
    only the page path — answers ``cross-origin``. What keeps that narrow is
    that redeeming the page route already required the grant.
    """
    return "cross-origin" if page else "same-origin"


def is_page_path(path: str) -> bool:
    """Whether a mount-relative path is a page-grant redemption.

    A prefix test, never a substring one: ``/probe/p/x`` contains ``p/`` and is
    an ordinary content path, and handing it a page's relaxations would be the
    whole exception leaking.
    """
    return path.startswith(PAGE_PATH_PREFIX)


def page_path_of(scope: Scope) -> bool:
    """:func:`is_page_path` of the path this app itself routes on.

    Starlette leaves ``path`` as the whole request target and records the
    prefix a mount consumed in ``root_path``; the difference is what the router
    inside this app matches, and it is the only spelling the decision may be
    made from — ``/c`` is where the API happens to mount the domain today, and
    a request that does not carry it is not a page.
    """
    return is_page_path(routed_path(scope))


#: What a browser may read off a content response from script. Without these a
#: cross-origin ``fetch`` sees an opaque header block and cannot resume a range
#: download or revalidate an etag.
EXPOSE_HEADERS: Final = "Content-Range, ETag, Content-Disposition"

#: The methods the content domain answers. It is read-only by construction:
#: bytes are written through the API host, which is where the session lives.
ALLOW_METHODS: Final = "GET, HEAD, OPTIONS"

#: Request headers a browser may send cross-origin for a ranged, revalidating
#: read.
ALLOW_HEADERS: Final = "Range, If-Range, If-None-Match, If-Modified-Since"

#: The VS Code webview's origin is ``vscode-webview://<random uuid>`` — a new
#: uuid per window, so it can never be pinned as an exact string. The scheme is
#: the grantable unit, and it is a scheme no page on the open web can claim.
ALLOWED_ORIGIN_SCHEMES: Final[tuple[str, ...]] = ("vscode-webview:",)

#: Query parameters that carry a live credential. A content URL is a bearer
#: token in a query string, so the whole point of the log line is to say which
#: object was read without saying how to read it again.
SIGNED_QUERY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "token",
        "sig",
        "signature",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "se",
        "st",
    }
)

#: A path segment longer than this is treated as credential-bearing. A content
#: URL puts its whole token in the path (``/c/<nonce>.<claim>.<sig>``), and no
#: fixed segment this mount routes on comes close to it.
MAX_LOGGED_SEGMENT: Final = 16

#: How much of a credential-bearing segment survives, so an operator can still
#: correlate two lines about the same download without being able to replay it.
LOGGED_SEGMENT_PREFIX: Final = 8

#: What a stripped value is replaced with, so the log still shows that a token
#: was present (and which parameter carried it) without carrying the secret.
REDACTED: Final = "REDACTED"

#: The opaque body every refusal on this mount carries. Identical to the API's
#: ``not_found``, so a probe cannot tell the two apps apart by their 404s.
NOT_FOUND_BODY: Final[dict[str, str]] = {"code": "not_found"}

#: Longest filename we will put on the wire. Node names are capped at 255 bytes,
#: but a header that long is a liability in proxies with small header buffers,
#: and the name is a courtesy — the caller already knows what it asked for.
MAX_FILENAME_CHARS: Final = 200

#: What an empty or fully-stripped name becomes. Never the node id: a filename
#: is written to the user's disk and an id there is worse than a generic word.
FALLBACK_FILENAME: Final = "download"

#: Bidi overrides let a name render as ``photo.exe`` while ending in ``.jpg``
#: (or the reverse). They carry no meaning in a filename, so they are removed
#: rather than escaped.
_BIDI_CHARS: Final[frozenset[str]] = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏")


def allowed_origins() -> tuple[str, ...]:
    """The exact origins a content response may name in ``Allow-Origin``.

    The SPA's origin plus whatever else the deployment put in the API's CORS
    list; the extension is matched by scheme instead (its origin is per-window).
    """
    origins = [origin for origin in settings.cors_origins_list if origin]
    frontend = settings.frontend_base_url
    if frontend and frontend.rstrip("/") not in origins:
        origins.append(frontend.rstrip("/"))
    return tuple(origins)


def origin_allowed(origin: str) -> bool:
    """Whether ``origin`` may read a content response cross-origin."""
    if not origin:
        return False
    if origin.rstrip("/") in allowed_origins():
        return True
    return any(origin.startswith(scheme) for scheme in ALLOWED_ORIGIN_SCHEMES)


def scrub_content_query(query_string: str) -> str:
    """``query_string`` with every credential-bearing value replaced.

    Order and parameter names survive so a log line still shows the shape of
    the request; the values that would let a reader replay it do not.
    """
    if not query_string:
        return ""
    pairs = parse_qsl(query_string, keep_blank_values=True)
    return urlencode(
        [(key, REDACTED if key.lower() in SIGNED_QUERY_KEYS else value) for key, value in pairs]
    )


def scrub_content_path(path: str) -> str:
    """``path`` with every credential-bearing segment cut down to a prefix.

    The query-string scrubber alone is not enough here: on this mount the token
    *is* the path, so a line logging ``scope["path"]`` verbatim writes a live
    bearer credential into the operator's log — and a signed URL that reaches a
    log is a signed URL anyone with the log can redeem. The head of the segment
    stays because two lines about one download still have to be correlatable.
    """
    parts = path.split("/")
    return "/".join(
        f"{part[:LOGGED_SEGMENT_PREFIX]}\u2026" if len(part) > MAX_LOGGED_SEGMENT else part
        for part in parts
    )


def _sanitise(name: bytes) -> str:
    """A node's name reduced to something safe to put in a header."""
    decoded = name.decode("utf-8", errors="replace")
    kept = [
        char
        for char in decoded
        if char not in _BIDI_CHARS and unicodedata.category(char) != "Cc" and char != "\x7f"
    ]
    cleaned = "".join(kept).strip()
    return cleaned[:MAX_FILENAME_CHARS] or FALLBACK_FILENAME


def _ascii_fallback(cleaned: str) -> str:
    """The ``filename=`` token for a client that never learned RFC 5987.

    Everything outside printable ASCII — and the two characters that would end
    the quoted string early — becomes ``_``, so the parameter is unambiguous
    whatever the reader does with it.
    """
    out = [char if " " <= char <= "~" and char not in '"\\' else "_" for char in cleaned]
    return "".join(out).strip() or FALLBACK_FILENAME


def disposition_header(name: bytes, mime_sniffed: str) -> str:
    """The ``Content-Disposition`` for a node called ``name`` sniffed as ``mime_sniffed``.

    RFC 6266 shape: a quoted ASCII ``filename`` every client understands, plus
    the ``filename*`` extended form carrying the real UTF-8 name. ``inline`` is
    only ever chosen from the sniffed-type allowlist, so a type the sniffer
    learns before the allowlist does is still downloaded rather than rendered.
    """
    cleaned = _sanitise(name)
    ascii_name = _ascii_fallback(cleaned)
    encoded = quote(cleaned, safe="")
    disposition = disposition_for(mime_sniffed)
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def _header(scope: Scope, name: bytes) -> str:
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", ())
    for key, value in headers:
        if key == name:
            return value.decode("latin-1")
    return ""


async def _not_found(send: Send) -> None:
    await JSONResponse(NOT_FOUND_BODY, status_code=404)({"type": "http"}, _no_receive, send)


async def _no_receive() -> Message:  # pragma: no cover - a 404 body is never read
    return {"type": "http.disconnect"}


async def opaque_not_found(request: Request, exc: Exception) -> Response:
    """Answer a routing 404 with the body every deliberate refusal here uses.

    The framework's own 404 is ``{"detail": "Not Found"}``, which is a *second*
    shape on a domain whose whole refusal contract is one shape: a caller could
    sort "this address exists and refused my token" from "this address does not
    exist" without ever holding a valid token. That is an existence oracle,
    and Files never answers whether something exists to a caller who cannot
    read it.

    A 405 is the same oracle wearing a different number: "method not allowed"
    only ever means "this address routes", which is exactly what a prober with
    no token wants to learn, and the framework renders it with the ``detail``
    body besides. Every refusal the mount itself raises therefore collapses to
    the one answer. The deliberate non-404 statuses this domain does return
    (416 for a range outside the grant, and the rest of the Files catalogue)
    are ``FilesError``s with their own handlers and never arrive here.
    """
    assert isinstance(exc, StarletteHTTPException)
    return JSONResponse(NOT_FOUND_BODY, status_code=404)


class HostGate:
    """404 anything that did not arrive under the content origin.

    The mount lives on the API app, so without this a request that reaches the
    API host with a ``/c/`` path would be served user bytes from an origin that
    carries the session cookie — the one thing the separate domain exists to
    prevent. An unconfigured content origin means same-origin content instead:
    the gate opens for any Host, which is the shape local dev and a
    single-origin self-hosted install take. Production cannot get there — the
    settings validator names a missing content origin as a boot error.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        expected = settings.files_content_host
        # No content origin configured means same-origin content: the app serves
        # /c on its own host. Production cannot reach this branch — the settings
        # validator refuses to boot APP_ENV=production without the separate
        # origin — so the gate only ever opens for local dev and a self-hosted
        # install that chose one origin.
        if expected is not None and _header(scope, b"host").lower() != expected:
            await _not_found(send)
            return
        await self.app(scope, receive, send)


class ContentHeaders:
    """Stamp the A5 recipe (and the CORS block) onto every content response.

    Pure ASGI rather than ``BaseHTTPMiddleware`` for the same reason the API's
    header middleware is: content responses stream, and BaseHTTPMiddleware sits
    in the middle of the body. This only rewrites the header block.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = _header(scope, b"origin")
        # From the path this app routes on, not the one the host received: the
        # decision has to mean the same thing wherever the domain is mounted.
        page = page_path_of(scope)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                self._apply(MutableHeaders(scope=message), origin, page=page)
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _apply(self, headers: MutableHeaders, origin: str, *, page: bool) -> None:
        for key, value in CONTENT_HEADERS.items():
            headers[key] = value
        # Read off the response the route is about to send, so the policy is
        # keyed on what is actually being served rather than on a route
        # remembering to say so.
        headers["Content-Security-Policy"] = csp_for(headers.get("Content-Type", ""), page=page)
        headers["Cross-Origin-Resource-Policy"] = corp_for(page=page)
        headers["Vary"] = "Origin"
        headers["Access-Control-Expose-Headers"] = EXPOSE_HEADERS
        headers["Access-Control-Allow-Methods"] = ALLOW_METHODS
        headers["Access-Control-Allow-Headers"] = ALLOW_HEADERS
        if origin_allowed(origin):
            # Echoed, never ``*``: the list is short and a wildcard would also
            # grant every other page the browser has open.
            headers["Access-Control-Allow-Origin"] = origin


class ContentAccessLog:
    """One access line per content request, with the credentials taken out.

    The API's access log records the path only; a content URL puts its bearer
    token in the query string, and that is exactly the part an operator wants
    (which object, which caller) and must not keep (the token itself).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = {"code": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            raw_query = scope.get("query_string", b"")
            query = scrub_content_query(
                raw_query.decode("latin-1") if isinstance(raw_query, bytes) else str(raw_query)
            )
            target = scrub_content_path(scope.get("path", ""))
            log.info(
                "files.content.access",
                method=scope.get("method"),
                target=f"{target}?{query}" if query else target,
                status=status["code"],
            )


async def nb_output_page(build_hash: str) -> Response:
    """The notebook output frame: a bootstrap that takes everything by message.

    A notebook's HTML, SVG, Vega-Lite, Plotly and widget outputs run here, in a
    frame on the content origin under exactly the HTML policy every other page
    on this mount gets (``ContentHeaders`` stamps it from the content type; this
    route names no header of its own). The page serves no user bytes: renderer
    code and output data arrive only by ``postMessage`` from the notebook tab,
    so the URL carries nothing but the app build's hash, which keeps a page a
    newer build expects from being confused with an older one.

    The script is the whole contract on the frame's side. Its message listener
    is the first thing it registers, before any module exists; it answers only
    ``window.parent``, only a message stamped with this frame's nonce (from the
    URL fragment, which never reaches a server), and only from the app origin it
    has named. That origin is chosen from the list embedded below, never ``*``:
    from ``location.ancestorOrigins`` where the browser has it, and otherwise
    from the first stamped message the parent sends, checked against the list.
    Until it can name one it posts nothing, only queues.

    It sits behind the Files flag like the rest of the mount: a notebook is a
    file in a drive, so a deployment with Files off has no notebook to frame,
    and the dark mount keeps answering one opaque 404 for every path.
    """
    if re.fullmatch(r"[0-9a-f]{8,64}", build_hash) is None:
        raise StarletteHTTPException(status_code=404)
    origins = [origin for origin in allowed_origins() if _framable(origin)]
    # JSON inside a script element: "<" is escaped so no value can close it.
    embedded = json.dumps(origins).replace("<", "\\u003c")
    script = """
(() => {
  "use strict";
  const ORIGINS = __ORIGINS__;
  const MAX_HEIGHT = 20000;
  const MAX_ERRORS = 20;
  const nonce = new URLSearchParams(location.hash.slice(1)).get("n") || "";
  const framed = window.parent !== window;
  const ancestors = location.ancestorOrigins;
  const browserNamesParent = !!ancestors && ancestors.length > 0;
  let parentOrigin = null;
  if (browserNamesParent && ORIGINS.indexOf(ancestors[0]) >= 0) parentOrigin = ancestors[0];
  const queued = [];
  const send = (message, transfer) => {
    if (!framed || !nonce) return;
    if (parentOrigin === null) { queued.push([message, transfer]); return; }
    window.parent.postMessage(message, parentOrigin, transfer || []);
  };
  const post = (type, payload, transfer) =>
    send(Object.assign({}, payload || {}, { alk: 1, frame: nonce, type: type }), transfer);
  const modules = Object.create(null);
  const waiting = Object.create(null);
  const listeners = [];
  let init = null;
  let rendered = false;
  let takesModules = false;
  let errors = 0;
  let theme = "light";

  addEventListener("message", (event) => {
    if (!framed || event.source !== window.parent) return;
    const m = event.data;
    if (!m || typeof m !== "object" || m.alk !== 1 || !nonce || m.frame !== nonce) return;
    if (parentOrigin === null) {
      if (browserNamesParent || ORIGINS.indexOf(event.origin) < 0) return;
      parentOrigin = event.origin;
      for (const [message, transfer] of queued.splice(0)) send(message, transfer);
    } else if (event.origin !== parentOrigin) {
      return;
    }
    receive(m);
  });

  const report = (message) => {
    if (errors >= MAX_ERRORS) return;
    errors += 1;
    post("error", { message: String(message).slice(0, 500) });
  };
  const applyTheme = (next) => {
    theme = next === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = theme;
  };
  const inject = (code) => {
    const script = document.createElement("script");
    script.textContent = code;
    document.head.appendChild(script);
    script.remove();
  };
  const api = {
    root: null,
    post: post,
    theme: () => theme,
    onMessage: (listener) => { listeners.push(listener); },
    loadModule: (name, version) => new Promise((resolve) => {
      const key = name + "@" + version;
      (waiting[key] = waiting[key] || []).push(resolve);
      post("need_module", { name: name, version: version });
    }),
    reportError: report,
  };
  const render = () => {
    if (rendered || init === null) return;
    for (const name in modules) {
      const module = modules[name];
      if (module.mimes.indexOf(init.mime) < 0) continue;
      rendered = true;
      takesModules = module.handlesModules === true;
      api.root = document.getElementById("out");
      try {
        const done = module.render(init, api);
        if (done && typeof done.then === "function") done.then(null, report);
      } catch (error) {
        report(error && error.message ? error.message : error);
      }
      return;
    }
  };
  window.__alkRegister = (name, version, module) => {
    if (typeof name !== "string" || typeof version !== "string" || modules[name] || !module) return;
    if (typeof module.render !== "function" || !Array.isArray(module.mimes)) return;
    modules[name] = module;
    render();
  };
  const dispatch = (m) => {
    for (const listener of listeners) {
      try { listener(m); } catch (error) { report(error); }
    }
  };
  const receive = (m) => {
    if (m.type === "module" && takesModules) {
      dispatch(m);
    } else if (m.type === "module") {
      if (typeof m.name !== "string" || typeof m.code !== "string") return;
      try { inject(m.code); } catch (error) { report(error); }
      const key = m.name + "@" + m.version;
      for (const resolve of (waiting[key] || []).splice(0)) resolve();
      render();
    } else if (m.type === "init") {
      if (init !== null || typeof m.mime !== "string") return;
      init = m;
      applyTheme(m.theme);
      render();
    } else {
      if (m.type === "theme") applyTheme(m.theme);
      dispatch(m);
    }
  };

  addEventListener("error", (event) => report(event.message));
  addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    report(reason && reason.message ? reason.message : reason);
  });
  addEventListener("securitypolicyviolation", (event) =>
    report("Blocked by the output sandbox: " + event.effectiveDirective));
  document.addEventListener("click", (event) => {
    const target = event.target;
    const link = target && target.closest ? target.closest("a") : null;
    if (!link) return;
    const raw = link.getAttribute("href") || link.getAttribute("xlink:href") || "";
    if (raw.charAt(0) === "#") return;
    event.preventDefault();
    try { post("link", { href: new URL(raw).href }); } catch (error) { /* relative */ }
  }, true);
  // The content's own height, not the document's: the document is never
  // shorter than the frame, so a frame drawn taller than its content would
  // be told to stay that tall, and could never shrink.
  let lastHeight = -1;
  const measure = () => {
    const out = document.getElementById("out");
    const bottom = out ? out.getBoundingClientRect().bottom + window.scrollY : 0;
    const height = Math.min(MAX_HEIGHT, Math.ceil(Math.max(bottom, document.body.scrollHeight)));
    if (height === lastHeight) return;
    lastHeight = height;
    post("size", { height: height });
  };
  addEventListener("DOMContentLoaded", () => {
    const observer = new ResizeObserver(measure);
    observer.observe(document.body);
    const out = document.getElementById("out");
    if (out) observer.observe(out);
    measure();
  });
  post("ready", {});
})();
""".replace("__ORIGINS__", embedded)
    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer">'
        "<style>:root{color-scheme:light;color:#1f2328}"
        ":root[data-theme=dark]{color-scheme:dark;color:#e6e6e6}"
        "html,body{margin:0;padding:0;background:transparent;"
        "font:13px/1.45 system-ui,sans-serif}#out{overflow-x:auto}</style>"
        f'<script>{script}</script></head><body><div id="out"></div></body></html>'
    )
    return HTMLResponse(page)


def build_content_app(routers: Sequence[APIRouter] | None = None) -> FastAPI:
    """The content-domain application, ready to mount at :data:`CONTENT_MOUNT_PATH`.

    ``routers`` is a parameter so the middleware stack can be proven against a
    planted route that returns each response shape the recipe has to survive,
    rather than only against whichever families happen to have landed.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    parent = APIRouter(dependencies=[Depends(require_files_enabled)])
    for child in _routers(routers):
        parent.include_router(child)
    parent.add_api_route("/nb-output/{build_hash}", nb_output_page, methods=["GET"])
    app.include_router(parent)
    register_content_error_handlers(app)
    app.add_exception_handler(StarletteHTTPException, opaque_not_found)
    # Added innermost-first: the header stamp must wrap the host gate so a
    # refusal carries the same recipe a served byte does, and the log must wrap
    # both so it records the status that actually went out.
    app.add_middleware(HostGate)
    app.add_middleware(ContentHeaders)
    app.add_middleware(ContentAccessLog)
    return app


def _routers(routers: Sequence[APIRouter] | None) -> Iterable[APIRouter]:
    return routers if routers is not None else discovered_content_routers()


__all__ = [
    "ALLOWED_ORIGIN_SCHEMES",
    "ALLOW_HEADERS",
    "ALLOW_METHODS",
    "CONTENT_HEADERS",
    "CONTENT_MOUNT_PATH",
    "CSP_BY_SNIFFED_TYPE",
    "DEFAULT_CSP",
    "EXPOSE_HEADERS",
    "FALLBACK_FILENAME",
    "LOGGED_SEGMENT_PREFIX",
    "MAX_FILENAME_CHARS",
    "MAX_LOGGED_SEGMENT",
    "NOT_FOUND_BODY",
    "PAGE_PATH_PREFIX",
    "REDACTED",
    "SIGNED_QUERY_KEYS",
    "ContentAccessLog",
    "ContentHeaders",
    "HostGate",
    "allowed_origins",
    "build_content_app",
    "corp_for",
    "csp_for",
    "disposition_header",
    "frame_ancestors",
    "is_page_path",
    "opaque_not_found",
    "origin_allowed",
    "page_path_of",
    "scrub_content_path",
    "scrub_content_query",
]
