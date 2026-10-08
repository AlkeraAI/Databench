"""The content domain's header recipe, its host gate, and its filename builder.

Three separable contracts, each proven where it can actually fail:

* every response that leaves the content mount — a served version, a refusal, a
  416 — carries the whole A5 recipe, and no API response carries any of it;
* the mount and the API refuse each other's hostnames, so user bytes can never
  be served from the origin that holds the session cookie;
* the ``Content-Disposition`` builder survives the names an attacker chooses.

The served/416 shapes come from a planted content router handed to
``build_content_app``: the byte-serving families have not landed, and the
recipe must hold for whichever ones eventually do.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog
from alkera_core.auth.tenancy import ORG_HEADER
from alkera_core.config import settings
from alkera_core.observability.security_headers import API_CSP
from backend.content_app import (
    CONTENT_MOUNT_PATH,
    DEFAULT_CSP,
    EXPOSE_HEADERS,
    FALLBACK_FILENAME,
    build_content_app,
    corp_for,
    csp_for,
    disposition_header,
    frame_ancestors,
    origin_allowed,
    scrub_content_path,
    scrub_content_query,
)
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import Response

CONTENT_HOST = "files.localhost:8000"
CONTENT_ORIGIN = f"http://{CONTENT_HOST}"
APP_ORIGIN = "http://app.example.test"

#: What a probe path's suffix is served as, so one planted route can produce
#: every response shape the per-type table has a recipe for.
PROBE_TYPES = {
    "html": "text/html",
    "pdf": "application/pdf",
    "svg": "image/svg+xml",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "png": "image/png",
    "json": "application/json",
    "bin": "application/octet-stream",
}


@pytest.fixture
def content_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Files enabled with a content origin configured, as production has it."""
    monkeypatch.setattr(settings, "files_enabled", True)
    monkeypatch.setattr(settings, "files_content_base_url", CONTENT_ORIGIN)
    monkeypatch.setattr(settings, "api_cors_origins", APP_ORIGIN)
    monkeypatch.setattr(settings, "frontend_base_url", APP_ORIGIN)


def _probe_router() -> APIRouter:
    """One route per response shape the recipe has to survive."""
    router = APIRouter()

    @router.get("/probe/ok")
    async def _ok() -> Response:
        return Response(b"hello", media_type="text/plain")

    @router.get("/probe/gone")
    async def _gone() -> Response:
        return Response(b'{"code":"not_found"}', status_code=404, media_type="application/json")

    @router.get("/probe/range")
    async def _range() -> Response:
        return Response(status_code=416, headers={"Content-Range": "bytes */5"})

    @router.get("/probe/pdf")
    async def _pdf() -> Response:
        # The disposition comes from the same sniffed value the policy does,
        # built by the one helper every byte-serving route uses.
        return Response(
            b"%PDF-1.7\n",
            media_type="application/pdf",
            headers={"Content-Disposition": disposition_header(b"report.pdf", "application/pdf")},
        )

    @router.get("/probe/html")
    async def _html() -> Response:
        return Response(
            b"<script>alert(1)</script>",
            media_type="text/html",
            headers={"Content-Disposition": disposition_header(b"page.html", "text/html")},
        )

    @router.get("/probe/{group}/{name}")
    async def _typed(group: str, name: str) -> Response:
        """A typed response reached by an ordinary content path.

        ``group`` is free so ``/probe/p/x.html`` exists: a path with ``p`` in
        it that is not the page route, which is what separates a prefix test
        from a substring one.
        """
        return Response(b"bytes", media_type=PROBE_TYPES[name.rpartition(".")[2]])

    @router.get("/p/{token}/{path:path}")
    async def _page(token: str, path: str) -> Response:
        """The page route's shape: a grant token, then a relative path."""
        return Response(b"bytes", media_type=PROBE_TYPES[path.rpartition(".")[2]])

    return router


@pytest.fixture
def content_client(content_on: None) -> Iterator[AsyncClient]:
    """A real API app carrying the real mount, driven over ASGI."""
    host = Starlette()
    host.mount(CONTENT_MOUNT_PATH, build_content_app(routers=[_probe_router()]))
    yield AsyncClient(transport=ASGITransport(app=host), base_url=CONTENT_ORIGIN)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        pytest.param("/c/probe/ok", 200, id="served-version"),
        pytest.param("/c/probe/gone", 404, id="not-found"),
        pytest.param("/c/probe/range", 416, id="range-not-satisfiable"),
        pytest.param("/c/probe/nothing-here", 404, id="unrouted"),
    ],
)
async def test_every_content_response_carries_the_recipe(
    content_client: AsyncClient, path: str, expected_status: int
) -> None:
    async with content_client as client:
        response = await client.get(path, headers={"Origin": APP_ORIGIN})
    assert response.status_code == expected_status
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Access-Control-Expose-Headers"] == EXPOSE_HEADERS
    assert response.headers["Access-Control-Allow-Origin"] == APP_ORIGIN
    assert response.headers["Vary"] == "Origin"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "echoed"),
    [
        pytest.param(APP_ORIGIN, True, id="app-origin"),
        pytest.param("vscode-webview://0d1e2f30-4a5b-6c7d-8e9f-a0b1c2d3e4f5", True, id="extension"),
        pytest.param("https://evil.example", False, id="unknown-origin"),
        pytest.param("", False, id="no-origin"),
    ],
)
async def test_allow_origin_is_echoed_only_for_a_granted_origin(
    content_client: AsyncClient, origin: str, echoed: bool
) -> None:
    headers = {"Origin": origin} if origin else {}
    async with content_client as client:
        response = await client.get("/c/probe/ok", headers=headers)
    assert response.status_code == 200
    if echoed:
        assert response.headers["Access-Control-Allow-Origin"] == origin
    else:
        assert "Access-Control-Allow-Origin" not in response.headers
    # Never a wildcard, whatever the request said.
    assert response.headers.get("Access-Control-Allow-Origin") != "*"


@pytest.mark.asyncio
async def test_the_mount_refuses_the_api_host(content_client: AsyncClient) -> None:
    async with content_client as client:
        response = await client.get("/c/probe/ok", headers={"Host": "api.example.test"})
    assert response.status_code == 404
    assert response.json() == {"code": "not_found"}
    # The refusal is still a content response: a relaxed CSP exactly where a
    # probe looks would be the hole the recipe exists to close.
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"


@pytest.mark.asyncio
async def test_without_a_content_origin_the_mount_serves_content_on_its_own_host(
    content_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No separate origin means same-origin content — the shape a local dev run
    and a single-origin self-hosted install take. The gate opens for any Host
    rather than refusing every one, and the A5 header recipe is still stamped,
    so the bytes are sandboxed even where the cookie can reach them. What keeps
    that safe on SaaS is the production validator, which names a missing content
    origin as a boot error (pinned in ``packages/api-core/tests/test_settings``)."""
    monkeypatch.setattr(settings, "files_content_base_url", None)
    assert settings.files_content_host is None
    host = Starlette()
    host.mount(CONTENT_MOUNT_PATH, build_content_app(routers=[_probe_router()]))
    async with AsyncClient(transport=ASGITransport(app=host), base_url=CONTENT_ORIGIN) as client:
        response = await client.get("/c/probe/ok")
        api_host = await client.get("/c/probe/ok", headers={"Host": "api.example.test"})
    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert api_host.status_code == 200


@pytest.mark.asyncio
async def test_the_mount_answers_the_opaque_404_while_files_is_dark(
    content_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "files_enabled", False)
    host = Starlette()
    host.mount(CONTENT_MOUNT_PATH, build_content_app(routers=[_probe_router()]))
    async with AsyncClient(transport=ASGITransport(app=host), base_url=CONTENT_ORIGIN) as client:
        response = await client.get("/c/probe/ok")
    assert response.status_code == 404
    assert response.json() == {"code": "not_found"}


# --------------------------------------------------------------------------
# The API side of the same boundary.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/health/live", id="health"),
        pytest.param("/api/v1/auth/me", id="authed-route"),
        pytest.param("/api/v1/files/does-not-exist", id="files-api-route"),
    ],
)
async def test_no_api_response_carries_the_content_recipe(
    client: AsyncClient, content_on: None, path: str
) -> None:
    response = await client.get(path)
    assert response.headers["Content-Security-Policy"] == API_CSP
    assert "sandbox" not in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Cache-Control"] != "private, no-store"
    # The API exposes the org echo to the portal and nothing of the content
    # recipe's exposures.
    assert response.headers["Access-Control-Expose-Headers"] == ORG_HEADER


@pytest.mark.asyncio
async def test_the_api_refuses_the_content_host(client: AsyncClient, content_on: None) -> None:
    response = await client.get("/health/live", headers={"Host": CONTENT_HOST})
    assert response.status_code == 404
    assert response.json() == {"code": "not_found"}


@pytest.mark.asyncio
async def test_the_api_refuses_a_content_path_under_its_own_host(
    client: AsyncClient, content_on: None
) -> None:
    response = await client.get("/c/probe/ok")
    assert response.status_code == 404
    assert response.json() == {"code": "not_found"}
    # The guard answered, not the mount: the refusal carries the API's locked-down
    # recipe, never the content sandbox a served byte would get.
    assert response.headers["Content-Security-Policy"] == API_CSP


# --------------------------------------------------------------------------
# RFC 6266 filenames.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param(
            b"report.csv",
            "attachment; filename=\"report.csv\"; filename*=UTF-8''report.csv",
            id="ascii",
        ),
        pytest.param(
            "rapport-é.txt".encode(),
            "attachment; filename=\"rapport-_.txt\"; filename*=UTF-8''rapport-%C3%A9.txt",
            id="utf8",
        ),
        pytest.param(
            b'we"ird.txt',
            "attachment; filename=\"we_ird.txt\"; filename*=UTF-8''we%22ird.txt",
            id="double-quote-cannot-close-the-token",
        ),
        pytest.param(
            b"line\r\nSet-Cookie: a=b.txt",
            'attachment; filename="lineSet-Cookie: a=b.txt"; '
            "filename*=UTF-8''lineSet-Cookie%3A%20a%3Db.txt",
            id="crlf-stripped",
        ),
        pytest.param(
            "photo‮gnp.exe".encode(),
            "attachment; filename=\"photognp.exe\"; filename*=UTF-8''photognp.exe",
            id="bidi-override-stripped",
        ),
        pytest.param(
            b"",
            f"attachment; filename=\"{FALLBACK_FILENAME}\"; filename*=UTF-8''{FALLBACK_FILENAME}",
            id="empty-name",
        ),
    ],
)
def test_disposition_header_table(name: bytes, expected: str) -> None:
    assert disposition_header(name, "application/octet-stream") == expected


def test_a_300_byte_name_is_truncated_and_stays_one_header_line() -> None:
    header = disposition_header(b"a" * 300, "application/octet-stream")
    assert 'filename="' + "a" * 200 + '"' in header
    assert "a" * 201 not in header
    assert "\r" not in header and "\n" not in header


@pytest.mark.parametrize(
    ("mime", "disposition"),
    [
        pytest.param("image/png", "inline", id="allowlisted-png"),
        pytest.param("text/plain", "inline", id="allowlisted-text"),
        pytest.param("application/pdf", "inline", id="allowlisted-pdf"),
        pytest.param("text/html", "inline", id="allowlisted-html"),
        # A drawing is served inline under the SVG content policy (no scripts, no
        # external loads); the sniffer only names it SVG when the head starts with <svg.
        pytest.param("image/svg+xml", "inline", id="svg-inline"),
        pytest.param("application/octet-stream", "attachment", id="unknown"),
    ],
)
def test_inline_only_for_the_sniffed_allowlist(mime: str, disposition: str) -> None:
    assert disposition_header(b"x.bin", mime).startswith(f"{disposition}; ")


# --------------------------------------------------------------------------
# The CSP is per response AND per path: a PDF steps out of the sandbox, a page
# renders inside it, and a page served under a grant may load the files beside it.
# --------------------------------------------------------------------------

#: The one origin the ``content_on`` fixture configures, spelled as the
#: ``frame-ancestors`` source list it has to become.
ANCESTORS = APP_ORIGIN

#: Every recipe of the header contract, byte for byte, as (sniffed type, page,
#: policy). Spelled out rather than built, so a change to the builder that
#: alters a single source shows up as a diff against a literal string.
RECIPES = [
    pytest.param(
        "text/html",
        False,
        "default-src 'none'; script-src 'unsafe-inline'; object-src 'none'; "
        "style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; "
        f"frame-ancestors {ANCESTORS}; base-uri 'none'; form-action 'none'; "
        "sandbox allow-scripts",
        id="html-single-file",
    ),
    pytest.param(
        "text/html",
        True,
        "default-src 'none'; script-src 'self' 'unsafe-inline'; object-src 'none'; "
        "style-src 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
        f"media-src 'self' data:; frame-ancestors {ANCESTORS}; "
        "base-uri 'none'; form-action 'none'; sandbox allow-scripts",
        id="html-page",
    ),
    pytest.param(
        "application/pdf",
        False,
        "default-src 'none'; object-src 'none'; script-src 'none'; style-src 'none'; "
        f"frame-ancestors {ANCESTORS}; base-uri 'none'; form-action 'none'",
        id="pdf-single-file",
    ),
    pytest.param(
        "application/pdf",
        True,
        "default-src 'none'; object-src 'none'; script-src 'none'; style-src 'none'; "
        f"frame-ancestors {ANCESTORS}; base-uri 'none'; form-action 'none'",
        id="pdf-page",
    ),
    pytest.param(
        "image/svg+xml",
        False,
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
        f"frame-ancestors {ANCESTORS}; base-uri 'none'; form-action 'none'; sandbox",
        id="svg-single-file",
    ),
    pytest.param(
        "image/svg+xml",
        True,
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
        f"frame-ancestors {ANCESTORS}; base-uri 'none'; form-action 'none'; sandbox",
        id="svg-page",
    ),
    *[
        pytest.param(
            mime,
            page,
            f"default-src 'none'; frame-ancestors {ANCESTORS}; sandbox",
            id=f"{mime.replace('/', '-')}-{'page' if page else 'single-file'}",
        )
        for mime in ("video/mp4", "video/webm", "audio/mpeg", "audio/wav")
        for page in (False, True)
    ],
]


@pytest.mark.parametrize(("mime", "page", "policy"), RECIPES)
def test_the_policy_for_every_reasoned_type(
    content_on: None, mime: str, page: bool, policy: str
) -> None:
    assert csp_for(mime, page=page) == policy


@pytest.mark.asyncio
@pytest.mark.parametrize(("mime", "page", "policy"), RECIPES)
async def test_the_policy_reaches_the_wire_through_the_real_mount(
    content_client: AsyncClient, mime: str, page: bool, policy: str
) -> None:
    """The same recipes again, but stamped by the middleware on a real response.

    ``page`` is not a parameter any route passes: it is read off the path the
    mount saw, so the only way to prove it is to ask for the two paths and
    compare what comes back.
    """
    suffix = next(key for key, value in PROBE_TYPES.items() if value == mime)
    path = f"/c/p/tok/report.{suffix}" if page else f"/c/probe/typed/report.{suffix}"
    async with content_client as client:
        response = await client.get(path, headers={"Origin": APP_ORIGIN})
    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == policy


@pytest.mark.parametrize(
    "content_type",
    [
        pytest.param("application/json", id="json"),
        pytest.param("image/png", id="png"),
        pytest.param("text/plain", id="text"),
        pytest.param("application/octet-stream", id="unknown"),
        pytest.param("", id="absent"),
    ],
)
@pytest.mark.parametrize("page", [False, True])
def test_every_unreasoned_type_keeps_the_default_recipe(
    content_on: None, content_type: str, page: bool
) -> None:
    """The allowlist shape again, one layer down.

    A type nobody reasoned about -- including the empty one a refusal carries --
    gets the default policy on either path, so a sniffer that learns a type
    before this table does cannot relax the policy by itself, and a grant's
    path cannot relax it either.
    """
    assert csp_for(content_type, page=page) == DEFAULT_CSP
    assert "sandbox" in csp_for(content_type, page=page)
    assert "frame-ancestors" not in csp_for(content_type, page=page)


@pytest.mark.parametrize(
    "content_type",
    [
        pytest.param("text/html", id="bare"),
        pytest.param("text/html; charset=utf-8", id="with-a-parameter"),
        pytest.param("TEXT/HTML", id="upper-case"),
    ],
)
@pytest.mark.parametrize("page", [False, True])
def test_html_renders_without_ever_leaving_the_sandbox(
    content_on: None, content_type: str, page: bool
) -> None:
    """The other half of the exchange, asserted rather than described.

    A PDF gives up the sandbox and keeps the fetch directives; a page does the
    reverse. It stays sandboxed with no allowances -- so no script, no form, no
    same-origin -- and the only thing a grant's path buys is the folder beside
    it: ``'self'`` on the asset directives, never on script or style, and the
    app's own origins in ``frame-ancestors`` so the preview can frame it.
    """
    policy = csp_for(content_type, page=page)
    assert policy != DEFAULT_CSP
    # Scripts run, and nothing else is allowed: no same-origin (the opaque origin
    # is the whole containment), no forms, popups, modals or top navigation.
    sandbox = next(d.strip() for d in policy.split(";") if d.strip().startswith("sandbox"))
    assert sandbox == "sandbox allow-scripts"
    # No network: default-src 'none' with no connect/worker/frame source.
    assert "default-src 'none'" in policy
    for directive in ("connect-src", "worker-src", "frame-src", "child-src"):
        assert directive not in policy
    assert "object-src 'none'" in policy
    assert "form-action 'none'" in policy
    assert "base-uri 'none'" in policy
    assert f"frame-ancestors {ANCESTORS}" in policy
    assert "style-src 'unsafe-inline'" in policy
    assert "'unsafe-eval'" not in policy
    # ``'self'`` is what a page under a grant gets and a single file does not,
    # and it never reaches a directive that executes.
    assert ("img-src 'self' data:" in policy) is page
    # Scripts are the page's own: inline, or the files beside it under a grant;
    # never another host.
    assert ("script-src 'self' 'unsafe-inline'" in policy) is page
    script_src = next(d.strip() for d in policy.split(";") if d.strip().startswith("script-src"))
    assert "http" not in script_src and "*" not in script_src
    assert "style-src 'self'" not in policy


@pytest.mark.parametrize(
    "content_type",
    [
        pytest.param("application/pdf", id="bare"),
        pytest.param("application/pdf; charset=binary", id="with-a-parameter"),
        pytest.param("APPLICATION/PDF", id="upper-case"),
    ],
)
@pytest.mark.parametrize("page", [False, True])
def test_a_pdf_drops_the_sandbox_and_keeps_everything_else(
    content_on: None, content_type: str, page: bool
) -> None:
    """The exchange, asserted rather than described.

    ``sandbox`` goes, because a sandboxed document cannot instantiate the PDF
    viewer and the response would render as a blank frame. Nothing else does:
    the document may still fetch nothing, run nothing, and be framed by nobody
    outside the app.
    """
    policy = csp_for(content_type, page=page)
    assert "sandbox" not in policy
    assert "default-src 'none'" in policy
    assert "script-src 'none'" in policy
    assert "object-src 'none'" in policy
    assert f"frame-ancestors {ANCESTORS}" in policy
    assert "form-action 'none'" in policy


# --------------------------------------------------------------------------
# Who may frame a content response.
# --------------------------------------------------------------------------


def test_frame_ancestors_is_exactly_the_configured_origins(
    content_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "api_cors_origins", f"{APP_ORIGIN},https://portal.example.test")
    monkeypatch.setattr(settings, "frontend_base_url", APP_ORIGIN)
    assert frame_ancestors() == f"{APP_ORIGIN} https://portal.example.test"


def test_frame_ancestors_is_none_when_no_origin_is_configured(
    content_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty list must close the door, not open it.

    A join over nothing is the empty string, and a ``frame-ancestors`` with no
    sources is a directive browsers cannot parse -- which would leave the page
    framable by anyone. The fallback is the literal ``'none'``.
    """
    monkeypatch.setattr(settings, "api_cors_origins", "")
    monkeypatch.setattr(settings, "frontend_base_url", "")
    assert frame_ancestors() == "'none'"
    assert "frame-ancestors 'none'" in csp_for("text/html", page=True)


@pytest.mark.parametrize(
    "configured",
    [
        pytest.param("*", id="bare-wildcard"),
        pytest.param(f"{APP_ORIGIN},*", id="wildcard-beside-an-origin"),
        pytest.param("https://*.example.test", id="wildcard-host"),
    ],
)
def test_a_wildcard_origin_never_reaches_frame_ancestors(
    content_on: None, monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    """``frame-ancestors *`` is every page on the web, which is the control off.

    The CORS list is a deployment-time string and a wildcard in it is a
    plausible thing for an operator to write; it may echo an Allow-Origin, but
    it may never become the list of documents allowed to frame a user's bytes.
    """
    monkeypatch.setattr(settings, "api_cors_origins", configured)
    monkeypatch.setattr(settings, "frontend_base_url", "")
    assert "*" not in frame_ancestors()
    assert "*" not in csp_for("text/html", page=True)


def test_the_extension_scheme_may_read_bytes_but_may_not_frame_a_page(
    content_on: None,
) -> None:
    """The one place the two origin lists must differ.

    ``vscode-webview:`` is granted by SCHEME for reads, because the webview's
    origin carries a per-window uuid nobody can pin. A ``frame-ancestors``
    source of the bare scheme would admit every webview of every extension the
    user has installed, so the framing list stays the exact origins.
    """
    assert origin_allowed("vscode-webview://0d1e2f30-4a5b-6c7d-8e9f-a0b1c2d3e4f5") is True
    assert "vscode-webview" not in frame_ancestors()
    assert "vscode-webview" not in csp_for("text/html", page=True)


# --------------------------------------------------------------------------
# A page may be framed and read; everything else stays same-origin.
# --------------------------------------------------------------------------


def test_corp_relaxes_only_for_a_page() -> None:
    assert corp_for(page=False) == "same-origin"
    assert corp_for(page=True) == "cross-origin"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "corp", "assets"),
    [
        pytest.param("/c/p/tok/report.html", "cross-origin", True, id="page"),
        pytest.param("/c/p/tok/deep/report.html", "cross-origin", True, id="page-nested"),
        pytest.param("/c/probe/typed/report.html", "same-origin", False, id="single-file"),
        pytest.param("/c/probe/p/report.html", "same-origin", False, id="p-is-not-a-prefix-here"),
    ],
)
async def test_the_page_path_is_a_prefix_and_not_a_substring(
    content_client: AsyncClient, path: str, corp: str, assets: bool
) -> None:
    """Both halves of the path decision, on the wire.

    ``/c/probe/p/...`` contains ``p/`` and is not the page route; reading the
    path with ``in`` instead of a prefix test would hand a plain content path a
    page's relaxations, which is the whole exception leaking.
    """
    async with content_client as client:
        response = await client.get(path, headers={"Origin": APP_ORIGIN})
    assert response.status_code == 200
    assert response.headers["Cross-Origin-Resource-Policy"] == corp
    assert ("img-src 'self' data:" in response.headers["Content-Security-Policy"]) is assets


@pytest.mark.asyncio
async def test_a_page_path_under_the_wrong_host_is_still_refused(
    content_client: AsyncClient,
) -> None:
    """The host gate runs inside the header stamp, so the relaxation cannot be
    the thing that lets a misrouted request through."""
    async with content_client as client:
        response = await client.get(
            "/c/p/tok/report.html", headers={"Host": "api.example.test", "Origin": APP_ORIGIN}
        )
    assert response.status_code == 404
    assert response.json() == {"code": "not_found"}
    assert response.headers["Content-Security-Policy"] == DEFAULT_CSP


@pytest.mark.asyncio
async def test_an_html_response_renders_inline_but_can_never_run_script(
    content_client: AsyncClient,
) -> None:
    """A self-contained page is a deliverable, so it has to open.

    The agent is told to write one of two artefact formats -- a PDF or an HTML
    page -- so an HTML answer that can only be saved to disk is half the product
    missing. It is served ``inline``, and the page it renders is one that cannot
    reach anything: ``sandbox allow-scripts`` keeps it in an opaque origin with
    no network, and the fetch directives let through only the inline CSS and
    ``data:`` assets a self-contained page is made of.
    """
    async with content_client as client:
        response = await client.get("/c/probe/html", headers={"Origin": APP_ORIGIN})
    assert response.status_code == 200
    assert response.headers["Content-Disposition"].startswith("inline; ")
    policy = response.headers["Content-Security-Policy"]
    # Still sandboxed: its own scripts run, and nothing else is allowed --
    # no allow-same-origin, no allow-forms, no popups, no top navigation.
    assert "sandbox allow-scripts" in policy
    assert "allow-same-origin" not in policy and "allow-forms" not in policy
    assert "allow-popups" not in policy and "allow-top-navigation" not in policy
    assert "script-src 'unsafe-inline'" in policy
    assert "object-src 'none'" in policy
    assert "form-action 'none'" in policy
    assert "base-uri 'none'" in policy
    assert f"frame-ancestors {ANCESTORS}" in policy
    # ...but the page actually renders: embedded CSS and data: assets.
    assert "style-src 'unsafe-inline'" in policy
    assert "img-src data:" in policy
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/c/probe/html", id="single-file"),
        pytest.param("/c/p/tok/report.html", id="page"),
    ],
)
async def test_the_html_policy_never_lets_a_page_reach_the_network(
    content_client: AsyncClient, path: str
) -> None:
    """The one thing rendering HTML here must not buy an attacker.

    Serving a page off the host that holds unscanned bytes is only safe while
    the page is inert: no script to run, and no origin to call. ``default-src``
    stays ``'none'``, and the relaxations name no URL -- ``'unsafe-inline'``
    styles, ``data:`` assets, and at most ``'self'``, which is the grant's own
    folder and nothing beyond it. ``frame-ancestors`` is the exception by
    construction: it names who may embed the response, never what it may fetch.
    """
    async with content_client as client:
        response = await client.get(path, headers={"Origin": APP_ORIGIN})
    policy = response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy
    for directive in policy.split(";"):
        parts = directive.strip().split()
        if not parts or parts[0] == "frame-ancestors":
            continue
        sources = parts[1:]
        assert "*" not in sources
        assert "'unsafe-eval'" not in sources
        assert not any(source.startswith("http") for source in sources)


@pytest.mark.asyncio
async def test_a_pdf_response_carries_its_own_policy_and_renders_inline(
    content_client: AsyncClient,
) -> None:
    async with content_client as client:
        response = await client.get("/c/probe/pdf", headers={"Origin": APP_ORIGIN})
    assert response.status_code == 200
    assert "sandbox" not in response.headers["Content-Security-Policy"]
    assert response.headers["Content-Disposition"].startswith("inline; ")
    # The rest of the recipe is untouched -- only the CSP and CORP are per-response.
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert response.headers["Cache-Control"] == "private, no-store"


# --------------------------------------------------------------------------
# Signed tokens never reach a log.
# --------------------------------------------------------------------------


def test_scrub_keeps_the_shape_and_drops_the_secret() -> None:
    scrubbed = scrub_content_query("token=s3cr3t-value&v=7&X-Amz-Signature=deadbeef")
    assert "s3cr3t-value" not in scrubbed
    assert "deadbeef" not in scrubbed
    assert scrubbed == "token=REDACTED&v=7&X-Amz-Signature=REDACTED"


@pytest.mark.asyncio
async def test_the_content_access_log_line_carries_no_token(
    content_client: AsyncClient,
) -> None:
    """Aimed at the shape a mint really produces: the token IS the path.

    A line built from ``scope["path"]`` verbatim writes a live bearer
    credential into the operator's log, where anyone who can read the log can
    redeem it. Both halves are asserted at once because both are on the wire at
    once, and the route and the head of the nonce survive so an operator can
    still say which download a line is about.
    """
    nonce = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
    token = f"{nonce}.Y2xhaW0.{'a' * 64}"

    with structlog.testing.capture_logs() as records:
        async with content_client as client:
            response = await client.get(f"/c/{token}?token=s3cr3t-value&v=7")

    assert response.status_code == 404
    access = [r for r in records if r.get("event") == "files.content.access"]
    assert access, "the content mount logged no access line"
    assert access[-1]["target"] == "/c/9f8e7d6c\u2026?token=REDACTED&v=7"
    assert access[-1]["status"] == 404
    assert not any("s3cr3t-value" in str(record) for record in records)
    assert not any(token in str(record) for record in records)


@pytest.mark.asyncio
async def test_the_mount_is_wired_into_the_real_app(content_on: None) -> None:
    """The production app really carries the mount, not just the fixture's host."""
    from backend.app_factory import create_app

    app: FastAPI = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CONTENT_ORIGIN) as client:
        response = await client.get("/c/anything")
    assert response.status_code == 404
    # Reached the mount (its recipe is on the refusal), not the API's 404.
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"


# --------------------------------------------------------------------------
# The token IS the path, so the path is a credential too.
# --------------------------------------------------------------------------


def test_scrub_keeps_the_route_and_cuts_the_credential_segment() -> None:
    """A real minted shape, not a placeholder: ``/c/<nonce>.<claim>.<sig>``.

    The nonce head survives so two lines about one download correlate; nothing
    that could be replayed does.
    """
    nonce = "0123456789abcdef0123456789abcdef"
    token = f"{nonce}.Y2xhaW0.{'f' * 64}"
    scrubbed = scrub_content_path(f"/c/{token}")
    assert token not in scrubbed
    assert scrubbed == "/c/01234567\u2026"
    assert scrub_content_path("/c/probe/ok") == "/c/probe/ok"


@pytest.mark.asyncio
async def test_a_method_the_mount_refuses_answers_the_one_opaque_body(
    content_client: AsyncClient,
) -> None:
    """A 405 says "this address routes", which is an existence oracle.

    Byte-identical to the answer for an address that does not route at all, and
    carrying the same recipe, so a prober holding no token learns nothing from
    either.
    """
    async with content_client as client:
        refused = await client.post("/c/probe/ok")
        absent = await client.post("/c/nothing-here")

    assert refused.status_code == 404
    assert refused.content == absent.content == b'{"code":"not_found"}'
    assert refused.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert refused.headers["Cache-Control"] == "private, no-store"
