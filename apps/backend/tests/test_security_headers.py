"""Security response headers on the API.

A deployment may put the API behind a load balancer that adds no headers of
its own, so they have to come from the app. Each header is pinned by name
here, because a scanner checks presence and a silently dropped one is a new
finding.
"""

from __future__ import annotations

import pytest
from alkera_core.config import settings
from alkera_core.observability.security_headers import (
    API_CSP,
    HTML_CSP,
    SecurityHeadersMiddleware,
)
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

# Exactly the nine the scan named missing, minus Cross-Origin-Embedder-Policy
# (see test_coep_is_deliberately_absent below).
REQUIRED_HEADERS = [
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Content-Security-Policy",
    "Permissions-Policy",
    "Cross-Origin-Opener-Policy",
    "Cross-Origin-Resource-Policy",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("header", REQUIRED_HEADERS)
@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/health/live", id="200-ok"),
        pytest.param("/api/v1/auth/me", id="401-unauthenticated"),
        pytest.param("/definitely-not-a-route", id="404-not-found"),
    ],
)
async def test_every_response_carries_the_header(
    client: AsyncClient, path: str, header: str
) -> None:
    """Success, auth failure, and not-found all go out through different code
    paths inside Starlette — a scanner will hit all three."""
    resp = await client.get(path)
    assert resp.headers.get(header), f"{header} missing on {path} ({resp.status_code})"


@pytest.mark.asyncio
async def test_json_responses_get_the_locked_down_policy(client: AsyncClient) -> None:
    """A JSON endpoint loads no scripts, embeds no frames, and posts no forms, so
    `default-src 'none'` costs nothing and makes any future reflected-HTML bug
    inert."""
    resp = await client.get("/health/live")
    assert resp.headers["content-security-policy"] == API_CSP
    assert "frame-ancestors 'none'" in API_CSP


@pytest.mark.asyncio
async def test_api_responses_are_not_cacheable(client: AsyncClient) -> None:
    """API bodies are per-session by construction and must not be reused from a
    shared proxy or a shared machine's disk cache."""
    resp = await client.get("/api/v1/auth/me")
    assert "no-store" in resp.headers["cache-control"]
    assert resp.headers["pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_hsts_is_off_in_local_dev(client: AsyncClient) -> None:
    """A browser that has once seen HSTS for `localhost` refuses plain http on
    EVERY localhost port afterwards, across every project on the machine — and it
    is not the browser's to undo per-port. So it is deployment-gated, not
    unconditional."""
    assert settings.is_local
    resp = await client.get("/health/live")
    assert "strict-transport-security" not in resp.headers


def _probe_app(**kwargs: object) -> Starlette:
    async def json_route(_request):  # type: ignore[no-untyped-def]
        return JSONResponse({"ok": True})

    async def html_route(_request):  # type: ignore[no-untyped-def]
        return HTMLResponse("<p>docs</p>")

    async def cached_route(_request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("x", headers={"Cache-Control": "public, max-age=60"})

    app = Starlette(
        routes=[
            Route("/json", json_route),
            Route("/html", html_route),
            Route("/cached", cached_route),
        ]
    )
    app.add_middleware(SecurityHeadersMiddleware, **kwargs)  # type: ignore[arg-type]
    return app


async def _get(app: Starlette, path: str):  # type: ignore[no-untyped-def]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://probe") as c:
        return await c.get(path)


@pytest.mark.asyncio
async def test_hsts_is_emitted_when_enabled() -> None:
    resp = await _get(_probe_app(hsts=True, hsts_max_age=63_072_000), "/json")
    hsts = resp.headers["strict-transport-security"]
    assert "max-age=63072000" in hsts
    assert "includeSubDomains" in hsts
    # Never `preload`: committing a domain to the browser preload list is a
    # separate, hard-to-reverse decision, not a side effect of this middleware.
    assert "preload" not in hsts


@pytest.mark.asyncio
async def test_html_responses_get_the_relaxed_policy() -> None:
    """`/docs` and `/redoc` legitimately load scripts and styles. One policy
    cannot serve both them and a JSON API, so the middleware branches on
    content-type — and the HTML branch still forbids framing."""
    resp = await _get(_probe_app(hsts=False), "/html")
    assert resp.headers["content-security-policy"] == HTML_CSP
    assert "frame-ancestors 'none'" in HTML_CSP


@pytest.mark.asyncio
async def test_a_route_that_chose_its_own_cache_policy_keeps_it() -> None:
    """The audit-log CSV export sets its own headers deliberately; a blanket
    overwrite here would silently undo that."""
    resp = await _get(_probe_app(hsts=False), "/cached")
    assert resp.headers["cache-control"] == "public, max-age=60"
    assert "pragma" not in resp.headers


@pytest.mark.asyncio
async def test_cross_origin_resource_policy_is_configurable() -> None:
    """`same-site` fits the SaaS shape (app. + api. share a registrable domain);
    a self-hosted install that splits them across unrelated domains needs
    `cross-origin`."""
    resp = await _get(_probe_app(hsts=False, cross_origin_resource_policy="cross-origin"), "/json")
    assert resp.headers["cross-origin-resource-policy"] == "cross-origin"


@pytest.mark.asyncio
async def test_coep_is_deliberately_absent(client: AsyncClient) -> None:
    """The scan listed Cross-Origin-Embedder-Policy among the missing nine. It is
    left off on purpose: `require-corp` governs cross-origin ISOLATION for a
    document that embeds subresources, a JSON API embeds none, and turning it on
    breaks the CDN-loaded Swagger UI in local dev for no gain. Pinned so the
    omission stays a decision rather than drift."""
    resp = await client.get("/health/live")
    assert "cross-origin-embedder-policy" not in resp.headers


#: The content origin the sweep configures, as production has one.
CONTENT_HOST = "files.localhost:8000"


@pytest.fixture
def content_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "files_enabled", True)
    monkeypatch.setattr(settings, "files_content_base_url", f"http://{CONTENT_HOST}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "headers", "cookies", "status"),
    [
        pytest.param("GET", "/definitely-not-a-route", {}, {}, 404, id="404"),
        pytest.param("DELETE", "/health/live", {}, {}, 405, id="405"),
        pytest.param("POST", "/api/v1/auth/login", {}, {}, 422, id="422"),
        pytest.param("GET", "/api/v1/auth/me", {}, {}, 401, id="401"),
        pytest.param(
            "POST",
            "/api/v1/auth/logout",
            {"Origin": "https://elsewhere.example"},
            {"alkera_session": "forged"},
            403,
            id="403-cross-origin-write",
        ),
        pytest.param("POST", "/api/v1/webhooks/github", {}, {}, 503, id="503-webhook-unconfigured"),
        pytest.param(
            "GET", "/", {"Host": CONTENT_HOST}, {}, 404, id="404-api-path-on-the-content-origin"
        ),
        pytest.param("GET", "/c/anything", {}, {}, 404, id="404-content-path-on-the-api-origin"),
    ],
)
async def test_every_refusal_carries_the_headers(
    client: AsyncClient,
    content_origin: None,
    method: str,
    path: str,
    headers: dict[str, str],
    cookies: dict[str, str],
    status: int,
) -> None:
    """A refusal is a response a browser can be pointed at like any other, so
    each way the API refuses goes out with the full recipe, the edge's own
    refusals included."""
    client.cookies.update(cookies)
    resp = await client.request(
        method, path, headers=headers, json={} if method == "POST" else None
    )
    assert resp.status_code == status, resp.text
    missing = [h for h in REQUIRED_HEADERS if not resp.headers.get(h)]
    assert not missing, f"{missing} missing on {method} {path} ({resp.status_code})"
    assert resp.headers["content-security-policy"] == API_CSP


@pytest.mark.asyncio
async def test_a_refusal_on_the_content_mount_carries_its_own_recipe(
    client: AsyncClient, content_origin: None
) -> None:
    """The content plane stamps its stricter sandbox recipe on its own 404."""
    resp = await client.get("/c/no/such/thing", headers={"Host": CONTENT_HOST})
    assert resp.status_code == 404
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in resp.headers["content-security-policy"]
