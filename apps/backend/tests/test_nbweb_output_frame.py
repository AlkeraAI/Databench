"""The notebook output frame's bootstrap page on the content mount.

The page is the frame side of the output contract: it must be served under
exactly the content origin's HTML policy (no relaxation for being "ours"),
name the app origins it may answer, and never post to a wildcard. Its runtime
behaviour is proven in a real browser by ``apps/web/e2e/nb-output-frame.spec.ts``;
these tests pin what the server hands that browser.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import pytest
from alkera_core.config import settings
from backend.content_app import CONTENT_MOUNT_PATH, DEFAULT_CSP, build_content_app
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette

CONTENT_HOST = "files.localhost:8000"
CONTENT_ORIGIN = f"http://{CONTENT_HOST}"
APP_ORIGIN = "http://app.example.test"
SECOND_ORIGIN = "http://second.example.test"
BUILD_HASH = "0123456789abcdef"

EXPECTED_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; object-src 'none'; "
    "style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; "
    f"frame-ancestors {APP_ORIGIN} {SECOND_ORIGIN}; base-uri 'none'; form-action 'none'; "
    "sandbox allow-scripts"
)


@pytest.fixture
def content_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "files_enabled", True)
    monkeypatch.setattr(settings, "files_content_base_url", CONTENT_ORIGIN)
    # A wildcard and a policy-breaking value an operator might write: neither
    # may become an origin the frame answers.
    monkeypatch.setattr(
        settings,
        "api_cors_origins",
        f"{APP_ORIGIN},https://*.example.test,{SECOND_ORIGIN},http://bad;x",
    )
    monkeypatch.setattr(settings, "frontend_base_url", APP_ORIGIN)


@pytest.fixture
def client(content_on: None) -> Iterator[AsyncClient]:
    host = Starlette()
    host.mount(CONTENT_MOUNT_PATH, build_content_app(routers=[]))
    yield AsyncClient(transport=ASGITransport(app=host), base_url=CONTENT_ORIGIN)


def _embedded_origins(body: str) -> list[str]:
    match = re.search(r"const ORIGINS = (\[.*?\]);", body)
    assert match is not None, "the bootstrap embeds no origin list"
    origins = json.loads(match.group(1))
    assert isinstance(origins, list)
    return [str(origin) for origin in origins]


@pytest.mark.asyncio
async def test_the_page_is_served_under_the_unchanged_html_policy(client: AsyncClient) -> None:
    async with client as c:
        response = await c.get(f"/c/nb-output/{BUILD_HASH}")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/html")
    assert response.headers["Content-Security-Policy"] == EXPECTED_CSP
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"


@pytest.mark.asyncio
async def test_the_page_embeds_exactly_the_framable_app_origins(client: AsyncClient) -> None:
    async with client as c:
        body = (await c.get(f"/c/nb-output/{BUILD_HASH}")).text
    assert _embedded_origins(body) == [APP_ORIGIN, SECOND_ORIGIN]


@pytest.mark.asyncio
async def test_the_page_never_posts_to_a_wildcard(client: AsyncClient) -> None:
    async with client as c:
        body = (await c.get(f"/c/nb-output/{BUILD_HASH}")).text
    posts = re.findall(r"postMessage\(([^)]*)\)", body)
    assert posts, "the bootstrap posts nothing at all"
    for args in posts:
        target = args.split(",")[1].strip()
        assert target == "parentOrigin"
    assert '"*"' not in body
    assert "'*'" not in body


@pytest.mark.asyncio
async def test_the_listener_is_registered_before_any_module_can_load(client: AsyncClient) -> None:
    async with client as c:
        body = (await c.get(f"/c/nb-output/{BUILD_HASH}")).text
    listener = body.index('addEventListener("message"')
    assert listener < body.index("window.__alkRegister =")
    assert listener < body.index('document.createElement("script")')
    assert "event.source !== window.parent" in body


@pytest.mark.asyncio
async def test_an_origin_cannot_close_the_script_it_is_embedded_in(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = "http://x</script><script>alert(1)</script>"
    monkeypatch.setattr(settings, "api_cors_origins", f"{APP_ORIGIN},{hostile}")
    async with client as c:
        body = (await c.get(f"/c/nb-output/{BUILD_HASH}")).text
    assert body.count("</script>") == 1
    assert hostile in _embedded_origins(body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build_hash",
    [
        pytest.param("0123456", id="too-short"),
        pytest.param("a" * 65, id="too-long"),
        pytest.param("0123456789ABCDEF", id="upper-case"),
        pytest.param("0123456789abcdeg", id="not-hex"),
        pytest.param("..%2F..%2Fetc", id="traversal"),
    ],
)
async def test_a_malformed_build_hash_is_the_opaque_404(
    client: AsyncClient, build_hash: str
) -> None:
    async with client as c:
        response = await c.get(f"/c/nb-output/{build_hash}")
    assert response.status_code == 404
    assert response.json() == {"code": "not_found"}
    assert response.headers["Content-Security-Policy"] == DEFAULT_CSP


@pytest.mark.asyncio
async def test_a_dark_files_deployment_does_not_serve_the_page(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "files_enabled", False)
    async with client as c:
        response = await c.get(f"/c/nb-output/{BUILD_HASH}")
    assert response.status_code == 404
    assert response.json() == {"code": "not_found"}


@pytest.mark.asyncio
async def test_the_page_is_not_served_under_the_api_host(client: AsyncClient) -> None:
    async with client as c:
        response = await c.get(f"/c/nb-output/{BUILD_HASH}", headers={"Host": "api.example.test"})
    assert response.status_code == 404
    assert response.headers["Content-Security-Policy"] == DEFAULT_CSP
