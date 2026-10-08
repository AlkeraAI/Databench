"""Ending a chat's turn when no box is left to say it ended.

A turn's state is the box's to write: it says ``working`` when a turn starts
and ``idle`` when it ends. When the box goes away under the turn (its machine
stopped because the credit ran out, a person stopped it, it left the plane),
or a reader's Stop reaches a chat no box holds, nothing will ever write
``idle``: the chat owes a turn to every listing, the tape reads "Working…"
and the composer offers Stop and nothing else, until some box takes the chat
again and notices. So the server ends the turn the way the box would: the
same ``set_meta`` on the chat's document under the server's own peer id, the
same two columns every listing reads, and the same frame on the chat's
channel, so a viewer that folds the frame and one that reloads read one thing.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.db.locking import LockRank, lock_rows
from alkera_core.events import Entity, EventType, emit
from alkera_core.models.realtime_doc import RealtimeDoc
from alkera_core.models.workspace_object import WorkspaceObject
from alkera_core.objects.chat_transcript import publish_server_entries
from alkera_core.schemas.objects.transcript import aside_note_id, system_note_entries
from alkera_core.schemas.realtime import SERVER_PEER_ID, DocEnvelope, OpPayload
from alkera_core.status import CHAT_STATUS, Generic

#: The reason a turn the server ended because its box refused the chat is
#: recorded under (the chat's status vocabulary words it).
REFUSED: Final = "refused"

#: The meta key a chat's turn state rides under, and its timestamp's.
TURN_STATE_META_KEY: Final = "turn_state"
TURN_STATE_AT_META_KEY: Final = "turn_state_at"


def _working(doc: RealtimeDoc) -> bool:
    # The live column first, as every listing reads it; a document stamped
    # before the column existed carries the word in its meta alone.
    if doc.turn_state is not None:
        return doc.turn_state == "working"
    meta = (doc.state or {}).get("meta")
    turn = meta.get(TURN_STATE_META_KEY) if isinstance(meta, dict) else None
    return isinstance(turn, dict) and turn.get("state") == "working"


async def end_turn_nobody_runs(
    db: AsyncSession, chat: WorkspaceObject, *, actor: Mapping[str, Any] | None
) -> bool:
    """Stamp ``chat``'s document idle when it says a turn is working. The
    caller holds the document and the chat, taken in that order
    (:func:`alkera_core.objects.chat_end.lock_chat_for_write`); re-taking the
    document here costs nothing. True when the document changed."""
    return await _stamp_idle(db, chat, actor=actor) is not None


async def _stamp_idle(
    db: AsyncSession, chat: WorkspaceObject, *, actor: Mapping[str, Any] | None
) -> RealtimeDoc | None:
    """:func:`end_turn_nobody_runs`, answering with the locked document it
    stamped, or ``None`` when no turn was working."""
    doc = (
        await lock_rows(
            db,
            LockRank.REALTIME_DOC,
            select(RealtimeDoc)
            .where(
                RealtimeDoc.org_id == chat.org_team_id,
                RealtimeDoc.doc_type == "chat",
                RealtimeDoc.doc_id == str(chat.id),
            )
            .execution_options(populate_existing=True),
        )
    ).scalar_one_or_none()
    if doc is None or not _working(doc):
        return None
    at = datetime.now(UTC).isoformat()
    writes = {TURN_STATE_META_KEY: {"state": "idle", "at": at}, TURN_STATE_AT_META_KEY: at}
    state = dict(doc.state or {})
    meta = state.get("meta")
    state["meta"] = {**(meta if isinstance(meta, dict) else {}), **writes}
    doc.state = state
    doc.seq += 1
    doc.turn_state = "idle"
    doc.turn_state_at = at
    await db.flush()
    op = OpPayload(op_id=f"srv-{uuid4().hex[:12]}", intent="set_meta", meta=writes)
    envelope = DocEnvelope(
        doc_id=str(chat.id),
        doc_type="chat",
        epoch=doc.epoch,
        peer_id=SERVER_PEER_ID,
        seq=doc.seq,
        kind="op",
        payload=op.model_dump(mode="json"),
    )
    await emit(
        db,
        org_id=doc.org_id,
        type=EventType.DOC_OP,
        entity=Entity.DOC,
        entity_id=f"doc:chat:{chat.id}",
        version=doc.seq,
        payload={
            "envelope": envelope.model_dump(mode="json"),
            "team_id": str(doc.team_id) if doc.team_id else None,
            "relay": False,
        },
        actor=dict(actor) if actor is not None else None,
    )
    return doc


async def end_turn_for(
    db: AsyncSession, chat: WorkspaceObject, *, reason: str, actor: Mapping[str, Any] | None
) -> bool:
    """End ``chat``'s running turn and record why: on the chat, so it reads
    "stopped" for ``reason`` until a message is sent after it, and on the
    transcript, so the answer that stopped mid-sentence is followed by the
    reason it stopped, for every reader and for good. The caller holds the
    chat's document and then the chat. True when a turn was ended."""
    doc = await _stamp_idle(db, chat, actor=actor)
    if doc is None:
        return False
    now = datetime.now(UTC)
    chat.spec = {
        **(chat.spec or {}),
        "turn_end_reason": reason,
        "turn_end_at": now.isoformat(),
    }
    await db.flush()
    await publish_server_entries(
        db,
        chat=chat,
        doc=doc,
        entries=system_note_entries(
            session_id=str(chat.id),
            # An aside: the note reports on the machine, so a message still
            # waiting above it stays owed and runs when the chat runs again.
            note_id=aside_note_id(f"turn-end-{uuid4().hex[:12]}"),
            text=turn_end_note(reason),
            now=now,
        ),
        actor=actor,
    )
    return True


def turn_end_note(reason: str) -> str:
    """The transcript's words for a turn the server ended for ``reason``: the
    chat's own status sentence for it, so the pill and the tape say the same
    thing. The machine is not named: the line stays in the transcript for
    good, and which machine served a chat is not every reader's to see."""
    stopped = CHAT_STATUS.states["stopped"]
    code = reason if reason in stopped.reasons else ""
    return CHAT_STATUS.fact("stopped", reason=code, machine=Generic("the machine")).sentence


__all__ = [
    "REFUSED",
    "TURN_STATE_AT_META_KEY",
    "TURN_STATE_META_KEY",
    "end_turn_for",
    "end_turn_nobody_runs",
    "turn_end_note",
]
