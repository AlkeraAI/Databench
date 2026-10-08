"""The edge-gate return: the API sends a browser back to the app, and only there.

A sign-in gate at the API host's load balancer sets its cookie on a top-level
navigation only; the app visits this route once so the gate can run, and the
answer must land on the app's own origin whatever the caller put in ``next``.
"""

from __future__ import annotations

import pytest
from alkera_core.config import settings
from httpx import AsyncClient

RETURN = "/api/v1/auth/gate/return"


def _app(path: str) -> str:
    return f"{settings.frontend_base_url.rstrip('/')}{path}"


@pytest.mark.parametrize(
    ("next_path", "landing"),
    [
        pytest.param("/workspace/chats/abc?tab=files", "/workspace/chats/abc?tab=files", id="path"),
        pytest.param("/", "/", id="root"),
        pytest.param(None, "/dashboard", id="absent"),
        pytest.param("", "/dashboard", id="empty"),
        pytest.param("https://evil.example/x", "/dashboard", id="absolute-url"),
        pytest.param("//evil.example/x", "/dashboard", id="protocol-relative"),
        pytest.param("/\\evil.example", "/dashboard", id="backslash"),
        pytest.param("/x\r\nSet-Cookie: a=b", "/dashboard", id="crlf"),
        pytest.param("workspace", "/dashboard", id="relative-without-slash"),
    ],
)
async def test_the_return_lands_under_the_apps_origin(
    client: AsyncClient, next_path: str | None, landing: str
) -> None:
    params = {} if next_path is None else {"next": next_path}
    resp = await client.get(RETURN, params=params, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == _app(landing)
    assert resp.headers["cache-control"] == "no-store"


async def test_the_return_needs_no_session(client: AsyncClient) -> None:
    """The gate's cookie is the only credential a browser carries on this hop;
    the app's session may not exist yet."""
    client.cookies.clear()
    resp = await client.get(RETURN, params={"next": "/login"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == _app("/login")


async def test_an_overlong_next_is_refused_not_truncated(client: AsyncClient) -> None:
    resp = await client.get(RETURN, params={"next": "/" + "a" * 2048}, follow_redirects=False)
    assert resp.status_code == 422
