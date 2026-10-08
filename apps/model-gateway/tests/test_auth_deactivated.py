"""Deprovisioning at the gateway: a deactivated account cannot spend.

The backend API refuses `is_active = False` on every request. The gateway — the
surface where the org's money is actually spent — must own the same durable
check rather than inheriting it from the token revocation that deactivation
happens to trigger today: a deactivation path that skips the revoke, or a token
minted after the deactivation, would otherwise keep drawing the org's credits
until the 90-day CLI token expires.

The tests deactivate WITHOUT revoking (and re-mint a fresh, post-deactivation
token) precisely so the two checks are pinned independently.
"""

from __future__ import annotations

import httpx
import pytest
from alkera_core.auth import encode_cli_token, register_token
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import TokenType, User
from httpx import AsyncClient


async def _deactivate(user_id: object) -> None:
    """Set `is_active = False` and NOTHING else — no `revoke_all_for_user`."""
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        user.is_active = False
        await db.commit()


async def _fresh_token(user_id: object) -> str:
    """Mint a brand-new CLI token, as the device-grant endpoint would. Its `iat`
    post-dates any `token_epoch` bump, so revocation cannot be what stops it."""
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        assert user is not None
        token, claims = encode_cli_token(
            user_id=user.id, email=user.email, org_team_id=user.home_org_team_id, platform_role=None
        )
        await register_token(db, claims=claims, token_type=TokenType.CLI)
        await db.commit()
    return token


async def _messages(client: AsyncClient, token: str, model_id: str) -> httpx.Response:
    body = {"model": model_id, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
    return await client.post(
        "/anthropic/v1/messages", headers={"Authorization": f"Bearer {token}"}, json=body
    )


async def _responses(client: AsyncClient, token: str, model_id: str) -> httpx.Response:
    body = {
        "model": model_id,
        "max_output_tokens": 64,
        "input": [{"role": "user", "content": "hi"}],
    }
    return await client.post(
        "/openai/v1/responses", headers={"Authorization": f"Bearer {token}"}, json=body
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post",
    [pytest.param(_messages, id="anthropic"), pytest.param(_responses, id="openai")],
)
async def test_deactivated_user_is_401_on_every_ingress(
    gateway_client, upstream, seed, make_response, post
) -> None:
    s = await seed(granted_nanos=10**12)
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    await _deactivate(s.user_id)

    resp = await post(gateway_client, s.token, s.model_id)

    assert resp.status_code == 401
    assert len(upstream.requests) == 0  # refused before any provider spend


@pytest.mark.asyncio
async def test_deactivated_user_is_401_on_a_freshly_minted_token(
    gateway_client, upstream, seed
) -> None:
    """The token-revocation side effect cannot cover this: the token is minted
    AFTER the deactivation, so its `iat` post-dates the epoch and its jti is
    unrevoked. Only the durable `is_active` check refuses it."""
    s = await seed(granted_nanos=10**12)
    await _deactivate(s.user_id)
    token = await _fresh_token(s.user_id)

    resp = await _messages(gateway_client, token, s.model_id)

    assert resp.status_code == 401
    assert len(upstream.requests) == 0


@pytest.mark.asyncio
async def test_deactivated_user_cannot_list_models(gateway_client, seed) -> None:
    s = await seed()
    await _deactivate(s.user_id)

    resp = await gateway_client.get("/v1/models", headers={"Authorization": f"Bearer {s.token}"})

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_active_user_is_unaffected(gateway_client, upstream, seed, make_response) -> None:
    """The asymmetric case: the new gate must not refuse a normal account."""
    s = await seed(granted_nanos=10**12)
    upstream.script(make_response(input_tokens=10, output_tokens=5))

    resp = await _messages(gateway_client, s.token, s.model_id)

    assert resp.status_code == 200
    assert len(upstream.requests) == 1
