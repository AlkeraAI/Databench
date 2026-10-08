"""The opencode config builder that points opencode at the Alkera gateway."""

from __future__ import annotations

import json

import pytest
from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.harness.adapters.opencode_alkera import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    anthropic_base_url,
    build_alkera_opencode_config,
    build_manifest_model,
    openai_base_url,
    resolve_runtime_effort,
)

GW = "https://gw.example.com"
TOKEN = "jwt-abc123"


@pytest.mark.parametrize(
    ("selected", "catalog_default", "expected"),
    [
        pytest.param("none", "medium", "none", id="explicit-none-is-off"),
        pytest.param("low", "medium", "low", id="explicit-enabled-effort"),
        pytest.param(None, "medium", "medium", id="omitted-inherits-catalog-default"),
        pytest.param(None, None, None, id="omitted-without-default-uses-base"),
        pytest.param("bogus", "bogus", None, id="unsupported-values-use-base"),
    ],
)
def test_runtime_effort_resolution_matches_provider_selection(
    selected: str | None,
    catalog_default: str | None,
    expected: str | None,
) -> None:
    """The shared runtime/display resolver preserves provider effort semantics."""
    assert (
        resolve_runtime_effort(
            selected,
            ("none", "low", "medium", "high"),
            catalog_default,
        )
        == expected
    )


def _claude() -> GatewayModel:
    return GatewayModel(id="claude-sonnet-4.6", display_name="Claude Sonnet 4.6", wire="anthropic")


def _gpt() -> GatewayModel:
    return GatewayModel(id="gpt-5", display_name="GPT-5", wire="openai")


def test_base_urls_match_the_gateway_routes() -> None:
    # @ai-sdk/anthropic appends /messages; @ai-sdk/openai (Responses) appends
    # /responses — so these baseURLs land on the gateway's ingress paths.
    assert anthropic_base_url(GW) == "https://gw.example.com/anthropic/v1"
    assert openai_base_url(GW) == "https://gw.example.com/openai/v1"
    # Trailing slashes on the gateway URL are normalized.
    assert anthropic_base_url("https://gw.example.com/") == "https://gw.example.com/anthropic/v1"


def test_both_providers_emitted_with_correct_npm_and_auth() -> None:
    cfg = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[_claude(), _gpt()])
    providers = cfg["provider"]

    anthropic = providers[ANTHROPIC_PROVIDER_ID]
    assert anthropic["npm"] == "@ai-sdk/anthropic"
    assert anthropic["options"]["baseURL"] == "https://gw.example.com/anthropic/v1"
    assert anthropic["options"]["apiKey"] == TOKEN
    assert anthropic["models"] == {"claude-sonnet-4.6": {"name": "Claude Sonnet 4.6"}}

    openai = providers[OPENAI_PROVIDER_ID]
    # The Responses API: @ai-sdk/openai (NOT -compatible). Reasoning continuity +
    # codex parity with opencode-native; ZDR holds via the gateway forcing
    # store=false + stateless encrypted reasoning.
    assert openai["npm"] == "@ai-sdk/openai"
    assert openai["options"]["baseURL"] == "https://gw.example.com/openai/v1"
    assert openai["options"]["apiKey"] == TOKEN
    assert openai["models"] == {"gpt-5": {"name": "GPT-5"}}


def test_openai_provider_is_the_responses_api_variant() -> None:
    cfg = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[_gpt()])
    # The custom `alkera-openai` provider id + bare @ai-sdk/openai package is what
    # makes opencode fall through to the Responses API (it hardcodes sdk.responses()
    # only for the built-in `openai` id). The reduced-fidelity -compatible package
    # (chat-completions) must NEVER appear.
    npms = {p["npm"] for p in cfg["provider"].values()}
    assert "@ai-sdk/openai" in npms
    assert "@ai-sdk/openai-compatible" not in npms


def test_only_present_wire_families_get_a_provider() -> None:
    only_anthropic = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[_claude()])
    assert set(only_anthropic["provider"]) == {ANTHROPIC_PROVIDER_ID}

    only_openai = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[_gpt()])
    assert set(only_openai["provider"]) == {OPENAI_PROVIDER_ID}


def test_default_model_resolves_to_provider_prefixed_id() -> None:
    cfg = build_alkera_opencode_config(
        gateway_url=GW, token=TOKEN, models=[_claude(), _gpt()], default_model="gpt-5"
    )
    assert cfg["model"] == f"{OPENAI_PROVIDER_ID}/gpt-5"


def test_default_model_falls_back_to_first_model() -> None:
    cfg = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[_claude(), _gpt()])
    assert cfg["model"] == f"{ANTHROPIC_PROVIDER_ID}/claude-sonnet-4.6"


def test_unknown_default_model_falls_back_to_first() -> None:
    cfg = build_alkera_opencode_config(
        gateway_url=GW, token=TOKEN, models=[_claude()], default_model="no-such-model"
    )
    assert cfg["model"] == f"{ANTHROPIC_PROVIDER_ID}/claude-sonnet-4.6"


def test_no_models_is_rejected() -> None:
    # Building a config with zero models would let opencode silently fall back to
    # its own public hosted model — a gateway-isolation break. Fail loudly.
    with pytest.raises(ValueError, match="at least one model is required"):
        build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[])


def test_empty_token_is_rejected() -> None:
    with pytest.raises(ValueError, match="token is required"):
        build_alkera_opencode_config(gateway_url=GW, token="", models=[_claude()])


def _opus() -> GatewayModel:
    return GatewayModel(
        id="claude-opus-4.5",
        display_name="Claude Opus 4.5",
        wire="anthropic",
        efforts=("low", "medium", "high"),
        default_effort="medium",
    )


def test_effort_variants_become_model_entries() -> None:
    cfg = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[_opus()])
    models = cfg["provider"][ANTHROPIC_PROVIDER_ID]["models"]
    # Base entry + one per effort (so the harness can switch variant per prompt).
    assert "claude-opus-4.5" in models
    assert "claude-opus-4.5::low" in models
    assert "claude-opus-4.5::high" in models
    assert models["claude-opus-4.5::high"]["name"] == "Claude Opus 4.5 (high)"


def test_default_effort_baked_into_default_model() -> None:
    cfg = build_alkera_opencode_config(
        gateway_url=GW,
        token=TOKEN,
        models=[_opus()],
        default_model="claude-opus-4.5",
        default_effort="high",
    )
    assert cfg["model"] == f"{ANTHROPIC_PROVIDER_ID}/claude-opus-4.5::high"


def test_default_effort_falls_back_to_model_default() -> None:
    cfg = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[_opus()])
    # No effort passed → the model's own default ("medium").
    assert cfg["model"] == f"{ANTHROPIC_PROVIDER_ID}/claude-opus-4.5::medium"


def test_unsupported_default_effort_falls_back() -> None:
    cfg = build_alkera_opencode_config(
        gateway_url=GW, token=TOKEN, models=[_opus()], default_effort="ludicrous"
    )
    assert cfg["model"] == f"{ANTHROPIC_PROVIDER_ID}/claude-opus-4.5::medium"


def test_model_without_efforts_has_no_suffix() -> None:
    cfg = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[_gpt()])
    assert cfg["model"] == f"{OPENAI_PROVIDER_ID}/gpt-5"


def test_no_static_idempotency_header_is_set() -> None:
    # opencode headers are static; a fixed idempotency key would 409 every call
    # after the first. The config must carry no such header anywhere.
    cfg = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[_claude(), _gpt()])
    serialized = json.dumps(cfg)
    assert "idempotency" not in serialized.lower()
    for prov in cfg["provider"].values():
        assert "headers" not in prov["options"]


# --------------------------------------------------------------------------- #
# Per-model `limit` — arms opencode's native auto-compaction
# --------------------------------------------------------------------------- #


def _windowed() -> GatewayModel:
    return GatewayModel(
        id="claude-opus-4.8",
        display_name="Claude Opus 4.8",
        wire="anthropic",
        efforts=("low", "high"),
        default_effort="low",
        context_window=1_000_000,
        max_output_tokens=64_000,
    )


def test_limit_emitted_on_base_and_every_variant_when_window_known() -> None:
    cfg = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[_windowed()])
    models = cfg["provider"][ANTHROPIC_PROVIDER_ID]["models"]
    # `input` triggers compaction at ~90% of the window (headroom so opencode can
    # actually summarize the history); `context` is the real window; `output` the cap.
    expected = {"context": 1_000_000, "input": 900_000, "output": 64_000}
    # The base AND each effort variant must carry the limit (opencode keys overflow
    # detection off the SELECTED model entry — a variant without it would not compact).
    assert models["claude-opus-4.8"]["limit"] == expected
    assert models["claude-opus-4.8::low"]["limit"] == expected
    assert models["claude-opus-4.8::high"]["limit"] == expected


def test_limit_input_leaves_compaction_headroom_below_context() -> None:
    # The whole point of the fix: opencode must trigger compaction BELOW the hard
    # window so the conversation is still summarizable. input must sit meaningfully
    # under context (never == it, which is the failure mode that can't be compacted).
    cfg = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[_windowed()])
    limit = cfg["provider"][ANTHROPIC_PROVIDER_ID]["models"]["claude-opus-4.8"]["limit"]
    assert limit["input"] < limit["context"]
    # A real buffer below the wall — never == context (the un-compactable failure mode).
    assert limit["input"] <= int(limit["context"] * 0.95)


def test_no_limit_when_context_window_unknown() -> None:
    # A 0 window must emit NO `limit` (never `context: 0`, which opencode reads as
    # "overflow detection disabled" anyway — but emitting it would be a silent trap).
    cfg = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[_claude()])
    entry = cfg["provider"][ANTHROPIC_PROVIDER_ID]["models"]["claude-sonnet-4.6"]
    assert "limit" not in entry


def test_unknown_max_output_falls_back_to_default_not_zero() -> None:
    # A known context but unknown output still emits a limit (output defaulted) — the
    # threshold is usable ≈ context - output, so a 0 output would be wrong, not absent.
    m = GatewayModel(id="m", display_name="M", wire="anthropic", context_window=200_000)
    cfg = build_alkera_opencode_config(gateway_url=GW, token=TOKEN, models=[m])
    limit = cfg["provider"][ANTHROPIC_PROVIDER_ID]["models"]["m"]["limit"]
    assert limit["context"] == 200_000
    # `limit.output` is the per-step max_tokens opencode requests, so a model with an
    # unknown cap must ask for no more than opencode's own 32k ceiling.
    # same-author-ok: 32k is opencode's `OUTPUT_TOKEN_MAX`, not a value copied from us.
    assert limit["output"] == 32_000


def test_build_manifest_model_carries_limits() -> None:
    # The daemon single-model config builder reconstructs a GatewayModel from this
    # manifest dict, so the limits MUST round-trip through it.
    manifest = build_manifest_model(_windowed(), "high")
    assert manifest["context_window"] == 1_000_000
    assert manifest["max_output_tokens"] == 64_000
