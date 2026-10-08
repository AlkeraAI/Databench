"""Gateway auth accepts the `x-api-key` header, not just `Authorization: Bearer`.

opencode's `@ai-sdk/anthropic` wire authenticates with the native Anthropic
`x-api-key` header. The gateway must treat that header as the same Alkera JWT so
the anthropic/bedrock wire works through it. Bearer remains canonical and wins
when both are present.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient

_BODY = {"model": None, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}


async def _post(client: AsyncClient, headers: dict[str, str], model_id: str) -> httpx.Response:
    body = {**_BODY, "model": model_id}
    return await client.post("/anthropic/v1/messages", headers=headers, json=body)


@pytest.mark.asyncio
async def test_x_api_key_authenticates(gateway_client, upstream, seed, make_response) -> None:
    s = await seed(granted_nanos=10**12)
    upstream.script(make_response(input_tokens=10, output_tokens=5))

    resp = await _post(gateway_client, {"x-api-key": s.token}, s.model_id)

    assert resp.status_code == 200
    assert b"text_delta" in resp.content
    assert len(upstream.requests) == 1


@pytest.mark.asyncio
async def test_bad_x_api_key_is_rejected(gateway_client, upstream, seed) -> None:
    s = await seed()
    resp = await _post(gateway_client, {"x-api-key": "not-a-jwt"}, s.model_id)
    assert resp.status_code == 401
    assert len(upstream.requests) == 0


@pytest.mark.asyncio
async def test_bearer_wins_when_both_present(gateway_client, upstream, seed, make_response) -> None:
    s = await seed(granted_nanos=10**12)
    upstream.script(make_response(input_tokens=10, output_tokens=5))

    # Valid bearer + a garbage x-api-key: bearer is canonical and must win.
    resp = await _post(
        gateway_client,
        {"Authorization": f"Bearer {s.token}", "x-api-key": "garbage"},
        s.model_id,
    )
    assert resp.status_code == 200
    assert len(upstream.requests) == 1
