"""The request-context middleware is wired on the gateway: every response
carries an X-Trace-Id, and an incoming X-Request-Id is echoed as the trace id."""

from __future__ import annotations

from httpx import AsyncClient


async def test_response_carries_trace_header(gateway_client: AsyncClient) -> None:
    resp = await gateway_client.get("/health/live")
    assert resp.status_code == 200
    assert resp.headers["x-trace-id"]


async def test_incoming_request_id_propagates(gateway_client: AsyncClient) -> None:
    resp = await gateway_client.get("/health/live", headers={"X-Request-Id": "rid-gw-1"})
    assert resp.headers["x-trace-id"] == "rid-gw-1"
