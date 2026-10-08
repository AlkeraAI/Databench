"""A model the operator has disabled must not be servable by slug.

Model slugs are public vendor names, and a newly added model is created disabled
while its routes are created enabled — so filtering the catalog's ``enabled`` flag
in the ``/v1/models`` listing alone leaves every staged, deprecated or withdrawn
model reachable to anyone who types its id. A staged model also typically has
routes before it has sell prices, which makes it unpriced (and so unmetered)
inference on the deployment's own provider keys.
"""

from __future__ import annotations

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.model_catalog import Model
from httpx import AsyncClient


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _body(model_id: str) -> dict:
    return {"model": model_id, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}


async def _disable_model(model_id: str) -> None:
    async with AsyncSessionLocal() as db:
        model = await db.get(Model, model_id)
        assert model is not None
        model.enabled = False
        await db.commit()


@pytest.mark.asyncio
async def test_a_disabled_model_with_an_enabled_route_is_refused(
    gateway_client: AsyncClient, upstream, seed, make_response
) -> None:
    s = await seed(granted_nanos=10**12)
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    await _disable_model(s.model_id)

    resp = await gateway_client.post(
        "/anthropic/v1/messages", headers=_auth(s.token), json=_body(s.model_id)
    )

    assert resp.status_code == 404
    assert len(upstream.requests) == 0  # never reached the provider


@pytest.mark.asyncio
async def test_a_disabled_model_is_indistinguishable_from_an_unknown_one(
    gateway_client: AsyncClient, upstream, seed, make_response
) -> None:
    """Slugs are guessable, so a refusal that differs between "disabled" and
    "no such model" turns the gateway into an enumeration oracle for the
    operator's staged catalog."""
    s = await seed(granted_nanos=10**12)
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    await _disable_model(s.model_id)

    disabled = await gateway_client.post(
        "/anthropic/v1/messages", headers=_auth(s.token), json=_body(s.model_id)
    )
    unknown = await gateway_client.post(
        "/anthropic/v1/messages", headers=_auth(s.token), json=_body("no-such-model-at-all")
    )

    assert disabled.status_code == unknown.status_code == 404
    # Same shape, differing only in the slug the caller supplied.
    assert disabled.json()["error"]["message"].replace(s.model_id, "X") == unknown.json()["error"][
        "message"
    ].replace("no-such-model-at-all", "X")


@pytest.mark.asyncio
async def test_an_enabled_model_still_serves(
    gateway_client: AsyncClient, upstream, seed, make_response
) -> None:
    """The positive control: enforcing the model flag must not narrow the normal
    serving path."""
    s = await seed(granted_nanos=10**12)
    upstream.script(make_response(input_tokens=10, output_tokens=5))

    resp = await gateway_client.post(
        "/anthropic/v1/messages", headers=_auth(s.token), json=_body(s.model_id)
    )

    assert resp.status_code == 200
    assert len(upstream.requests) == 1


@pytest.mark.asyncio
async def test_the_openai_ingress_enforces_the_same_flag(
    gateway_client: AsyncClient, upstream, seed
) -> None:
    """Both ingresses resolve through the same routing helper, so neither may be
    the one that still serves a withdrawn model."""
    from alkera_core.llm_provider import Provider

    s = await seed(provider=Provider.OPENAI, granted_nanos=10**12)
    await _disable_model(s.model_id)

    resp = await gateway_client.post(
        "/openai/v1/responses",
        headers=_auth(s.token),
        json={
            "model": s.model_id,
            "max_output_tokens": 64,
            "input": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 404
    assert len(upstream.requests) == 0
