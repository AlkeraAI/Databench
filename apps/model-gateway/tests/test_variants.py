"""Reasoning-effort variants: the gateway parses `<model_id>::<effort>`, resolves
routing on the base model, validates the effort, and injects the provider's
native knob into the upstream body."""

from __future__ import annotations

import json

import httpx
import pytest
from alkera_core.gateway import (
    join_model_effort,
    join_model_variant,
    split_model_effort,
    split_model_variant,
)
from model_gateway.adapters import _apply_anthropic_effort


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_split_and_join_round_trip() -> None:
    assert split_model_effort("claude-opus-4.5::high") == ("claude-opus-4.5", "high")
    assert split_model_effort("gpt-5") == ("gpt-5", None)
    assert split_model_effort("m::") == ("m", None)  # empty effort collapses
    assert join_model_effort("gpt-5", "minimal") == "gpt-5::minimal"
    assert join_model_effort("gpt-5", None) == "gpt-5"


def test_split_model_variant_parses_three_components() -> None:
    assert split_model_variant("claude-opus-4.8::high::summarized") == (
        "claude-opus-4.8",
        "high",
        "summarized",
    )
    # Effort but no display (opencode leaves display to the header).
    assert split_model_variant("claude-opus-4.8::high") == ("claude-opus-4.8", "high", None)
    # Bare model.
    assert split_model_variant("gpt-5") == ("gpt-5", None, None)
    # Whitespace/empty trailing components collapse to None.
    assert split_model_variant("m::high::") == ("m", "high", None)
    assert split_model_variant("m::::summarized") == ("m", None, "summarized")
    assert split_model_variant("m::") == ("m", None, None)


def test_join_model_variant_round_trips_and_omits_empties() -> None:
    assert join_model_variant("claude-opus-4.8", "high", "summarized") == (
        "claude-opus-4.8::high::summarized"
    )
    # No display → identical to the 2-component effort encoding (back-compat).
    assert join_model_variant("claude-opus-4.8", "high", None) == "claude-opus-4.8::high"
    assert join_model_variant("gpt-5", None, None) == "gpt-5"
    # Display with no effort still round-trips (empty middle component).
    assert split_model_variant(join_model_variant("m", None, "summarized")) == (
        "m",
        None,
        "summarized",
    )


def test_apply_anthropic_effort_sets_output_config() -> None:
    payload: dict = {"model": "x"}
    _apply_anthropic_effort(payload, "high")
    assert payload["output_config"] == {"effort": "high"}
    # None is a no-op.
    payload2: dict = {"model": "x"}
    _apply_anthropic_effort(payload2, None)
    assert "output_config" not in payload2


# OpenAI effort/normalization is on the Responses wire now: effort rides in
# `reasoning.effort` (asserted in test_reasoning_injection.py via the gateway), and
# there is no body-stripping (the Responses transport is a faithful proxy).


# --------------------------------------------------------------------------- #
# End-to-end through the gateway (mocked upstream)
# --------------------------------------------------------------------------- #


async def _post(client, token: str, model: str) -> httpx.Response:
    body = {"model": model, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
    return await client.post("/anthropic/v1/messages", headers=_auth(token), json=body)


@pytest.mark.asyncio
async def test_valid_effort_injected_into_upstream_body(
    gateway_client, upstream, seed, make_response
) -> None:
    s = await seed(granted_nanos=10**12, reasoning_efforts=["low", "high"], default_effort="low")
    upstream.script(make_response(input_tokens=10, output_tokens=5))

    resp = await _post(gateway_client, s.token, join_model_effort(s.model_id, "high"))
    assert resp.status_code == 200
    assert len(upstream.requests) == 1
    sent = json.loads(upstream.requests[0].content)
    # Routing resolved on the base id; the effort rode through as output_config.
    assert sent["model"] != s.model_id  # transport rewrote to the upstream id
    assert sent["output_config"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_unsupported_effort_is_rejected(gateway_client, upstream, seed) -> None:
    s = await seed(reasoning_efforts=["low", "high"])
    resp = await _post(gateway_client, s.token, join_model_effort(s.model_id, "ludicrous"))
    assert resp.status_code == 400
    assert len(upstream.requests) == 0  # never reached the provider


@pytest.mark.asyncio
async def test_base_model_without_effort_still_works(
    gateway_client, upstream, seed, make_response
) -> None:
    s = await seed(granted_nanos=10**12, reasoning_efforts=["low", "high"])
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    resp = await _post(gateway_client, s.token, s.model_id)  # no ::effort
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert "output_config" not in sent


@pytest.mark.asyncio
async def test_v1_models_exposes_efforts(gateway_client, upstream, seed) -> None:
    s = await seed(reasoning_efforts=["low", "medium", "high"], default_effort="medium")
    resp = await gateway_client.get("/v1/models", headers=_auth(s.token))
    assert resp.status_code == 200
    by_id = {m["id"]: m for m in resp.json()["data"]}
    assert by_id[s.model_id]["efforts"] == ["low", "medium", "high"]
    assert by_id[s.model_id]["default_effort"] == "medium"


@pytest.mark.asyncio
async def test_v1_models_exposes_reasoning_formats(gateway_client, upstream, seed) -> None:
    """What a chat may switch onto without losing its reasoning is catalog data
    the gateway serves; a row that names none serves ``None`` and ``[]``."""
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models.model_catalog import Model

    s = await seed(reasoning_efforts=["low"])
    async with AsyncSessionLocal() as db:
        row = await db.get(Model, s.model_id)
        assert row is not None
        row.reasoning_format = "anthropic:a"
        row.reads_reasoning_formats = ["anthropic:b"]
        await db.commit()
    plain = await seed()
    for seeded, fmt, reads in ((s, "anthropic:a", ["anthropic:b"]), (plain, None, [])):
        resp = await gateway_client.get("/v1/models", headers=_auth(seeded.token))
        row_out = {m["id"]: m for m in resp.json()["data"]}[seeded.model_id]
        assert (row_out["reasoning_format"], row_out["reads_reasoning_formats"]) == (fmt, reads)
