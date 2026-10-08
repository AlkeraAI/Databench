"""The observability wiring is active on the real backend app: every response
carries an X-Trace-Id, errors render the canonical envelope, and an incoming
X-Request-Id is echoed as the trace id."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_unknown_route_returns_envelope_with_trace_header(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["trace_id"] == resp.headers["x-trace-id"]


async def test_validation_error_renders_envelope(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/login", json={"email": "not-an-email"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert "errors" in body["error"]["details"]
    # The submitted (bad) value must not be echoed back.
    assert "not-an-email" not in resp.text


async def test_incoming_request_id_is_echoed_as_trace_id(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/does-not-exist", headers={"X-Request-Id": "rid-abc-123"})
    assert resp.headers["x-trace-id"] == "rid-abc-123"
    assert resp.json()["error"]["trace_id"] == "rid-abc-123"


async def test_successful_response_carries_trace_header(client: AsyncClient) -> None:
    resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.headers["x-trace-id"]
