"""Model-tier-aware subagent routing.

The pure selector + the caching resolver: a tier preference selects the lowest
model whose tier is >= the requested tier (so ``explore``/cheap → haiku,
``review``/standard → sonnet), falling back to the highest tier available below
when nothing meets the floor; a slug pins exactly; no preference / frontier
inherits the parent. Exhaustive over the tier matrix + the resolver's fetch
caching and graceful degradation.
"""

from __future__ import annotations

from collections.abc import Sequence

from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.harness.adapters.opencode_alkera import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
)
from alkera_cli.harness.subagent_routing import (
    make_tier_model_resolver,
    select_subagent_model,
)
from alkera_cli.plugins.plugin_base.surfaces import AgentDefinition


def _m(model_id: str, tier: str, *, wire: str = "anthropic") -> GatewayModel:
    return GatewayModel(id=model_id, display_name=model_id, wire=wire, tier=tier)  # type: ignore[arg-type]


_FRONTIER = _m("big-opus", "frontier")
_STANDARD = _m("mid-sonnet", "standard")
_CHEAP = _m("lil-haiku", "cheap")
_ALL = [_FRONTIER, _STANDARD, _CHEAP]

_EXPLORE = AgentDefinition(name="explore", mode="explore", model_tier="cheap")
_REVIEW = AgentDefinition(name="review", mode="explore", model_tier="standard")
_WORKER = AgentDefinition(name="worker", mode="worker")
_PARENT = {"provider_id": ANTHROPIC_PROVIDER_ID, "model_id": "big-opus"}


# --- select_subagent_model -------------------------------------------------


def test_explore_routes_to_cheapest_available() -> None:
    out = select_subagent_model(_EXPLORE, _PARENT, candidates=_ALL)
    assert out is not None
    assert out["model_id"] == "lil-haiku"  # the cheap tier wins


def test_routed_subagent_model_carries_token_limits() -> None:
    # A tier-routed subagent's model dict MUST carry the limits — otherwise on the
    # daemon single-model path the child's opencode config omits `limit` and that
    # subagent never auto-compacts.
    cheap = GatewayModel(
        id="lil-haiku",
        display_name="lil-haiku",
        wire="anthropic",
        tier="cheap",
        context_window=200_000,
        max_output_tokens=64_000,
    )
    out = select_subagent_model(_EXPLORE, _PARENT, candidates=[_FRONTIER, cheap])
    assert out is not None
    assert out["model_id"] == "lil-haiku"
    assert out["context_window"] == 200_000
    assert out["max_output_tokens"] == 64_000


def test_explore_falls_back_to_standard_when_no_cheap() -> None:
    out = select_subagent_model(_EXPLORE, _PARENT, candidates=[_FRONTIER, _STANDARD])
    assert out is not None
    assert out["model_id"] == "mid-sonnet"  # cheapest NON-frontier available


def test_explore_uses_frontier_when_it_is_the_only_model() -> None:
    # cheap floor admits everything; with only frontier present, it's the lowest
    # (only) tier >= cheap, so route there explicitly rather than guessing.
    out = select_subagent_model(_EXPLORE, _PARENT, candidates=[_FRONTIER])
    assert out is not None and out["model_id"] == "big-opus"


def test_review_routes_to_standard_tier() -> None:
    # THE regression guard: model_tier="standard" must pick sonnet, NOT the cheaper
    # haiku — the lowest tier whose rank is >= standard.
    out = select_subagent_model(_REVIEW, _PARENT, candidates=_ALL)
    assert out is not None and out["model_id"] == "mid-sonnet"


def test_review_escalates_up_when_no_standard() -> None:
    # No standard model present → the lowest tier >= standard is frontier.
    out = select_subagent_model(_REVIEW, _PARENT, candidates=[_FRONTIER, _CHEAP])
    assert out is not None and out["model_id"] == "big-opus"


def test_review_falls_back_to_cheap_when_nothing_meets_floor() -> None:
    # Only a cheap model exists (nothing >= standard) → highest tier available below.
    out = select_subagent_model(_REVIEW, _PARENT, candidates=[_CHEAP])
    assert out is not None and out["model_id"] == "lil-haiku"


def test_worker_with_no_preference_inherits_parent() -> None:
    assert select_subagent_model(_WORKER, _PARENT, candidates=_ALL) is None


def test_frontier_preference_inherits_parent() -> None:
    agent = AgentDefinition(name="deep", model_tier="frontier")
    assert select_subagent_model(agent, _PARENT, candidates=_ALL) is None


def test_model_slug_pins_exact_model() -> None:
    agent = AgentDefinition(name="pinned", model_slug="mid-sonnet")
    out = select_subagent_model(agent, _PARENT, candidates=_ALL)
    assert out is not None and out["model_id"] == "mid-sonnet"


def test_model_slug_absent_inherits_parent() -> None:
    agent = AgentDefinition(name="pinned", model_slug="does-not-exist")
    assert select_subagent_model(agent, _PARENT, candidates=_ALL) is None


def test_slug_beats_tier_preference() -> None:
    agent = AgentDefinition(name="x", model_slug="big-opus", model_tier="cheap")
    out = select_subagent_model(agent, _PARENT, candidates=_ALL)
    assert out is not None and out["model_id"] == "big-opus"  # slug wins


def test_tie_break_is_deterministic_by_id() -> None:
    a = _m("cheap-b", "cheap")
    b = _m("cheap-a", "cheap")
    out = select_subagent_model(_EXPLORE, _PARENT, candidates=[a, b])
    assert out is not None and out["model_id"] == "cheap-a"  # lexicographically first


def test_selected_dict_shape_and_provider_mapping() -> None:
    model = GatewayModel(
        id="lil", display_name="Lil", wire="openai", efforts=("low", "high"), tier="cheap"
    )
    out = select_subagent_model(_EXPLORE, _PARENT, candidates=[model])
    assert out == {
        "provider_id": OPENAI_PROVIDER_ID,
        "model_id": "lil",
        "display_name": "Lil",
        "efforts": ["low", "high"],
        "effort": "low",  # no default_effort → first variant
        "context_window": 0,  # unknown on this fixture → carried as 0 (no limit emitted)
        "max_output_tokens": 0,
    }


def test_no_candidates_inherits_parent() -> None:
    assert select_subagent_model(_EXPLORE, _PARENT, candidates=[]) is None


# --- make_tier_model_resolver ---------------------------------------------


async def test_resolver_skips_fetch_for_no_preference_agent() -> None:
    calls = {"n": 0}

    async def fetch() -> Sequence[GatewayModel]:
        calls["n"] += 1
        return _ALL

    resolver = make_tier_model_resolver(fetch)
    assert await resolver(_WORKER, _PARENT) is None
    assert calls["n"] == 0  # a no-preference agent never needs the catalog


async def test_resolver_fetches_once_and_caches() -> None:
    calls = {"n": 0}

    async def fetch() -> Sequence[GatewayModel]:
        calls["n"] += 1
        return _ALL

    resolver = make_tier_model_resolver(fetch)
    first = await resolver(_EXPLORE, _PARENT)
    second = await resolver(_EXPLORE, _PARENT)
    assert first is not None and first["model_id"] == "lil-haiku"
    assert second == first
    assert calls["n"] == 1  # cached after the first fetch


async def test_resolver_degrades_to_inherit_on_fetch_error() -> None:
    async def fetch() -> Sequence[GatewayModel]:
        raise RuntimeError("gateway down")

    resolver = make_tier_model_resolver(fetch)
    assert await resolver(_EXPLORE, _PARENT) is None  # never blocks the spawn


async def test_resolver_retries_after_transient_failure() -> None:
    # A11: a transient fetch failure must NOT be cached — the next spawn retries.
    calls = {"n": 0}

    async def fetch() -> Sequence[GatewayModel]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient blip")
        return _ALL

    resolver = make_tier_model_resolver(fetch)
    assert await resolver(_EXPLORE, _PARENT) is None  # first call fails → inherit
    out = await resolver(_EXPLORE, _PARENT)  # retries (failure wasn't cached)
    assert out is not None and out["model_id"] == "lil-haiku"
    assert calls["n"] == 2
