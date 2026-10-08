"""Chat manifest — session-level metadata cache.

The manifest is a CACHE. JSONL is the
source of truth. Every field is defaulted so a corrupt or partial
manifest never bricks a chat — the reader can fall back to folding the
JSONL and reconstructing.

Manifest is written atomically (temp + rename) by the Chat handle on
close. Mid-session writes are best-effort and not required for
correctness — a crash mid-write leaves the previous manifest intact,
and on next open the fold-recovery path covers any drift.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import Field

from alkera_core.schemas.chat.base import VersionedChatModel


class TokenTotals(VersionedChatModel):
    """Aggregate token counts across the whole session.

    Source of truth is per-step `StepFinishPart.tokens`. The manifest aggregate is derived;
    recompute from the JSONL if you don't trust the cache.
    """

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


class ChatManifest(VersionedChatModel):
    """Session-level metadata cache. Every field defaulted."""

    # 1.2.0: added the unseen-badge / list-preview cache fields below
    # (last_message_preview, last_event_id, last_seen_event_id) — all
    # additive optional → minor bump.
    # 1.3.0: added background_jobs_running (additive, default 0) → minor bump.
    # 1.4.0: added analysis_pipeline (additive, default "off") → minor bump.
    SCHEMA_VERSION: ClassVar[str] = "1.4.0"

    session_id: str = ""
    parent_session_id: str | None = None
    title: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    cwd: str | None = None
    model: dict[str, Any] = Field(default_factory=dict)
    """Provider + model identifiers, e.g.
    `{"provider_id": "anthropic", "model_id": "claude-opus-4-7"}`.
    """
    agent: str | None = None
    permission_mode: str = "default"
    """The harness permission mode the session was last in
    (``read_only`` / ``default`` / ``auto`` / ``plan`` / ``bypass``), so resuming
    a chat restores how much autonomy edits/commands get rather than silently
    reverting to ``default``. A bare ``str`` (not the CLI's ``PermissionMode``
    Literal) to keep api-core free of an apps/cli dependency; the CLI coerces it
    on resume (a retired value like ``accept_edits`` normalizes to ``default``).

    Old manifests (pre-introduction of this field) load with the default value
    ``"default"`` — the correct historical behavior (resume always started in
    default mode before this field existed)."""
    analysis_pipeline: str = "off"
    """The analysis selector the session was last in (``off`` / ``analyst``), so
    a resumed chat keeps analyst mode. A bare ``str`` for the same reason as
    ``permission_mode``; the CLI coerces an unknown value to ``off``."""
    tokens_total: TokenTotals = Field(default_factory=TokenTotals)
    cost_total: float = 0.0
    harness_type: str = "agent"
    """The harness adapter bound to this chat. **Immutable** — set at
    creation time and used on every resume to pick the matching adapter.
    ``"agent"`` is the opencode adapter and ``"claude-agent"`` the Claude
    Code adapter (see ``alkera_cli.harness.registry``). The slug is stored
    in every manifest, so it never changes. Old manifests load with their
    stored value; ones predating this field load as ``"agent"``.
    """
    harness: dict[str, Any] = Field(default_factory=dict)
    """Harness identification details (writer name/version, pinned
    upstream SHA, etc.). Free-form companion to the typed
    ``harness_type`` above."""
    last_message_preview: str | None = None
    """First line of the most recent prose part — a display cache for
    chat-list UIs so listing N chats never tail-scans N JSONL files.
    Maintained by `Chat.append_event`; the JSONL stays the source of
    truth (the fold-recovery path reconstructs it)."""
    last_event_id: str | None = None
    """event_id of the most recent PERSISTED event (the chat.jsonl
    tail). Compared against ``last_seen_event_id`` for unseen badges."""
    last_seen_event_id: str | None = None
    """The last event the user's client had rendered when it last
    flushed. ``last_seen_event_id != last_event_id`` → the chat has
    activity the user hasn't seen. ``None`` (old manifests, never-opened
    chats) reads as unseen — the correct cold-start default."""
    background_jobs_running: int = 0
    """How many background jobs (bash / sql / subagent) are running RIGHT NOW
    in this chat — drives the "active in the background" indicator in the chat
    list (TUI + VS Code/portal). A pure runtime view: the runtime overlays the
    live registry count onto the listing of a chat that is OPEN with running
    jobs; the on-disk manifest is never written with a nonzero value (jobs do
    not survive a daemon restart, so a persisted count would be stale). Always
    ``0`` for a chat that isn't open, and on every fresh load."""


__all__ = ["ChatManifest", "TokenTotals"]
