"""The model catalog's vocabulary and the token usage a model reports.

Open: the model gateway routes and counts tokens with these whether or not
anyone bills for them. ``Provider`` is in ``alkera_core.llm_provider``; the
catalog rows are ``alkera_core.models.model_catalog``. Billing, an extension,
prices a ``Usage`` and draws credit by ``CreditClass``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RateKind(StrEnum):
    """A billable token dimension. New exotic kinds are added here + mapped in
    the relevant provider codec — no schema migration needed (usage is a JSON
    map on `UsageRecord`)."""

    INPUT = "input"
    OUTPUT = "output"
    CACHE_WRITE_5M = "cache_write_5m"
    CACHE_WRITE_1H = "cache_write_1h"
    CACHE_READ = "cache_read"
    REASONING = "reasoning"
    TOOL_USE = "tool_use"
    AUDIO_INPUT = "audio_input"
    AUDIO_OUTPUT = "audio_output"


class CreditClass(StrEnum):
    """A typed credit bucket, burned in `CREDIT_CLASS_BURN_ORDER`.

    The order spends expiring money first and bought money last: the included
    monthly allowance and the admin cycle pool lapse at a boundary, so a draw that
    skips them wastes them; promotional credit usually carries its own expiry;
    admin permanent credit never lapses but is free to the customer, so it goes
    before anything they paid for; prepaid top-ups and postpaid on-demand usage
    are the customer's own money and are touched only when nothing else covers
    the request.

    ADMIN_CYCLE and ADMIN_PERMANENT are credit a platform admin grants to an
    invoiced-by-hand org. The cycle pool expires at the end of the org's billing
    cycle; the permanent pool never expires. Neither is ever metered for an
    invoice (the billing extension's postpaid meter skips both).
    """

    FREE_MONTHLY = "free_monthly"
    ADMIN_CYCLE = "admin_cycle"
    PROMOTIONAL = "promotional"
    ADMIN_PERMANENT = "admin_permanent"
    PREPAID = "prepaid"
    ON_DEMAND = "on_demand"


class ModelTier(StrEnum):
    """Capability/cost tier of a catalog model — drives subagent routing.

    A subagent spawned for cheap, parallelizable work (``explore``) routes to the
    cheapest non-FRONTIER model available; FRONTIER models are reserved for the
    main agent, which keeps fan-out work off the most expensive models.
    "subagent-eligible" ==
    ``tier != FRONTIER``."""

    FRONTIER = "frontier"
    STANDARD = "standard"
    CHEAP = "cheap"


class ModelFlag(StrEnum):
    """Capability/behavior flags for a catalog model — a set; a model may carry any
    combination. Interpreted by the gateway.

    ANTHROPIC_HAIKU_STYLE_THINKING: engage reasoning via
    ``thinking:{type:"enabled", budget_tokens:N}`` (Claude Haiku 4.5-style) instead
    of ``output_config.effort`` / adaptive thinking — both of which the model 400s
    on. The gateway maps the requested effort (low/medium/high) to a hardcoded
    budget; the ``none`` effort sends NO thinking parameters at all.

    KEEP THE VALUE LIST IN SYNC with ``MODEL_FLAGS`` in
    ``apps/web/src/pages/admin/AdminCatalogPage.tsx`` (the frontend hand-duplicates
    these values, like the page's other enum option arrays)."""

    ANTHROPIC_HAIKU_STYLE_THINKING = "anthropic_haiku_style_thinking"


@dataclass(frozen=True)
class Usage:
    """Normalized token usage. Each provider codec maps its native usage in."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    reasoning: int = 0
    tool_use: int = 0
    audio_input: int = 0
    audio_output: int = 0

    def items(self) -> list[tuple[RateKind, int]]:
        return [
            (RateKind.INPUT, self.input),
            (RateKind.OUTPUT, self.output),
            (RateKind.CACHE_READ, self.cache_read),
            (RateKind.CACHE_WRITE_5M, self.cache_write_5m),
            (RateKind.CACHE_WRITE_1H, self.cache_write_1h),
            (RateKind.REASONING, self.reasoning),
            (RateKind.TOOL_USE, self.tool_use),
            (RateKind.AUDIO_INPUT, self.audio_input),
            (RateKind.AUDIO_OUTPUT, self.audio_output),
        ]

    def as_token_map(self) -> dict[str, int]:
        """rate_kind value -> count, omitting zeros (stored on UsageRecord)."""
        return {kind.value: count for kind, count in self.items() if count}

    @property
    def total_input_context(self) -> int:
        """Tokens that count toward context size for tier selection."""
        return self.input + self.cache_read + self.cache_write_5m + self.cache_write_1h


__all__ = ["CreditClass", "ModelFlag", "ModelTier", "RateKind", "Usage"]
