"""Friendly harness names ↔ the immutable manifest ``harness_type`` slug.

The manifest stores a ``harness_type`` slug (``"agent"`` for the opencode
adapter, the default, and ``"claude-agent"`` for Claude Code). The CLI
``--harness`` flag and the daemon ``harness`` param speak FRIENDLY names and
normalize them here, so the on-disk slug stays stable. The default harness is
advertised as ``alkera``, the product's agent; ``opencode`` names the same
adapter. Unknown names resolve to ``None`` (caller surfaces a choice error).
"""

from __future__ import annotations

#: Canonical manifest slugs (what `AdapterFactory` dispatches on; IMMUTABLE per chat).
OPENCODE_HARNESS = "agent"
CLAUDE_HARNESS = "claude-agent"

#: friendly/alias → canonical slug. Accepts the slugs themselves too, so the
#: daemon can receive either form. ``alkera`` and ``opencode`` both name the
#: default, opencode-backed harness.
_HARNESS_ALIASES: dict[str, str] = {
    "alkera": OPENCODE_HARNESS,
    "opencode": OPENCODE_HARNESS,
    OPENCODE_HARNESS: OPENCODE_HARNESS,
    "claude": CLAUDE_HARNESS,
    "claude-code": CLAUDE_HARNESS,
    "cc": CLAUDE_HARNESS,
    CLAUDE_HARNESS: CLAUDE_HARNESS,
}

#: Friendly names the CLI advertises in ``--harness`` help / choices.
HARNESS_CHOICES: tuple[str, ...] = ("alkera", "claude")


def resolve_harness_type(name: str) -> str | None:
    """Friendly name / alias / slug → canonical ``harness_type``, or ``None`` if
    unrecognized."""
    return _HARNESS_ALIASES.get(name.strip().lower())


__all__ = [
    "CLAUDE_HARNESS",
    "HARNESS_CHOICES",
    "OPENCODE_HARNESS",
    "resolve_harness_type",
]
