"""Backpressure: the gateway sheds with 429 past the concurrency cap and
never reaches the provider, but serves normally under the (high) default."""

from __future__ import annotations

import pytest
from alkera_core.config import settings


def _body(model_id: str) -> dict:
    return {"model": model_id, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.asyncio
async def test_sheds_with_429_at_capacity(
    gateway_client, upstream, seed, make_response, monkeypatch
) -> None:
    s = await seed(granted_nanos=10**12)
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    # A zero cap means the next request can't acquire a slot.
    monkeypatch.setattr(settings, "gateway_max_concurrent_streams", 0)

    resp = await gateway_client.post(
        "/anthropic/v1/messages",
        headers={"Authorization": f"Bearer {s.token}"},
        json=_body(s.model_id),
    )
    assert resp.status_code == 429
    assert len(upstream.requests) == 0  # shed before any DB work / provider call


@pytest.mark.asyncio
async def test_serves_under_capacity_and_releases_slot(
    gateway_client, upstream, seed, make_response, monkeypatch
) -> None:
    s = await seed(granted_nanos=10**12)
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    monkeypatch.setattr(settings, "gateway_max_concurrent_streams", 1)

    # Two sequential requests both succeed — the slot is released when each
    # stream finishes, so a cap of 1 doesn't wedge the gateway.
    for _ in range(2):
        resp = await gateway_client.post(
            "/anthropic/v1/messages",
            headers={"Authorization": f"Bearer {s.token}"},
            json=_body(s.model_id),
        )
        assert resp.status_code == 200
