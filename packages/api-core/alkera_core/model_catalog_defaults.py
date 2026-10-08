"""The model catalog an open deployment starts with: the current Anthropic and
OpenAI models, at the providers' list prices.

Each :class:`DefaultModel` is a model a person can pick, the provider route it is
reached through, and that provider's public list price for it (pass-through:
what the provider charges is what the model costs, no markup). Provider list
prices are public, so they ship here as data. :func:`load_default_catalog`
writes any default model a deployment lacks, with its route, into the catalog
tables (``alkera_core.models.model_catalog``) and leaves every model it already
has as it is, so it is safe on a live database and on every run of the seeds.
Which models a gateway offers is decided per request: a model is listed only
while its provider has a credential (see ``model_gateway.api``).

A distribution adds to this on :data:`CATALOG_LOADERS`: a step that runs after
the load, in the same session (the product's billing adds its older models and
writes its price rows there).

Upstream model ids are the providers' stable public slugs. The Bedrock route is
not in the default set; add a ``Provider.BEDROCK`` entry pointing at a
``global.anthropic.*`` cross-region inference profile if you need it, with the
same ``cache_min_tokens`` as its Anthropic-direct twin.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.extensions import ExtensionPoint
from alkera_core.llm_provider import Provider
from alkera_core.model_catalog import CreditClass, ModelFlag, ModelTier
from alkera_core.models.model_catalog import Model, ModelRoute


@dataclass(frozen=True)
class LongContextTier:
    """A second price row per rate kind, applying once a request's input context
    reaches ``min_tokens`` (the billing engine picks the largest tier not above
    the request's input + cache tokens). Input and cache reads reprice by
    ``input_multiplier``, output by ``output_multiplier`` — how the providers
    publish it (OpenAI's gpt-6 family: 2x input, 2x cached input, 1.5x output
    above 272K input tokens)."""

    min_tokens: int
    input_multiplier: str
    output_multiplier: str


#: OpenAI's long-context boundary: prices change for a prompt of MORE than 272K
#: input tokens, so a 272,000-token prompt is still short context.
OPENAI_LONG_CONTEXT = LongContextTier(
    min_tokens=272_001, input_multiplier="2", output_multiplier="1.5"
)


@dataclass(frozen=True)
class DefaultModel:
    slug: str
    display_name: str
    family: str
    provider: Provider
    upstream_model_id: str
    efforts: list[str]
    tier: ModelTier = ModelTier.STANDARD
    default_effort: str | None = None
    thinking_mode: str | None = None
    flags: list[str] = field(default_factory=list)
    """Capability/behavior flags (``ModelFlag`` values) interpreted by the gateway,
    e.g. ``ANTHROPIC_HAIKU_STYLE_THINKING`` → enabled-thinking with a budget."""
    supports_thinking: bool = True
    """Whether the model is a reasoning model (informational — surfaced in
    ``/v1/models``). Per-model because it's a provider capability fact, NOT
    derivable from ``efforts``: a model can support extended thinking yet expose
    no ``output_config.effort`` knob (``efforts=[]`` but this stays True —
    Haiku 4.5 is exactly this case). Default True (all seeded models reason);
    set False for a genuinely non-reasoning model (e.g. a future gpt-4o-class
    chat model)."""
    supports_caching: bool = True
    """Whether the model supports prompt caching at all. Drives which cache rate
    kinds get priced. Set False for a model the provider does
    not cache (e.g. the OpenAI Pro tier has no cached-input price) so we don't
    seed prices for a token kind the model never bills."""
    cache_min_tokens: int | None = None
    """The provider's MINIMUM CACHEABLE PREFIX in tokens, for a wire whose caching
    is opt-in. Anthropic ignores a `cache_control` breakpoint shorter than this
    and charges those tokens as ordinary input, and the figure is per model
    (512 / 1024 / 2048 / 4096) rather than per family — it does not track model
    size. The gateway reads it when a stream dies before the provider reports
    usage, to decide whether a caller's breakpoint took effect at all. Left None
    on OpenAI: that wire caches automatically above one published wire-wide
    floor, so there is no per-model minimum to record. A Bedrock spec carrying an
    Anthropic model takes the SAME figure as its Anthropic-direct twin — one
    codec, one estimate, one minimum."""
    region: str | None = None
    # COST = SELL (pass-through, zero margin) — set both to the provider's real
    # list price. Defaults match Claude Sonnet 4.6 ($3 in / $15 out).
    input_sell: str = "3.00"
    output_sell: str = "15.00"
    input_cost: str = "3.00"
    output_cost: str = "15.00"
    context_window: int = 200_000
    # Max output (completion) tokens — surfaced via /v1/models so the agent reserves
    # output headroom before compacting. Default 64_000 (the Claude 4.x family ceiling);
    # OpenAI gpt-5.x models override to 128_000.
    max_output_tokens: int = 64_000
    cache_read_multiplier: str = "0.1"
    """The cache-read price as a multiple of the input price. 0.1 is the standard
    multiple on both providers; a model the provider prices differently says so
    here (Claude Opus 5.5 reads at 0.05x, Claude Fable 5.1 at 0.025x, gpt-6.1-sol
    at 0.05x) rather than hard-coding a price that stops tracking its input."""
    cache_write_multiplier: str | None = None
    """An OpenAI model's cache-write price as a multiple of input, when the
    provider publishes one (GPT-6 / GPT-5.6: 1.25x). None = OpenAI bills no
    separate write (older models). Anthropic's per-TTL writes are not set here —
    every Anthropic model has them."""
    long_context: LongContextTier | None = None
    """The provider's long-prompt tier, when it has one. None = one price across
    the whole context window (every Claude 4.6+ model, the older gpt-5.x rows)."""


# Anthropic effort vocabularies — EXACT, from each model's /v1/models capability
# caps. Effort is GA on 4.6+ (no beta flag) and engages reasoning only paired
# with adaptive thinking, so every reasoning Claude is thinking_mode "adaptive".
#   Opus 4.8 / 4.7           → low/medium/high/xhigh/max
#   Opus 4.6 / Sonnet 4.6    → low/medium/high/max   (no `xhigh`)
#   Haiku 4.5                → none/low/medium/high — NOT native effort levels: the
#     ANTHROPIC_HAIKU_STYLE_THINKING flag makes the gateway map each to a thinking
#     budget_tokens (`none` → no thinking at all). See the Haiku spec below.
CLAUDE_XHIGH_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CLAUDE_46_EFFORTS = ["low", "medium", "high", "max"]
HAIKU_EFFORTS = ["none", "low", "medium", "high"]

# OpenAI effort vocabularies — the EXACT enum each model accepts (probed live;
# `none` is the non-reasoning setting). The Chat-served gpt-5.x models dropped
# `minimal`; the Responses-served models (Pro, Codex) keep it.
OPENAI_CHAT_EFFORTS = ["none", "low", "medium", "high", "xhigh"]
OPENAI_RESP_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"]
# GPT-6 Astra and GPT-6.1 Sol always reason: `none` is refused.
OPENAI_NO_NONE_EFFORTS = ["low", "medium", "high", "xhigh"]


#: Which other Claudes' signed thinking each Claude reads on replay, by upstream
#: id. ONLY relations a live probe proved (mint thinking on A, replay it to B under
#: the ``thinking-binding-controls-2026-08-01`` beta, no ``model_binding_mismatch``
#: reported), pinned by a live-provider probe test.
#: A model absent here reads only its own reasoning. Held back although the probe
#: passed: Opus 5.5 reading Sonnet 5.5, which Anthropic documents as unreadable.
#: Not seeded for want of proof: Haiku 4.5 reading Sonnet 5.5 (Sonnet 5.5 did not
#: think on the probe question). Migrations ``0181`` and ``0182`` write the same
#: table onto existing rows; keep them in step.
PROVEN_REASONING_READS: dict[str, tuple[str, ...]] = {
    "claude-fable-5-1": (
        "claude-haiku-4-5",
        "claude-fable-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ),
    "claude-opus-5-5": (
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ),
    "claude-sonnet-5-5": (
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ),
    "claude-fable-5": (
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ),
    "claude-opus-5": (
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ),
    "claude-sonnet-5": (
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
    ),
    "claude-opus-4-8": (
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ),
    "claude-opus-4-7": (
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ),
    "claude-opus-4-6": (
        "claude-haiku-4-5",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ),
    "claude-sonnet-4-6": (
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-5",
    ),
    "claude-haiku-4-5": (
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ),
}


def reasoning_format_for(provider: Provider, upstream_model_id: str) -> str:
    """The reasoning format a model emits: ``<wire>:<model>``. A Bedrock route
    names the same Claude as Anthropic direct (its ``<geo>.anthropic.`` prefix
    dropped), so the two share a format."""
    if provider == Provider.OPENAI:
        return f"openai:{upstream_model_id}"
    return f"anthropic:{upstream_model_id.rsplit('anthropic.', 1)[-1]}"


def default_models() -> list[DefaultModel]:
    """The current Anthropic and OpenAI models every deployment starts with."""
    return [
        # ===== Anthropic (direct) — 1M context at standard pricing; full cache
        # support (read + write-5m + write-1h). =====
        # ---- FRONTIER (latest; reserved for the main agent) ----
        # The Claude 5.x generation (Sonnet 5.5, Opus 5.5, Fable 5.1, Sonnet 5):
        # /v1/models reports a 1M input window, 128K max output, effort
        # low..max, and adaptive thinking only (thinking.type "enabled" 400s) —
        # each confirmed live. List prices from
        # https://platform.claude.com/docs/en/about-claude/pricing; cache writes
        # are the standard 1.25x / 2x, cache reads the per-model multiple there.
        DefaultModel(
            slug="claude-sonnet-5.5",
            display_name="Claude Sonnet 5.5",
            family="claude",
            provider=Provider.ANTHROPIC,
            upstream_model_id="claude-sonnet-5-5",
            cache_min_tokens=512,
            # The model a hosted reader starts on (HOSTED_DEFAULT_MODEL_SLUG), so
            # it sits with the models the main agent runs on, not in the
            # subagent-routable tiers — the same reason Opus 5 is here.
            tier=ModelTier.FRONTIER,
            efforts=CLAUDE_XHIGH_EFFORTS,
            default_effort="medium",
            thinking_mode="adaptive",
            # $2 in / $10 out; write-5m $2.50, write-1h $4, read $0.20 (0.1x).
            input_sell="2.00",
            output_sell="10.00",
            input_cost="2.00",
            output_cost="10.00",
            context_window=1_000_000,
            max_output_tokens=128_000,
        ),
        DefaultModel(
            slug="claude-opus-5.5",
            display_name="Claude Opus 5.5",
            family="claude",
            provider=Provider.ANTHROPIC,
            upstream_model_id="claude-opus-5-5",
            cache_min_tokens=512,
            tier=ModelTier.FRONTIER,
            efforts=CLAUDE_XHIGH_EFFORTS,
            default_effort="medium",
            thinking_mode="adaptive",
            # $4 in / $20 out; write-5m $5, write-1h $8; cache reads are 0.05x
            # input on this model ($0.20), not the usual 0.1x.
            input_sell="4.00",
            output_sell="20.00",
            input_cost="4.00",
            output_cost="20.00",
            cache_read_multiplier="0.05",
            context_window=1_000_000,
            max_output_tokens=128_000,
        ),
        DefaultModel(
            slug="claude-fable-5.1",
            display_name="Claude Fable 5.1",
            family="claude",
            provider=Provider.ANTHROPIC,
            upstream_model_id="claude-fable-5-1",
            cache_min_tokens=512,
            tier=ModelTier.FRONTIER,
            efforts=CLAUDE_XHIGH_EFFORTS,
            default_effort="medium",
            thinking_mode="adaptive",
            # $10 in / $50 out; write-5m $12.50, write-1h $20; cache reads are
            # 0.025x input on this model ($0.25), not the usual 0.1x.
            input_sell="10.00",
            output_sell="50.00",
            input_cost="10.00",
            output_cost="50.00",
            cache_read_multiplier="0.025",
            context_window=1_000_000,
            max_output_tokens=128_000,
        ),
        # ---- CHEAP (subagent-eligible) ----
        DefaultModel(
            slug="claude-haiku-4.5",
            display_name="Claude Haiku 4.5",
            family="claude",
            provider=Provider.ANTHROPIC,
            upstream_model_id="claude-haiku-4-5",
            cache_min_tokens=4096,
            tier=ModelTier.CHEAP,
            # Haiku 4.5 REJECTS output_config.effort and thinking:{type:"adaptive"}
            # (both 400 live), but DOES support thinking:{type:"enabled",
            # budget_tokens}. So instead of a thinking_mode, it carries the
            # ANTHROPIC_HAIKU_STYLE_THINKING flag: the gateway maps the chosen effort
            # to a hardcoded budget (low/medium/high) and injects enabled-thinking;
            # `none` (the default) sends NO thinking params at all. thinking_mode
            # stays None — the flag, not thinking_mode, drives this path.
            efforts=HAIKU_EFFORTS,
            default_effort="none",
            thinking_mode=None,
            flags=[ModelFlag.ANTHROPIC_HAIKU_STYLE_THINKING.value],
            input_sell="1.00",
            output_sell="5.00",
            input_cost="1.00",
            output_cost="5.00",
            context_window=200_000,
        ),
        # ---- GPT-6 Astra, GPT-6.1 Sol and GPT-6 Luna. Served on both chat
        # completions and Responses (probed live). 1.05M context, 128K output
        # (the model pages). Effort enums are the ones the chat wire accepts,
        # probed live with an invalid value — Astra and 6.1 Sol reject `none`, and
        # no model accepts the docs' `max`. Prices from
        # https://developers.openai.com/api/docs/pricing (Standard tier): short
        # context as listed, and above 272K input tokens 2x input, 2x cached
        # input, 2x cache write and 1.5x output. Cache writes are 1.25x input,
        # reported as `cache_write_tokens` and metered on cache_write_5m. ----
        DefaultModel(
            slug="gpt-6-astra",
            display_name="GPT-6 Astra",
            family="gpt",
            provider=Provider.OPENAI,
            upstream_model_id="gpt-6-astra",
            cache_write_multiplier="1.25",
            tier=ModelTier.FRONTIER,
            efforts=OPENAI_NO_NONE_EFFORTS,
            default_effort="medium",
            # $10 in / $50 out; cached $1 (0.1x). >272K: $20 / $75, cached $2.
            input_sell="10.00",
            output_sell="50.00",
            input_cost="10.00",
            output_cost="50.00",
            long_context=OPENAI_LONG_CONTEXT,
            context_window=1_050_000,
            max_output_tokens=128_000,
        ),
        DefaultModel(
            slug="gpt-6.1-sol",
            display_name="GPT-6.1 Sol",
            family="gpt",
            provider=Provider.OPENAI,
            upstream_model_id="gpt-6.1-sol",
            cache_write_multiplier="1.25",
            tier=ModelTier.FRONTIER,
            efforts=OPENAI_NO_NONE_EFFORTS,
            default_effort="medium",
            # $2 in / $10 out; cached $0.10 — 0.05x, not the usual 0.1x.
            # >272K: $4 / $15, cached $0.20.
            input_sell="2.00",
            output_sell="10.00",
            input_cost="2.00",
            output_cost="10.00",
            cache_read_multiplier="0.05",
            long_context=OPENAI_LONG_CONTEXT,
            context_window=1_050_000,
            max_output_tokens=128_000,
        ),
        DefaultModel(
            slug="gpt-6-luna",
            display_name="GPT-6 Luna",
            family="gpt",
            provider=Provider.OPENAI,
            upstream_model_id="gpt-6-luna",
            cache_write_multiplier="1.25",
            tier=ModelTier.CHEAP,
            efforts=OPENAI_CHAT_EFFORTS,
            default_effort="medium",
            # $0.10 in / $0.50 out; cached $0.01 (0.1x). >272K: $0.20 / $0.75,
            # cached $0.02.
            input_sell="0.10",
            output_sell="0.50",
            input_cost="0.10",
            output_cost="0.50",
            long_context=OPENAI_LONG_CONTEXT,
            context_window=1_050_000,
            max_output_tokens=128_000,
        ),
    ]


#: A step that runs after the default catalog is loaded, in the same session.
#: Returns a one-line summary.
CatalogLoader = Callable[[AsyncSession], Awaitable[str]]

CATALOG_LOADERS: ExtensionPoint[CatalogLoader] = ExtensionPoint("model_catalog_loaders")


async def add_model(session: AsyncSession, spec: DefaultModel) -> bool:
    """Add ``spec`` and its route if the catalog lacks the model; leave an
    existing model exactly as it is. Returns True if it was added."""
    if await session.get(Model, spec.slug) is not None:
        return False
    session.add(
        Model(
            id=spec.slug,
            display_name=spec.display_name,
            family=spec.family,
            enabled=True,
            context_window=spec.context_window,
            max_output_tokens=spec.max_output_tokens,
            supports_thinking=spec.supports_thinking,
            supports_caching=spec.supports_caching,
            cache_min_tokens=spec.cache_min_tokens,
            default_credit_class=CreditClass.FREE_MONTHLY,
            tier=spec.tier,
            reasoning_efforts=list(spec.efforts),
            default_effort=spec.default_effort,
            thinking_mode=spec.thinking_mode,
            flags=list(spec.flags),
            reasoning_format=(
                reasoning_format_for(spec.provider, spec.upstream_model_id)
                if spec.supports_thinking
                else None
            ),
            reads_reasoning_formats=[
                reasoning_format_for(spec.provider, upstream)
                for upstream in PROVEN_REASONING_READS.get(spec.upstream_model_id, ())
            ],
        )
    )
    await session.flush()
    session.add(
        ModelRoute(
            model_id=spec.slug,
            provider=spec.provider,
            upstream_model_id=spec.upstream_model_id,
            region=spec.region,
            priority=0,
            enabled=True,
        )
    )
    await session.flush()
    return True


async def load_default_catalog(session: AsyncSession) -> list[str]:
    """Add every default model this deployment lacks, with its route, then run
    each registered :data:`CATALOG_LOADERS` step. Returns the default slugs it
    created; empty on a re-run."""
    created = [spec.slug for spec in default_models() if await add_model(session, spec)]
    for step in CATALOG_LOADERS.items():
        await step(session)
    return created


async def seed_model_catalog(session: AsyncSession) -> str:
    """The seed every deployment runs: the default catalog, in every environment."""
    created = await load_default_catalog(session)
    return f"models created: {', '.join(created)}" if created else "models unchanged"


__all__ = [
    "CATALOG_LOADERS",
    "CLAUDE_46_EFFORTS",
    "CLAUDE_XHIGH_EFFORTS",
    "HAIKU_EFFORTS",
    "OPENAI_CHAT_EFFORTS",
    "OPENAI_LONG_CONTEXT",
    "OPENAI_NO_NONE_EFFORTS",
    "OPENAI_RESP_EFFORTS",
    "PROVEN_REASONING_READS",
    "CatalogLoader",
    "DefaultModel",
    "LongContextTier",
    "add_model",
    "default_models",
    "load_default_catalog",
    "reasoning_format_for",
    "seed_model_catalog",
]
