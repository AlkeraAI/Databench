"""CORS contract: credentials are allowed and only configured origins are echoed.

This is what lets the SPA on app.example.com authenticate against api.example.com
cross-origin (same-site) in production — the browser only sends + exposes the
session cookie when the API returns Access-Control-Allow-Credentials for the
SPA's exact origin. `API_CORS_ORIGINS` must list that origin (never `*`, which
is incompatible with credentials).
"""

from __future__ import annotations

import pytest
from alkera_core.config import settings
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_cors_allows_configured_origin_with_credentials(client: AsyncClient) -> None:
    origin = settings.cors_origins_list[0]
    resp = await client.get("/health/live", headers={"Origin": origin})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin
    assert resp.headers.get("access-control-allow-credentials") == "true"


@pytest.mark.asyncio
async def test_cors_does_not_echo_unlisted_origin(client: AsyncClient) -> None:
    resp = await client.get("/health/live", headers={"Origin": "https://evil.not-allowed.example"})
    # The server still answers, but without an allow-origin header the browser
    # blocks the response — so a random site can't read authenticated responses.
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers
