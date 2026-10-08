"""Shared model-gateway wire conventions.

Used by BOTH the gateway service (`apps/model-gateway`) and the CLI/harness
client (`apps/cli`), so the encoding stays in exactly one place.

Reasoning-effort variants travel in the request's ``model`` field as
``"<model_id>::<effort>"`` (e.g. ``"claude-opus-4.5::high"``). It's a
per-request signal — NOT a session header — so the harness can change the
variant mid-chat with the model pinned, and the gateway translates the effort
into the provider's native knob. Gateway model slugs MUST NOT contain the
separator.
"""

from __future__ import annotations

from typing import Final

MODEL_EFFORT_SEPARATOR = "::"

# How long a client may hear NOTHING on a streamed step (not a byte, not one of
# the gateway's keepalive comments) before it reads the connection as dead. The
# gateway writes a keepalive comment every GATEWAY_CLIENT_KEEPALIVE_SECONDS while
# the provider is silent, so a healthy step, however long it thinks, never goes
# this quiet. A connection that went half-open (a NAT or load balancer dropped it
# without a reset) does, and without this bound the agent would wait on it
# forever while everything around it looks alive. The production settings
# validator holds the keepalive to at most a quarter of this.
GATEWAY_CLIENT_SILENCE_SECONDS: Final = 300.0
GATEWAY_KEEPALIVES_PER_CLIENT_SILENCE: Final = 4

# How the gateway engages reasoning for an Anthropic-family model when an effort
# is chosen. Verified live (see the reasoning-effort provider matrix):
#   "adaptive"    — Claude 4.6+: inject thinking:{type:"adaptive"} with the effort
#                   (effort alone is a no-op on 4.6).
#   "effort_beta" — Opus 4.5: effort works standalone but the effort-2025-11-24
#                   beta flag must be sent (header on the direct wire, body
#                   `anthropic_beta` on Bedrock) for output_config.effort to be
#                   accepted.
# OpenAI reasoning models + non-reasoning models leave this NULL.
THINKING_MODE_ADAPTIVE: Final = "adaptive"
THINKING_MODE_EFFORT_BETA: Final = "effort_beta"
THINKING_MODES: Final = (THINKING_MODE_ADAPTIVE, THINKING_MODE_EFFORT_BETA)

# The Anthropic beta flag that unlocks output_config.effort on Opus 4.5.
EFFORT_BETA_FLAG: Final = "effort-2025-11-24"

# Haiku 4.5-style enabled-thinking: a model carrying the ANTHROPIC_HAIKU_STYLE_THINKING
# flag maps its chosen effort to a FIXED thinking budget (the gateway API takes no
# explicit budget). Defined here — not buried in the gateway adapter — so the catalog
# CRUD can validate a flagged model's reasoning_efforts against the same set.
HAIKU_THINKING_BUDGETS: Final = {"low": 1024, "medium": 4096, "high": 16384}
# The efforts a Haiku-style model may expose: the budget keys + "none" (no thinking).
# Any other effort would route + charge but silently run with NO thinking.
HAIKU_THINKING_EFFORTS: Final = ("none", *HAIKU_THINKING_BUDGETS)

# The catalog slug a reader on Alkera's hosted SaaS starts a new chat on when they
# have chosen nothing of their own. Spelled ONCE — the dev catalog seed creates this
# slug and `alkera_core.chat_defaults` seeds new chats with it, so the two cannot
# drift. A self-hosted install is NOT steered: it keeps whatever its own catalog
# offers first (see `hosted_default_model`). A person's saved preference always wins.
HOSTED_DEFAULT_MODEL_SLUG: Final = "claude-sonnet-5.5"


def split_model_effort(model: str) -> tuple[str, str | None]:
    """``"<model_id>::<effort>"`` → ``(model_id, effort)``. No separator →
    ``(model, None)``. Whitespace/empty effort collapses to ``None``.

    Catalog/validation callers use this — they never see a display component
    (display only rides per-REQUEST model fields, parsed by
    :func:`split_model_variant`)."""
    base, sep, effort = model.partition(MODEL_EFFORT_SEPARATOR)
    if not sep:
        return model, None
    effort = effort.strip()
    return base, (effort or None)


def join_model_effort(model_id: str, effort: str | None) -> str:
    """Inverse of :func:`split_model_effort`."""
    if not effort:
        return model_id
    return f"{model_id}{MODEL_EFFORT_SEPARATOR}{effort}"


# Per-turn reasoning display signal derived solely from effective effort. Maps to Anthropic's
# `thinking.display` ("summarized" surfaces readable thinking text on Claude 4.6+
# adaptive thinking, which OMITS it by default). Literal `none` disables it; any
# enabled effort requests it. Two wires reach the gateway:
#   - opencode → the THINKING_DISPLAY_HEADER (its model field stays "<id>::<effort>");
#   - claude   → the model field's 3rd component (the SDK has no per-turn header).
DISPLAY_SUMMARIZED: Final = "summarized"
THINKING_DISPLAY_HEADER: Final = "x-alkera-thinking-display"


def thinking_display_for_effort(effort: str | None) -> str | None:
    """Surface reasoning for an enabled effort; the literal ``none`` disables it."""
    if effort is None or effort.strip().casefold() == "none":
        return None
    return DISPLAY_SUMMARIZED


# The self-hosted billing meter for a proxied request: which bucket funded it on the
# self-hosted side — "enterprise" (a seat draw, the covered plan) vs "additional" (a
# pool draw, the postpaid spend Alkera invoices). This is the ONLY channel the
# enterprise-vs-additional split crosses the trust boundary on: the self-hosted gateway
# computes it at reserve (from the funding-source scope) and sets the header; Alkera
# persists it on the UsageRecord. Absent on a direct (SaaS) request.
USAGE_METER_HEADER: Final = "x-alkera-usage-meter"
# A request's idempotency key: the gateway admits one request per key and refuses
# a repeat (409). A self-hosted gateway in proxy mode sends one derived from its own
# request id, so a retried hop is recognised upstream instead of billed twice.
IDEMPOTENCY_KEY_HEADER: Final = "x-alkera-idempotency-key"
METER_ENTERPRISE: Final = "enterprise"
METER_ADDITIONAL: Final = "additional"
# BYOK: the customer's own provider keys served the request — raw usage recorded
# locally for visibility, billed at provider list cost, NEVER invoiced by Alkera
# (the invoice rollup filters meter == "additional" only). Never sent as a header:
# a BYOK gateway doesn't call home, and the forwarded-header guard on the hosted
# gateway accepts only enterprise|additional, so "byok" can't be injected either.
METER_BYOK: Final = "byok"


def split_model_variant(model: str) -> tuple[str, str | None, str | None]:
    """``"<model_id>::<effort>::<display>"`` → ``(model_id, effort, display)``.
    Each trailing component is optional; whitespace/empty collapses to ``None``.
    Routing/pricing always resolve on ``model_id``; ``effort`` + ``display`` are
    per-request knobs the transport injects upstream."""
    parts = model.split(MODEL_EFFORT_SEPARATOR, 2)
    model_id = parts[0]
    effort = parts[1].strip() if len(parts) > 1 else ""
    display = parts[2].strip() if len(parts) > 2 else ""
    return model_id, (effort or None), (display or None)


def join_model_variant(model_id: str, effort: str | None, display: str | None) -> str:
    """Inverse of :func:`split_model_variant`. Omits empty trailing components
    (``display`` with no effort still round-trips as ``"<id>::::<display>"``)."""
    if not display:
        return join_model_effort(model_id, effort)
    return f"{model_id}{MODEL_EFFORT_SEPARATOR}{effort or ''}{MODEL_EFFORT_SEPARATOR}{display}"
