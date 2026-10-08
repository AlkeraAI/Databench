"""The gateway's model catalog entry, as the CLI's packages share it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

WireProtocol = Literal["anthropic", "openai"]


@dataclass(frozen=True)
class GatewayModel:
    """A model the gateway can serve, as surfaced by ``GET /v1/models`` — its
    Alkera slug ``id`` (what the gateway resolves on), a display name, the
    ``wire`` protocol that decides which opencode provider it's filed under, and
    its selectable reasoning-effort variants (empty = no variant choice)."""

    id: str
    display_name: str
    wire: WireProtocol
    efforts: tuple[str, ...] = ()
    default_effort: str | None = None
    tier: str = "standard"
    """Capability/cost tier ("frontier" | "standard" | "cheap") — a plain str for
    forward-compat (an unknown future tier round-trips). Drives subagent routing;
    unused by opencode config building."""
    family: str = ""
    """Provider family slug from the catalog ("claude" | "gpt" | "test" | …).
    A plain str for forward-compat. E2e-fixture models carry ``"test"``; the
    model picker EXCLUDES those (see ``gateway_client.selectable_models``).
    Unused by opencode config building."""
    context_window: int = 0
    """The model's context-window size (tokens), from the gateway catalog. Emitted
    as opencode's per-model ``limit.context`` so opencode's NATIVE auto-compaction
    fires before the upstream rejects an over-long prompt. 0 = unknown → no ``limit``
    emitted (opencode treats ``limit.context = 0`` as "no overflow detection")."""
    max_output_tokens: int = 0
    """The model's max output (completion) tokens, from the gateway catalog. Emitted
    as opencode's per-model ``limit.output``, which caps the ``max_tokens`` opencode
    requests per step (the adapter lifts opencode's own 32k ceiling to the largest
    configured value) and sizes the compaction margin. 0 = unknown → a fallback is
    used."""
    reasoning_format: str | None = None
    """The identity of the replayable reasoning the model emits (``None``: none).
    A chat whose history carries a format may only move to a model that reads it."""
    reads_reasoning_formats: tuple[str, ...] = ()
    """The other reasoning formats the model reads on replay (it reads its own)."""
