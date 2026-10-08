"""The activity input an agent tool call would carry — the replay seam.

Nothing adopts this yet. It exists so the worker's activity interceptor has a
settled shape to recognise when a future activity executes a tool call on an
agent's behalf: the interceptor records the call (by reference, never by
payload) so a session can later be replayed or audited. Defined here, not in
the worker, because it would cross workflow history and so must be a versioned
persisted shape with a fixture.
"""

from __future__ import annotations

from typing import ClassVar

from alkera_core.versioning import VersionedModel


class ToolCallActivityInput(VersionedModel):
    """Identity of one tool call, by reference only."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    tool_name: str
    session_id: str
    call_id: str
    org_id: str
    input_ref: str
    """A blob / row reference to the call's input — never the input itself, so no
    payload (and no PII) crosses history."""
    idempotency_key: str
    """What a retried attempt presents so the tool runs once."""
