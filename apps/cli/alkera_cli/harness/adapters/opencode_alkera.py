"""Build the opencode config that points opencode at the Alkera model gateway.

opencode reaches Anthropic-family and OpenAI-family models through the gateway,
authenticated by the user's Alkera JWT, never with provider keys. Two opencode
providers are configured:

- ``alkera-anthropic`` (``@ai-sdk/anthropic``): the gateway's Anthropic Messages
  ingress. The AI SDK posts ``{baseURL}/messages``, so the baseURL ends in
  ``/anthropic/v1``.
- ``alkera-openai`` (``@ai-sdk/openai``): the gateway's OpenAI Responses API
  ingress, for reasoning continuity across turns (stateless encrypted
  reasoning), reasoning summaries and codex models, none of which Chat
  Completions supports. Zero data retention holds because the gateway forces
  ``store=false`` and asks for ``reasoning.encrypted_content``. The AI SDK posts
  ``{baseURL}/responses``, so the baseURL ends in ``/openai/v1``. The custom
  provider id is what sends ``@ai-sdk/openai`` down its default Responses path;
  opencode hardcodes ``sdk.responses()`` only for the built-in ``openai`` id.

**Idempotency.** opencode's provider ``options.headers`` are static, so a
per-request idempotency key cannot be expressed here, and one fixed key would
make the gateway 409 every call after the first. No idempotency header is set.
That is safe: the gateway mints a ``request_id`` per request and the billable
path always answers 200 with errors in-band as SSE events, so the AI SDK never
HTTP-retries a request that already billed. ``x-alkera-idempotency-key`` stays
the contract for direct gateway clients that can set a per-request value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from alkera_core.brand import product_name
from alkera_core.gateway import GATEWAY_CLIENT_SILENCE_SECONDS, join_model_effort

from alkera_cli.contracts.gateway_model import GatewayModel, WireProtocol

#: opencode provider ids (the ``<provider>/<model>`` left-hand side).
ANTHROPIC_PROVIDER_ID = "alkera-anthropic"
OPENAI_PROVIDER_ID = "alkera-openai"

#: opencode's idle bound on a streamed response (``chunkTimeout``, ms): a step
#: that hears nothing from the gateway, keepalive comments included, for this
#: long is on a dead connection and is ended with an error rather than waited
#: on forever. A healthy step never trips it, however long the model thinks.
STREAM_SILENCE_MS = int(GATEWAY_CLIENT_SILENCE_SECONDS * 1000)


def anthropic_base_url(gateway_url: str) -> str:
    # @ai-sdk/anthropic appends "/messages" → …/anthropic/v1/messages.
    return f"{gateway_url.rstrip('/')}/anthropic/v1"


def openai_base_url(gateway_url: str) -> str:
    # @ai-sdk/openai appends "/responses" → …/openai/v1/responses (the gateway route).
    return f"{gateway_url.rstrip('/')}/openai/v1"


def _provider_id_for(wire: WireProtocol) -> str:
    return ANTHROPIC_PROVIDER_ID if wire == "anthropic" else OPENAI_PROVIDER_ID


def build_manifest_model(model: GatewayModel, effort: str | None) -> dict[str, Any]:
    """The pinned manifest-model dict for a chosen gateway model + effort — the
    SINGLE source of the chat-pinning shape (provider_id, base model id, efforts,
    chosen effort). Both the CLI and the daemon (editor open_chat)
    call this, so a chat pins the same shape whichever started it; the gateway
    provider id is resolved here (one place) instead of re-encoded per client.
    Stores the BASE model id (no `::effort`); the chosen effort is a separate
    field the runtime bakes into opencode's default model."""
    if effort and effort in model.efforts:
        chosen: str | None = effort
    elif model.default_effort and model.default_effort in model.efforts:
        chosen = model.default_effort
    else:
        chosen = model.efforts[0] if model.efforts else None
    pinned: dict[str, Any] = {
        "provider_id": _provider_id_for(model.wire),
        "model_id": model.id,
        "display_name": model.display_name,
        "efforts": list(model.efforts),
        "effort": chosen,
        # Persist the limits so the daemon single-model config builder (which
        # reconstructs a GatewayModel from this manifest dict) can emit opencode's
        # per-model `limit` and arm native auto-compaction. 0 = unknown (omitted).
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
    }
    # The reasoning the model writes and reads, so a config rebuilt from this
    # dict tells opencode which earlier reasoning it may replay as written.
    if model.reasoning_format:
        pinned["reasoning_format"] = model.reasoning_format
    if model.reads_reasoning_formats:
        pinned["reads_reasoning_formats"] = list(model.reads_reasoning_formats)
    return pinned


def build_alkera_opencode_config(
    *,
    gateway_url: str,
    token: str,
    models: Sequence[GatewayModel],
    default_model: str | None = None,
    default_effort: str | None = None,
    reasoning_formats_by_model: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The inline opencode config (merged into ``OPENCODE_CONFIG_CONTENT`` via
    ``SessionConfig.harness_native["agent_config"]``) that routes opencode at
    the Alkera gateway with ``token`` as the per-provider apiKey.

    Each model contributes a base entry **plus one entry per reasoning-effort
    variant** (id ``"<model_id>::<effort>"``), so the harness can switch the
    variant per-prompt without reconfiguring opencode — the gateway reads the
    ``::effort`` suffix and injects the provider's native knob. Only the
    provider(s) that actually have models are emitted. ``default_model`` /
    ``default_effort`` choose the config's default ``model`` (the variant must be
    one the model supports, else falls back to the base). Raises ``ValueError``
    if ``token`` is empty (never point opencode at the gateway unauthenticated).

    A model that reads other models' reasoning also carries
    ``options.alkera`` (see :func:`_reasoning_options`), so opencode replays an
    earlier model's reasoning to it as written after a switch.
    ``reasoning_formats_by_model`` names the formats of models that ran in the
    chat but are not in ``models`` (a box spawns on the chat's model alone).
    """
    if not token:
        raise ValueError("token is required — the agent must authenticate to the gateway")
    if not models:
        # No models ⇒ no provider entry ⇒ opencode would silently fall back to its
        # own default (public hosted) model, breaking the "everything routes through
        # the Alkera gateway" contract. Fail loudly instead.
        raise ValueError("at least one model is required — the gateway returned none")

    formats = {
        **dict(reasoning_formats_by_model or {}),
        **{m.id: m.reasoning_format for m in models if m.reasoning_format},
    }
    anthropic_models = _model_entries([m for m in models if m.wire == "anthropic"], formats)
    openai_models = _model_entries([m for m in models if m.wire == "openai"], formats)

    provider: dict[str, Any] = {}
    if anthropic_models:
        provider[ANTHROPIC_PROVIDER_ID] = {
            "npm": "@ai-sdk/anthropic",
            "name": f"{product_name()} (Claude)",
            "options": {
                "baseURL": anthropic_base_url(gateway_url),
                "apiKey": token,
                "chunkTimeout": STREAM_SILENCE_MS,
            },
            "models": anthropic_models,
        }
    if openai_models:
        provider[OPENAI_PROVIDER_ID] = {
            "npm": "@ai-sdk/openai",
            "name": f"{product_name()} (OpenAI)",
            "options": {
                "baseURL": openai_base_url(gateway_url),
                "apiKey": token,
                "chunkTimeout": STREAM_SILENCE_MS,
            },
            "models": openai_models,
        }

    config: dict[str, Any] = {"provider": provider}
    chosen = _resolve_default_model(default_model, default_effort, models)
    if chosen is not None:
        config["model"] = chosen
    return config


# Used when a model's max output is unknown (catalog value 0). It becomes the
# per-step `max_tokens` opencode requests for that model, so it stays at opencode's
# own ceiling: a cap no model in the catalog has ever rejected.
_DEFAULT_MAX_OUTPUT_TOKENS = 32_000

# Trigger auto-compaction at this fraction of the real context window. opencode COMPACTS
# by summarizing the conversation — it sends the history to a summarizer model — which
# itself overflows ("Session too large to compact") if the conversation is already at the
# hard limit. opencode's default trigger (context - max_output ≈ 94%) leaves almost no
# room, so a long chat hits the wall and CAN'T be freed. We trigger earlier via
# `limit.input` (opencode uses it ONLY for the overflow check — never to gate/reject
# requests, verified in overflow.ts). For GRADUAL growth this is plenty of margin
# (summarization sends less than the full history — it drops the tail + truncates tool
# outputs). The remaining ~10% buffer also caps how large a SINGLE un-truncatable turn
# (a big paste/file) can be before it blows past the hard wall — about window*(1-fraction)
# (~100k on a 1M model). Lower = absorbs bigger single jumps but wastes more window; 0.90
# comfortably covers gradual fill + modest jumps without giving up much context.
_COMPACTION_TRIGGER_FRACTION = 0.90


def _model_limit(model: GatewayModel) -> dict[str, int] | None:
    """opencode's per-model ``limit`` (``{context, input, output}``) for a gateway model,
    or None when the context window is unknown.

    Two jobs:
    1. **Arm overflow detection.** opencode's ``isOverflow`` short-circuits to *false*
       when ``limit.context == 0`` (every custom ``alkera-*`` provider model resolves to
       0 — absent from models.dev — unless we supply it). Emitting the real window
       re-arms native auto-compaction; omitting ``limit`` when the window is unknown
       keeps the safe legacy behavior (no false ``context: 0``).
    2. **Leave compaction headroom.** ``limit.input`` triggers compaction at ~80% of the
       window (``usable = input - reserved``) so opencode can actually SUMMARIZE the
       history — a long chat that only compacts at the hard limit can't be freed."""
    if model.context_window <= 0:
        return None
    output = model.max_output_tokens if model.max_output_tokens > 0 else _DEFAULT_MAX_OUTPUT_TOKENS
    trigger = max(1, int(model.context_window * _COMPACTION_TRIGGER_FRACTION))
    return {"context": model.context_window, "input": trigger, "output": output}


def _reasoning_options(model: GatewayModel, formats: Mapping[str, str]) -> dict[str, Any] | None:
    """opencode's ``options.alkera`` for a model that reads other models'
    reasoning: the formats it reads, and the format each model it could be
    switched from writes, keyed by base model id (opencode files a reply under
    the model key it ran on). The vendored ``alkeraCanReplayReasoning`` reads
    both; opencode never sends them to the provider. ``None`` when the model
    reads only its own reasoning, which opencode replays without help."""
    reads = set(model.reads_reasoning_formats)
    if not reads:
        return None
    known = {mid: fmt for mid, fmt in sorted(formats.items()) if fmt in reads and mid != model.id}
    return {
        "alkera": {
            "readsReasoningFormats": sorted(reads),
            "reasoningFormatsByModel": known,
        }
    }


def _model_entries(
    models: Sequence[GatewayModel], formats: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    """The opencode ``models`` map for one wire: a base entry per model plus one
    per effort variant (keyed ``"<id>::<effort>"``). Each entry carries the model's
    ``limit`` (when known) so opencode's native auto-compaction is armed for the base
    AND every effort variant, and what reasoning it reads (``_reasoning_options``)."""
    entries: dict[str, dict[str, Any]] = {}
    for m in models:
        m_limit = _model_limit(m)
        options = _reasoning_options(m, formats)
        base: dict[str, Any] = {"name": m.display_name}
        if m_limit is not None:
            base["limit"] = dict(m_limit)
        if options is not None:
            base["options"] = options
        entries[m.id] = base
        for effort in m.efforts:
            variant: dict[str, Any] = {"name": f"{m.display_name} ({effort})"}
            if m_limit is not None:
                variant["limit"] = dict(m_limit)
            if options is not None:
                variant["options"] = options
            entries[join_model_effort(m.id, effort)] = variant
    return entries


def resolve_runtime_effort(
    selected_effort: str | None,
    offered_efforts: Sequence[str],
    catalog_default_effort: str | None,
) -> str | None:
    """Resolve the effort a running session actually sends to its provider.

    A supported explicit selection wins, followed by the catalog default when it
    is offered. Otherwise the base model is used without an effort variant. This
    intentionally differs from the model picker's middle-effort fallback: changing
    this rule can change provider cost for resumed sessions.
    """
    if selected_effort and selected_effort in offered_efforts:
        return selected_effort
    if catalog_default_effort and catalog_default_effort in offered_efforts:
        return catalog_default_effort
    return None


def _resolve_default_model(
    default_model: str | None, default_effort: str | None, models: Sequence[GatewayModel]
) -> str | None:
    if not models:
        return None
    target: GatewayModel | None = None
    if default_model is not None:
        target = next((m for m in models if m.id == default_model), None)
    if target is None:
        target = models[0]
    effort = resolve_runtime_effort(default_effort, target.efforts, target.default_effort)
    model_id = join_model_effort(target.id, effort)
    return f"{_provider_id_for(target.wire)}/{model_id}"
