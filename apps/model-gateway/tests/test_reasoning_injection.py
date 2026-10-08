"""Catalog-driven reasoning injection: the gateway engages reasoning per the
model's `thinking_mode` + `supports_thinking` (resolved from the catalog, NEVER
by string-sniffing the model id). Verified-live behavior (see the reasoning-effort
provider matrix) encoded as deterministic assertions on the upstream body/headers:

- thinking_mode="adaptive"   (Claude 4.6+): effort + thinking:{type:adaptive}
- thinking_mode="effort_beta" (Opus 4.5):   effort + the effort-2025-11-24 beta
                                            (header on direct / body on Bedrock)
- OpenAI (Responses wire): effort → reasoning.effort; store=false (ZDR) +
                           include:["reasoning.encrypted_content"] always enforced;
                           body otherwise passed through (no stripping)
"""

from __future__ import annotations

import json

import httpx
import pytest
from alkera_core.gateway import (
    THINKING_DISPLAY_HEADER,
    join_model_effort,
    join_model_variant,
)
from alkera_core.llm_provider import Provider
from model_gateway.adapters import AnthropicMessagesCodec, _request_max_tokens


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _post_anthropic(client, token: str, model: str) -> httpx.Response:
    body = {"model": model, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
    return await client.post("/anthropic/v1/messages", headers=_auth(token), json=body)


async def _post_openai(client, token: str, model: str, **extra) -> httpx.Response:
    body = {"model": model, "input": [{"role": "user", "content": "hi"}], **extra}
    return await client.post("/openai/v1/responses", headers=_auth(token), json=body)


# --------------------------------------------------------------------------- #
# Anthropic-direct wire
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_adaptive_injects_thinking_no_beta(gateway_client, upstream, seed, make_response):
    """Claude 4.6+ (thinking_mode='adaptive'): effort ALONE is a no-op upstream, so
    the gateway also injects thinking:{type:adaptive}. No beta header."""
    s = await seed(
        reasoning_efforts=["low", "high"], thinking_mode="adaptive", supports_thinking=True
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    resp = await _post_anthropic(gateway_client, s.token, join_model_effort(s.model_id, "high"))
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert sent["output_config"] == {"effort": "high"}
    assert sent["thinking"] == {"type": "adaptive"}
    assert "anthropic-beta" not in upstream.requests[0].headers


@pytest.mark.asyncio
async def test_effort_beta_sends_header_no_thinking(gateway_client, upstream, seed, make_response):
    """Opus 4.5 (thinking_mode='effort_beta'): effort works standalone but needs the
    beta flag — on the direct wire it rides in the anthropic-beta HEADER. No thinking."""
    s = await seed(
        reasoning_efforts=["low", "high"], thinking_mode="effort_beta", supports_thinking=True
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    resp = await _post_anthropic(gateway_client, s.token, join_model_effort(s.model_id, "high"))
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert sent["output_config"] == {"effort": "high"}
    assert "thinking" not in sent
    assert upstream.requests[0].headers.get("anthropic-beta") == "effort-2025-11-24"


@pytest.mark.asyncio
async def test_no_effort_no_thinking_no_beta(gateway_client, upstream, seed, make_response):
    """A reasoning model called WITHOUT an effort variant injects nothing — the base
    turn must not carry output_config / thinking / beta."""
    s = await seed(
        reasoning_efforts=["low", "high"], thinking_mode="adaptive", supports_thinking=True
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    resp = await _post_anthropic(gateway_client, s.token, s.model_id)  # no ::effort
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert "output_config" not in sent
    assert "thinking" not in sent
    assert "anthropic-beta" not in upstream.requests[0].headers


# --------------------------------------------------------------------------- #
# Show-thoughts → thinking.display:"summarized" (adaptive only; opt-in per turn)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_adaptive_display_summarized_via_header(
    gateway_client, upstream, seed, make_response
):
    """opencode's wire: the per-turn 'show thoughts' signal arrives as the
    x-alkera-thinking-display header; the gateway adds display:"summarized" so the
    adaptive thinking text is surfaced (it is OMITTED by default on Claude 4.6+)."""
    s = await seed(
        reasoning_efforts=["low", "high"], thinking_mode="adaptive", supports_thinking=True
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    body = {
        "model": join_model_effort(s.model_id, "high"),
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }
    resp = await gateway_client.post(
        "/anthropic/v1/messages",
        headers={**_auth(s.token), THINKING_DISPLAY_HEADER: "summarized"},
        json=body,
    )
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert sent["thinking"] == {"type": "adaptive", "display": "summarized"}


@pytest.mark.asyncio
async def test_adaptive_display_summarized_via_model_field(
    gateway_client, upstream, seed, make_response
):
    """claude's wire (the SDK has no per-turn header): display rides the model field's
    3rd component (`<id>::<effort>::summarized`). Same upstream result."""
    s = await seed(
        reasoning_efforts=["low", "high"], thinking_mode="adaptive", supports_thinking=True
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    model = join_model_variant(s.model_id, "high", "summarized")
    resp = await _post_anthropic(gateway_client, s.token, model)
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    # Routing still resolved on the base id; effort + display both injected.
    assert sent["output_config"] == {"effort": "high"}
    assert sent["thinking"] == {"type": "adaptive", "display": "summarized"}


@pytest.mark.asyncio
async def test_adaptive_display_summarized_with_no_effort(
    gateway_client, upstream, seed, make_response
):
    """Show-thoughts ON but NO reasoning effort chosen (bare `<id>` model field):
    an adaptive model STILL gets thinking:{type:"adaptive", display:"summarized"} so
    the thinking text surfaces. The display request must not be silently dropped just
    because the turn carried no `::effort` — that's the "thoughts never appear" bug.
    No `output_config.effort` is sent (the model picks its own adaptive budget)."""
    s = await seed(
        reasoning_efforts=["low", "high"], thinking_mode="adaptive", supports_thinking=True
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    body = {
        "model": s.model_id,  # bare id — no ::effort
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }
    resp = await gateway_client.post(
        "/anthropic/v1/messages",
        headers={**_auth(s.token), THINKING_DISPLAY_HEADER: "summarized"},
        json=body,
    )
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert sent["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "output_config" not in sent


@pytest.mark.asyncio
async def test_no_thinking_when_no_effort_and_thoughts_off(
    gateway_client, upstream, seed, make_response
):
    """The asymmetric guard: with NO effort AND show-thoughts OFF (no display), an
    adaptive model gets a clean body — no `thinking`, no `output_config`. Engaging
    adaptive thinking is opt-in via effort OR display; neither here means neither."""
    s = await seed(
        reasoning_efforts=["low", "high"], thinking_mode="adaptive", supports_thinking=True
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    body = {
        "model": s.model_id,  # bare id — no ::effort
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }
    resp = await gateway_client.post("/anthropic/v1/messages", headers=_auth(s.token), json=body)
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert "thinking" not in sent
    assert "output_config" not in sent


@pytest.mark.asyncio
async def test_display_ignored_for_non_adaptive_model(
    gateway_client, upstream, seed, make_response
):
    """display:"summarized" is an ADAPTIVE-only knob: an effort_beta (Opus 4.5) model
    gets its normal effort path with NO thinking block — the display signal can't
    conjure a thinking object that would 400 upstream."""
    s = await seed(
        reasoning_efforts=["low", "high"], thinking_mode="effort_beta", supports_thinking=True
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    body = {
        "model": join_model_effort(s.model_id, "high"),
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    }
    resp = await gateway_client.post(
        "/anthropic/v1/messages",
        headers={**_auth(s.token), THINKING_DISPLAY_HEADER: "summarized"},
        json=body,
    )
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert "thinking" not in sent
    assert sent["output_config"] == {"effort": "high"}


# --------------------------------------------------------------------------- #
# Bedrock wire (effort beta rides in the BODY — no header surface)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bedrock_adaptive_injects_thinking(
    gateway_client, seed, install_bedrock, bedrock_client_cls, make_bedrock_events
):
    s = await seed(
        provider=Provider.BEDROCK,
        upstream_model_id="us.anthropic.claude-sonnet-4-6",
        reasoning_efforts=["low", "high"],
        thinking_mode="adaptive",
        supports_thinking=True,
    )
    client = bedrock_client_cls(make_bedrock_events(input_tokens=10, output_tokens=5))
    install_bedrock(client)
    resp = await _post_anthropic(gateway_client, s.token, join_model_effort(s.model_id, "high"))
    assert resp.status_code == 200
    body = json.loads(client.calls[0]["body"])
    assert body["output_config"] == {"effort": "high"}
    assert body["thinking"] == {"type": "adaptive"}
    assert "anthropic_beta" not in body


@pytest.mark.asyncio
async def test_bedrock_effort_beta_in_body(
    gateway_client, seed, install_bedrock, bedrock_client_cls, make_bedrock_events
):
    """Opus 4.5 on Bedrock: the effort beta flag MUST be a body field
    (`anthropic_beta`) — invoke_model has no header surface, and without it Bedrock
    400s 'output_config.effort not permitted' (verified live)."""
    s = await seed(
        provider=Provider.BEDROCK,
        upstream_model_id="us.anthropic.claude-opus-4-5-20251101-v1:0",
        reasoning_efforts=["low", "high"],
        thinking_mode="effort_beta",
        supports_thinking=True,
    )
    client = bedrock_client_cls(make_bedrock_events(input_tokens=10, output_tokens=5))
    install_bedrock(client)
    resp = await _post_anthropic(gateway_client, s.token, join_model_effort(s.model_id, "high"))
    assert resp.status_code == 200
    body = json.loads(client.calls[0]["body"])
    assert body["output_config"] == {"effort": "high"}
    assert body["anthropic_beta"] == ["effort-2025-11-24"]
    assert "thinking" not in body
    # The model id reached Bedrock as the full date-suffixed inference profile.
    assert client.calls[0]["modelId"] == "us.anthropic.claude-opus-4-5-20251101-v1:0"


@pytest.mark.asyncio
async def test_bedrock_non_reasoning_injects_nothing(
    gateway_client, seed, install_bedrock, bedrock_client_cls, make_bedrock_events
):
    """A non-reasoning Bedrock model (no efforts, no thinking_mode) gets a clean
    body — no output_config / thinking / anthropic_beta."""
    s = await seed(
        provider=Provider.BEDROCK,
        upstream_model_id="anthropic.claude-haiku-4-5",
        reasoning_efforts=[],
        thinking_mode=None,
        supports_thinking=False,
    )
    client = bedrock_client_cls(make_bedrock_events(input_tokens=10, output_tokens=5))
    install_bedrock(client)
    resp = await _post_anthropic(gateway_client, s.token, s.model_id)
    assert resp.status_code == 200
    body = json.loads(client.calls[0]["body"])
    assert "output_config" not in body
    assert "thinking" not in body
    assert "anthropic_beta" not in body
    assert body["anthropic_version"] == "bedrock-2023-05-31"


# --------------------------------------------------------------------------- #
# OpenAI Responses wire: effort → reasoning.effort, ZDR invariants, pass-through
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_openai_responses_injects_effort_store_and_include(
    gateway_client, upstream, seed, make_openai_response
):
    """The Responses transport injects the catalog `::effort` into `reasoning.effort`,
    forces `store=false` (ZDR), and ensures the encrypted-reasoning include (the
    stateless reasoning-continuity anchor)."""
    s = await seed(
        provider=Provider.OPENAI,
        supports_thinking=True,
        cache_prices=False,
        reasoning_efforts=["low", "high"],
        default_effort="low",
    )
    upstream.script(make_openai_response(prompt_tokens=10, completion_tokens=5))
    resp = await _post_openai(gateway_client, s.token, join_model_effort(s.model_id, "high"))
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert sent["reasoning"]["effort"] == "high"
    assert sent["store"] is False
    assert "reasoning.encrypted_content" in sent["include"]


@pytest.mark.asyncio
async def test_openai_responses_passes_body_through_no_strip(
    gateway_client, upstream, seed, make_openai_response
):
    """Faithful proxy: the caller's Responses fields (e.g. reasoning.summary, a
    pre-set include) survive — the gateway strips NOTHING (unlike the retired
    chat-completions path), it only enforces its invariants."""
    s = await seed(provider=Provider.OPENAI, supports_thinking=True, cache_prices=False)
    upstream.script(make_openai_response(prompt_tokens=10, completion_tokens=5))
    resp = await _post_openai(
        gateway_client,
        s.token,
        s.model_id,
        reasoning={"summary": "auto"},
        include=["reasoning.encrypted_content"],
    )
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert sent["reasoning"]["summary"] == "auto"  # preserved, not stripped
    assert "reasoning.encrypted_content" in sent["include"]


@pytest.mark.asyncio
async def test_openai_responses_base_call_still_zdr(
    gateway_client, upstream, seed, make_openai_response
):
    """A base call (no `::effort`) injects no `reasoning.effort`, but `store=false`
    and the encrypted-reasoning include are ALWAYS enforced."""
    s = await seed(
        provider=Provider.OPENAI,
        supports_thinking=True,
        cache_prices=False,
        reasoning_efforts=["low", "high"],
    )
    upstream.script(make_openai_response(prompt_tokens=10, completion_tokens=5))
    resp = await _post_openai(gateway_client, s.token, s.model_id)  # no ::effort
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert "effort" not in sent.get("reasoning", {})
    assert sent["store"] is False
    assert "reasoning.encrypted_content" in sent["include"]


# --------------------------------------------------------------------------- #
# Haiku-style enabled thinking (ANTHROPIC_HAIKU_STYLE_THINKING flag)
# --------------------------------------------------------------------------- #

_HAIKU_FLAGS = ["anthropic_haiku_style_thinking"]
_HAIKU_EFFORTS = ["none", "low", "medium", "high"]


async def _post_anthropic_mt(client, token: str, model: str, max_tokens: int) -> httpx.Response:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": "hi"}],
    }
    return await client.post("/anthropic/v1/messages", headers=_auth(token), json=body)


@pytest.mark.asyncio
@pytest.mark.parametrize(("effort", "budget"), [("low", 1024), ("medium", 4096), ("high", 16384)])
async def test_haiku_maps_effort_to_enabled_budget(
    gateway_client, upstream, seed, make_response, effort, budget
):
    """The ANTHROPIC_HAIKU_STYLE_THINKING flag turns the chosen effort into
    thinking:{type:"enabled", budget_tokens:N} — never output_config.effort (Haiku
    400s on it) and never a beta header. max_tokens=24000 leaves room for the budget."""
    s = await seed(
        reasoning_efforts=_HAIKU_EFFORTS,
        thinking_mode=None,
        supports_thinking=True,
        flags=_HAIKU_FLAGS,
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    model = join_model_effort(s.model_id, effort)
    resp = await _post_anthropic_mt(gateway_client, s.token, model, 24000)
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert sent["thinking"] == {"type": "enabled", "budget_tokens": budget}
    assert "output_config" not in sent
    assert "anthropic-beta" not in upstream.requests[0].headers


@pytest.mark.asyncio
async def test_haiku_none_sends_clean_body(gateway_client, upstream, seed, make_response):
    """The `none` effort sends Haiku NO thinking parameters at all."""
    s = await seed(
        reasoning_efforts=_HAIKU_EFFORTS,
        thinking_mode=None,
        supports_thinking=True,
        flags=_HAIKU_FLAGS,
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    model = join_model_effort(s.model_id, "none")
    resp = await _post_anthropic_mt(gateway_client, s.token, model, 24000)
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert "thinking" not in sent
    assert "output_config" not in sent


@pytest.mark.asyncio
async def test_haiku_clamps_budget_below_max_tokens(gateway_client, upstream, seed, make_response):
    """budget_tokens must stay < max_tokens — a small max_tokens clamps the budget."""
    s = await seed(
        reasoning_efforts=_HAIKU_EFFORTS,
        thinking_mode=None,
        supports_thinking=True,
        flags=_HAIKU_FLAGS,
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    # high → 16384, but max_tokens=2000 clamps the budget to 1999 (< max_tokens).
    model = join_model_effort(s.model_id, "high")
    resp = await _post_anthropic_mt(gateway_client, s.token, model, 2000)
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert sent["thinking"] == {"type": "enabled", "budget_tokens": 1999}


@pytest.mark.asyncio
async def test_haiku_skips_thinking_when_max_tokens_too_small(
    gateway_client, upstream, seed, make_response
):
    """If max_tokens can't fit the 1024 minimum budget, send a clean body."""
    s = await seed(
        reasoning_efforts=_HAIKU_EFFORTS,
        thinking_mode=None,
        supports_thinking=True,
        flags=_HAIKU_FLAGS,
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    model = join_model_effort(s.model_id, "high")
    resp = await _post_anthropic_mt(gateway_client, s.token, model, 512)
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert "thinking" not in sent
    assert "output_config" not in sent


@pytest.mark.asyncio
async def test_haiku_flag_overrides_stray_effort_beta_mode(
    gateway_client, upstream, seed, make_response
):
    """A Haiku-flagged model takes the enabled-thinking path even if its thinking_mode
    is (mis)set to effort_beta: thinking:{type:"enabled"}, NO output_config.effort and
    — unlike a real Opus 4.5 — NO anthropic-beta header. The shared reasoning dispatch
    suppresses the beta flag for haiku on BOTH the direct and Bedrock wires, so a stray
    config can't make the two transports diverge."""
    s = await seed(
        reasoning_efforts=_HAIKU_EFFORTS,
        thinking_mode="effort_beta",
        supports_thinking=True,
        flags=_HAIKU_FLAGS,
    )
    upstream.script(make_response(input_tokens=10, output_tokens=5))
    model = join_model_effort(s.model_id, "medium")
    resp = await _post_anthropic_mt(gateway_client, s.token, model, 24000)
    assert resp.status_code == 200
    sent = json.loads(upstream.requests[0].content)
    assert sent["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert "output_config" not in sent
    assert "anthropic-beta" not in upstream.requests[0].headers


# --------------------------------------------------------------------------- #
# _request_max_tokens — single source for the codec estimate + the haiku clamp
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"max_tokens": 8000}, 8000),
        ({"max_tokens": 1}, 1),
        ({"max_tokens": 0}, 4096),  # non-positive → default
        ({"max_tokens": -5}, 4096),
        ({"max_tokens": "x"}, 4096),  # non-int → default
        ({"max_tokens": None}, 4096),
        ({}, 4096),  # absent → default
    ],
)
def test_request_max_tokens(body, expected):
    assert _request_max_tokens(body) == expected


def test_request_max_tokens_custom_default():
    assert _request_max_tokens({}, 9999) == 9999


def test_anthropic_codec_max_output_tokens_uses_shared_helper():
    """The codec's admission estimate and the haiku thinking-budget clamp both read
    max_tokens through _request_max_tokens — one rule, one place."""
    codec = AnthropicMessagesCodec()
    assert codec.max_output_tokens({"max_tokens": 7000}) == 7000
    assert codec.max_output_tokens({}) == codec.DEFAULT_MAX_OUTPUT
