"""The ``data:`` bodies of the portal's server-sent event stream.

In-flight shapes on plain ``BaseModel``: a frame lives for one hop and nothing
persists it. ``SseEventData`` is declared as the stream route's 200 response
model so the OpenAPI schema — and through it the generated clients — carry
:class:`~alkera_core.events.types.RealtimeEventType`; a client's handler map
can then be checked exhaustive against the real vocabulary.

A frame is deliberately thin. It names WHAT changed (type, entity, id,
version, org) and a few ids that say WHERE — never the change itself: the
client refetches through the authorized REST surface, so the stream can never
leak a field the reader is not entitled to, and a thin frame keeps the outbox
payload — which may run to ``MAX_PAYLOAD_BYTES`` — off the wire. An id is
admitted here only when a reader who already has the entity id could have
asked for it, and only when a surface would otherwise have to refetch a whole
family to answer one frame.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from alkera_core.events.types import RealtimeEventType

#: The ``data:`` body of a ``reset`` frame. ``overflow``: the server dropped
#: events for this subscriber (a slow reader) and the client must refetch
#: everything it shows. ``cursor_ahead``: the cursor a client resumed with is
#: past the newest committed row, so the server could not replay anything.
ResetReason = str


class SseEventData(BaseModel):
    """The body of one ``event: <type>`` frame; ``id:`` on the frame is the
    outbox row id the client resumes from.

    The optional fields below are the only things a frame carries beyond
    the entity it names, and every one of them is an ID the reader could have
    asked for anyway. They exist so a client can narrow WHICH cached read it
    re-takes: a folder mounted live has to know that this node sits in the
    folder it is showing, and a file open in a tab has to know its own node
    moved, or every frame costs a refetch of everything. They stay optional
    because plenty of writers genuinely do not hold them — a client handed
    ``None`` refreshes wider, which is slower and never wrong.
    """

    model_config = ConfigDict(extra="forbid")

    type: RealtimeEventType
    entity: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=1, max_length=255)
    version: int = Field(default=0, ge=0)
    org_id: UUID
    #: The drive the changed node belongs to.
    drive_id: UUID | None = None
    #: The folder the changed node sits in — absent for a drive root.
    parent_id: UUID | None = None
    #: Why the node changed, when the writer has a word for it.
    reason: str | None = Field(default=None, max_length=64)
    #: The leased folder whose in-flight plane moved — the root of the subtree,
    #: never the file inside it.
    lease_node_id: UUID | None = None
    #: On a lease frame: the change touched more folders under the lease than
    #: it named one by one, so every folder a client has open under it is
    #: stale. Absent otherwise.
    subtree: bool | None = None


class SseResetData(BaseModel):
    """The body of an ``event: reset`` frame (which carries no ``id:``, so the
    client's cursor is untouched)."""

    model_config = ConfigDict(extra="forbid")

    reason: ResetReason = Field(min_length=1, max_length=64)


__all__ = ["ResetReason", "SseEventData", "SseResetData"]
