"""The model catalog: the models a person can pick and the provider routes
the gateway tries for each, in priority order.

Open: routing needs it with or without billing. What a model costs and what
it sells for belong to the billing extension's own catalog models.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid, false, func, text, true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from alkera_core.db.base import Base
from alkera_core.db.enum_columns import enum_type
from alkera_core.db.errors import register_unique
from alkera_core.llm_provider import Provider
from alkera_core.model_catalog import CreditClass, ModelTier

#: The primary key of ``billing_models``: a model's id is its slug.
MODEL_ID_KEY = "billing_models_pkey"
#: What a second model with the same id is told.
MODEL_EXISTS_MESSAGE = "A model with this id already exists."


class Model(Base):
    """An Alkera-canonical model the user can select (e.g. ``claude-sonnet-4.6``).

    The slug `id` is the public identifier surfaced via the gateway's
    ``/v1/models`` and referenced by routes + prices.
    """

    __tablename__ = "billing_models"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    family: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    context_window: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The model's maximum output (completion) tokens. Surfaced via `/v1/models` so
    # the agent can reserve output headroom when deciding to compact: the harness
    # gives opencode a per-model `limit: {context, output}`, and opencode's overflow
    # check computes the usable input budget as roughly `context - output`. 0 =
    # unknown (the harness then omits `output` / falls back to a conservative default).
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    supports_thinking: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    supports_caching: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # The provider's MINIMUM CACHEABLE PREFIX for this model, in tokens. A
    # `cache_control` breakpoint over a shorter prefix is ignored and those
    # tokens are charged as ordinary input. Anthropic publishes 512, 1024, 2048
    # or 4096 depending on the model, and the figure does not track model size
    # (Haiku 4.5 needs 4096 while Sonnet 5 needs 1024), so it is recorded per
    # model rather than inferred from one. NULL = not recorded, which the
    # gateway's settle-time estimate treats as "cannot tell whether a breakpoint
    # took effect" and bills the whole prompt as plain input.
    cache_min_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Selectable reasoning-effort variants for this model (provider-specific
    # values, e.g. Claude `low/medium/high`, OpenAI `minimal/low/medium/high`).
    # Empty = no variant choice. `default_effort` is the one used when the caller
    # doesn't pick. The gateway translates the chosen effort into the provider's
    # native knob (`output_config.effort` / `reasoning_effort` / Bedrock thinking).
    reasoning_efforts: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), nullable=False, server_default=text("'{}'")
    )
    default_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The identity of the replayable reasoning this model emits (e.g.
    # "anthropic:claude-opus-5-5"); NULL = none. A chat whose history carries a
    # format may only move to a model that reads it (alkera_core.chat_models).
    reasoning_format: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The OTHER formats this model reads on replay; it always reads its own.
    # Seeded only with reads a live probe proved; empty = reads only itself.
    reads_reasoning_formats: Mapped[list[str]] = mapped_column(
        ARRAY(String(128)), nullable=False, server_default=text("'{}'")
    )
    # How the gateway engages reasoning when an effort is chosen (Anthropic-family
    # generations differ — verified live): "adaptive" (Claude 4.6+, needs
    # thinking:{type:adaptive}), "effort_beta" (Opus 4.5, effort works standalone
    # but needs the effort-2025-11-24 beta flag), or NULL (OpenAI reasoning_effort
    # / non-reasoning). The gateway never string-sniffs the model id — it reads this.
    thinking_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Capability/behavior flags (a set; any combination) interpreted by the gateway
    # — e.g. "anthropic_haiku_style_thinking" engages thinking:{type:"enabled",
    # budget_tokens:N} instead of output_config.effort. See ModelFlag. Mirrors the
    # reasoning_efforts ARRAY shape.
    flags: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False, server_default=text("'{}'")
    )
    # Capability/cost tier — drives subagent model routing (explore → cheapest
    # non-FRONTIER). Defaults to STANDARD so existing rows stay main-agent-usable.
    tier: Mapped[ModelTier] = mapped_column(
        enum_type(ModelTier, "model_tier"),
        nullable=False,
        server_default=ModelTier.STANDARD.value,
    )
    default_credit_class: Mapped[CreditClass] = mapped_column(
        enum_type(CreditClass, "credit_class"),
        nullable=False,
        server_default=CreditClass.FREE_MONTHLY.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ModelRoute(Base):
    """Ordered provider routing for a model. The gateway tries the lowest
    `priority` enabled route first, failing over to the next on a retryable
    error."""

    __tablename__ = "billing_model_routes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("billing_models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    provider: Mapped[Provider] = mapped_column(enum_type(Provider, "provider"), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    upstream_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["Model", "ModelRoute"]


register_unique(MODEL_ID_KEY, "id", MODEL_EXISTS_MESSAGE)
