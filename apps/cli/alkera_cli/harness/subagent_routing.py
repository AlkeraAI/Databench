"""Model-tier-aware subagent routing, so a subagent runs on the cheapest fit.

A subagent should run on the model TIER its work needs, not the main agent's
frontier model by default: ``explore`` wants the cheap tier (fast, parallel
discovery), ``review`` wants the standard/sonnet tier (analytical verification).
This maps an :class:`AgentDefinition` + the gateway model catalog to the child
chat's model selection: an explicit ``model_slug`` pins that model; otherwise a
sub-frontier ``model_tier`` preference selects the LOWEST-tiered model whose tier
is >= the requested tier (falling back to the highest tier available BELOW it when
nothing meets the floor); no preference / a frontier preference inherits the
parent's model.

Pure selection here (``select_subagent_model``) + a thin async resolver factory
(``make_tier_model_resolver``) the CLI/daemon wire onto the runtime with a
cached gateway fetch. The seam keeps the runtime testable with a fake resolver.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from alkera_cli.contracts.gateway_model import GatewayModel
from alkera_cli.harness.adapters.opencode_alkera import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
)
from alkera_cli.plugins.plugin_base.surfaces import AgentDefinition

#: (AgentDefinition, parent manifest-model dict | None) -> child manifest-model
#: dict, or None to inherit the parent's model. Async so the resolver can fetch
#: the gateway catalog (cached) on first use.
SubagentModelResolver = Callable[
    [AgentDefinition, dict[str, Any] | None], Awaitable[dict[str, Any] | None]
]

#: Lower rank == cheaper. An unknown tier is treated as STANDARD (forward-compat).
_TIER_RANK: dict[str, int] = {"cheap": 0, "standard": 1, "frontier": 2}


def _rank(tier: str) -> int:
    return _TIER_RANK.get(tier.lower(), 1)


def _to_model_dict(model: GatewayModel) -> dict[str, Any]:
    """The manifest model dict for a gateway model (mirrors the chat picker's
    selection shape; default effort = the model's default or first variant)."""
    provider_id = ANTHROPIC_PROVIDER_ID if model.wire == "anthropic" else OPENAI_PROVIDER_ID
    if model.default_effort and model.default_effort in model.efforts:
        effort: str | None = model.default_effort
    else:
        effort = model.efforts[0] if model.efforts else None
    return {
        "provider_id": provider_id,
        "model_id": model.id,
        "display_name": model.display_name,
        "efforts": list(model.efforts),
        "effort": effort,
        # Carry the limits so a tier-routed subagent's child opencode config emits a
        # per-model `limit` and auto-compacts too (critical on the daemon single-model
        # path, which rebuilds the GatewayModel from this dict). 0 = unknown (omitted).
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
    }


def select_subagent_model(
    agent: AgentDefinition,
    parent_model: dict[str, Any] | None,
    *,
    candidates: Sequence[GatewayModel],
) -> dict[str, Any] | None:
    """Pick the child model for a spawned subagent, or ``None`` to inherit the
    parent's model.

    - ``model_slug`` set → pin that exact model (``None`` if it isn't in the
      catalog — caller inherits the parent rather than guessing).
    - no preference (``""``) or a ``frontier`` preference → ``None`` (inherit the
      parent's model — "use the top tier, same as the parent").
    - any other ``model_tier`` preference (``cheap`` / ``standard``; an unknown
      tier is treated as ``standard`` for forward-compat) → the LOWEST-tiered
      candidate whose tier rank is >= the requested rank (so ``standard`` picks
      sonnet, not haiku). If NO candidate meets that floor, fall back to the
      HIGHEST tier available below it (never inherit when a preference was set and
      any model exists). Ties within a tier break deterministically by id."""
    if agent.model_slug:
        match = next((m for m in candidates if m.id == agent.model_slug), None)
        return _to_model_dict(match) if match is not None else None

    tier = (agent.model_tier or "").lower()
    if tier in ("", "frontier"):
        return None  # no preference / top tier → inherit the parent's model
    if not candidates:
        return None

    requested = _rank(tier)
    at_or_above = [m for m in candidates if _rank(m.tier) >= requested]
    if at_or_above:
        # The lowest tier that still meets the floor (cheapest model >= requested).
        best = min(at_or_above, key=lambda m: (_rank(m.tier), m.id))
    else:
        # Nothing meets the floor → the highest tier available below it.
        top = max(_rank(m.tier) for m in candidates)
        best = min((m for m in candidates if _rank(m.tier) == top), key=lambda m: m.id)
    return _to_model_dict(best)


#: A catalog lister that presents a given credential, for a resolver that is
#: bound per chat.
CatalogFetch = Callable[[str], Awaitable[Sequence[GatewayModel]]]


class TierModelResolver:
    """The resolver the runtime calls per spawn: the catalog fetched once (on
    SUCCESS only) and :func:`select_subagent_model` over it.

    ``fetch`` is the lister this resolver reads with as it stands (already
    credential-bound). ``fetch_as`` is how it reads as some OTHER credential —
    a chat's own gateway token — which is what :meth:`bound_to` builds a
    resolver on, with a cache of its own: what a chat's token may list is
    that chat's catalog, never another's. A fetch failure degrades to
    inherit-parent for THIS spawn and is not cached, so a transient gateway
    blip never permanently disables cost routing.
    """

    def __init__(
        self,
        fetch: Callable[[], Awaitable[Sequence[GatewayModel]]],
        *,
        fetch_as: CatalogFetch | None = None,
    ) -> None:
        self._fetch = fetch
        self._fetch_as = fetch_as
        self._cache: dict[str, Sequence[GatewayModel]] = {}

    def bound_to(self, credential: str) -> TierModelResolver:
        """This resolver reading the catalog as ``credential``, or itself when
        its lister cannot present another credential (a test's fixed lister)."""
        fetch_as = self._fetch_as
        if fetch_as is None:
            return self

        async def _fetch() -> Sequence[GatewayModel]:
            return await fetch_as(credential)

        return TierModelResolver(_fetch, fetch_as=fetch_as)

    async def __call__(
        self, agent: AgentDefinition, parent_model: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        # A subagent with no cost preference never needs the catalog.
        if not agent.model_slug and (agent.model_tier or "").lower() in ("", "frontier"):
            return None
        if "models" not in self._cache:
            try:
                self._cache["models"] = list(await self._fetch())  # cache ONLY on success
            except Exception:
                return None  # transient failure → inherit parent, retry next time
        return select_subagent_model(agent, parent_model, candidates=self._cache["models"])


def make_tier_model_resolver(
    fetch: Callable[[], Awaitable[Sequence[GatewayModel]]],
    *,
    fetch_as: CatalogFetch | None = None,
) -> TierModelResolver:
    """Build a resolver that fetches the catalog once (cached for the process,
    on SUCCESS only) and applies :func:`select_subagent_model`. ``fetch`` is the
    gateway model lister (already auth-bound); ``fetch_as`` the lister as a
    credential the runtime binds per chat. See :class:`TierModelResolver`."""
    return TierModelResolver(fetch, fetch_as=fetch_as)


def _catalog_as() -> CatalogFetch:
    async def _fetch_as(credential: str) -> Sequence[GatewayModel]:
        from alkera_cli.gateway.client import fetch_models
        from alkera_cli.host.config import get_settings

        return await fetch_models(gateway_url=get_settings().alkera_gateway_url, token=credential)

    return _fetch_as


def default_subagent_model_resolver() -> SubagentModelResolver:
    """The production resolver shared by the CLI + daemon: routes cheap-tier
    subagents using the gateway's live model catalog (fetched once, cached).
    Every chat's spawns read the catalog as that chat's own credential (a local
    chat's bound profile, a cloud chat's gateway token); unbound it lists
    nothing, so a spawn inherits the parent's model rather than reading the
    catalog as whichever sign-in is current now."""

    async def _unbound() -> Sequence[GatewayModel]:
        return []

    return make_tier_model_resolver(_unbound, fetch_as=_catalog_as())


def machine_subagent_model_resolver() -> SubagentModelResolver:
    """The resolver a box runs: it reads the catalog as each chat's gateway
    token and as nothing else. Unbound it lists nothing — a spawn inherits the
    parent's model — and never reaches for a login on the box's disk."""

    async def _nothing() -> Sequence[GatewayModel]:
        return []

    return make_tier_model_resolver(_nothing, fetch_as=_catalog_as())


__all__ = [
    "CatalogFetch",
    "SubagentModelResolver",
    "TierModelResolver",
    "default_subagent_model_resolver",
    "machine_subagent_model_resolver",
    "make_tier_model_resolver",
    "select_subagent_model",
]
