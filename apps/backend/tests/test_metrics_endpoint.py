"""The Prometheus /metrics endpoint exposes the RED request metrics."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_metrics_endpoint_exposes_request_metrics(client: AsyncClient) -> None:
    await client.get("/health/live")  # generate at least one recorded request
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    # The RED surface + the component label, plus the default process collectors.
    assert "alkera_http_requests_total" in body
    assert "alkera_http_request_duration_seconds" in body
    assert 'component="backend"' in body
    # A platform-independent default collector (the process/RSS collector is
    # Linux-/proc-only, so it's present in the container but not on macOS).
    assert "python_info" in body


async def test_metrics_endpoint_is_not_in_the_openapi_schema(client: AsyncClient) -> None:
    # It's mounted as a plain Starlette route, so it must not leak into the API docs.
    schema = (await client.get("/openapi.json")).json()
    assert "/metrics" not in schema["paths"]
