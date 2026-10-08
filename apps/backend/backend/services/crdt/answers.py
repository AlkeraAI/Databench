"""What the document store answers a peer with: the document it opens, and
the verdict on an update it sent."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from alkera_core.schemas.realtime import CrdtSaving, DocEnvelope


@dataclass(frozen=True, slots=True)
class Sync:
    """What a peer opening a document is sent: Loro updates since its vector
    (``mode == "updates"``) or the whole document, the server's vector after
    them, the epoch and schema they belong to, and whether the document's
    edits are reaching its source as its row records it."""

    mode: str
    data: bytes
    vv: bytes
    epoch: int
    doc_schema: int
    #: Whether its edits reach its source (``None``: it rests nowhere).
    saving: CrdtSaving | None = None


@dataclass(frozen=True, slots=True)
class Applied:
    """A judged update. ``changed`` is ``False`` for an update the server
    already held (a retry); ``envelope`` is the ``crdt`` broadcast the
    commit emits (``None`` when nothing changed)."""

    changed: bool
    epoch: int
    log_seq: int
    vv: bytes
    delta: bytes = b""
    envelope: DocEnvelope | None = None
    log_bytes: int = 0
    log_rows: int = 0
    snapshot_bytes: int = 0
    #: The worker cache key the update was judged under.
    cache_key: str = ""
    #: Who the broadcast is addressed to (the parent's team and visibility).
    team_id: UUID | None = None
    visibility: str = "org"
    #: What the type said the update touched (the strategy's ``describe``).
    notes: Mapping[str, Any] = field(default_factory=dict)


__all__ = ["Applied", "Sync"]
