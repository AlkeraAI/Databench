"""What the lane tells every replica's sockets without the event outbox.

Every outbox commit rings a ``NOTIFY``, and Postgres lets one notifying
transaction commit at a time per database, holding that lock through its WAL
flush: at keystroke rates every writer would queue behind it. These
announcements are notify-only transactions that do not wait for the WAL
instead. They can be lost (a crash, a listener reconnecting); what they carry
is durable elsewhere, and a tab that misses one finds out another way.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final, cast

from alkera_core.events import HubEvent
from alkera_core.events.listener import EPHEMERAL_CHANNEL, ephemeral_payload
from alkera_core.events.types import Entity
from alkera_core.logging import get_logger
from alkera_core.schemas.realtime import SERVER_PEER_ID, CrdtSavingPayload, DocEnvelope, DocType
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.crdt.errors import safe_error
from backend.services.crdt.registry import CrdtDocType, DocRef

if TYPE_CHECKING:
    from backend.services.crdt.docs import Applied

log = get_logger(__name__)

#: The hub event a committed update (or a saving notice) becomes on every replica.
CRDT_UPDATE_EVENT_TYPE: Final = "doc.crdt_update"


async def _notify(factory: Callable[[], AsyncSession], encoded: str) -> None:
    async with factory() as db:
        await db.execute(text("SET LOCAL synchronous_commit = off"))
        await db.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": EPHEMERAL_CHANNEL, "payload": encoded},
        )
        await db.commit()


def broadcast_event(ref: DocRef, applied: Applied, envelope: DocEnvelope) -> HubEvent:
    payload: dict[str, Any] = {"envelope": envelope.model_dump(mode="json")}
    if applied.team_id is not None:
        payload["team_id"] = str(applied.team_id)
    return HubEvent(
        lane="ephemeral",
        org_id=ref.org_id,
        type=CRDT_UPDATE_EVENT_TYPE,
        entity=Entity.DOC.value,
        entity_id=ref.channel,
        version=applied.log_seq,
        visibility=applied.visibility,
        payload=payload,
        id=None,
        channel=ref.channel,
    )


async def broadcast(factory: Callable[[], AsyncSession], ref: DocRef, applied: Applied) -> None:
    """Announce a committed update to every replica's sockets.

    The update is durable in ``crdt_updates`` when this runs; a tab that
    misses the announcement finds the gap at its next import, or at its idle
    check, and resyncs by version vector. Nothing acknowledged is lost. An
    update too large for one notification goes by reference and is read from
    the log; one whose version vector alone is too large goes without it (a
    tab imports the delta and never reads the vector off a broadcast). Never
    raises."""
    envelope = applied.envelope
    if not applied.changed or envelope is None:
        return
    referenced = {
        **{k: v for k, v in envelope.payload.items() if k != "data_b64"},
        "log_ref": applied.log_seq,
    }
    encoded: str | None = None
    for payload in (
        envelope.payload,
        referenced,
        {k: v for k, v in referenced.items() if k != "vv_b64"},
    ):
        try:
            encoded = ephemeral_payload(
                broadcast_event(ref, applied, envelope.model_copy(update={"payload": payload}))
            )
        except ValueError:
            continue
        break
    if encoded is None:
        log.warning("crdt.broadcast.too_large", doc=ref.key, log_seq=applied.log_seq)
        return
    try:
        await _notify(factory, encoded)
    except Exception as exc:  # the tabs resync from their vectors
        log.warning("crdt.broadcast.failed", doc=ref.key, error=safe_error(exc))


async def saving(
    factory: Callable[[], AsyncSession],
    doc_type: CrdtDocType,
    ref: DocRef,
    *,
    epoch: int,
    paused: bool,
    reason: str = "",
) -> None:
    """Tell every subscriber whether the document's edits are reaching its
    source (``paused``, with why, or landing again). Each socket that holds
    the document re-reads its grant on it. Never raises: a missed one is said
    again by the next write back."""
    envelope = DocEnvelope(
        doc_id=ref.doc_id,
        doc_type=cast(DocType, ref.doc_type),
        epoch=max(epoch, 1),
        peer_id=SERVER_PEER_ID,
        seq=0,
        kind="crdt",
        payload=CrdtSavingPayload(
            state="paused" if paused else "ok", reason=reason[:64] if paused else ""
        ).model_dump(mode="json"),
    )
    payload: dict[str, Any] = {"envelope": envelope.model_dump(mode="json")}
    try:
        async with factory() as db:
            team = await doc_type.team_of(db, ref=ref)
        if team is not None:
            payload["team_id"] = str(team)
        event = HubEvent(
            lane="ephemeral",
            org_id=ref.org_id,
            type=CRDT_UPDATE_EVENT_TYPE,
            entity=Entity.DOC.value,
            entity_id=ref.channel,
            version=0,
            visibility="org",
            payload=payload,
            id=None,
            channel=ref.channel,
        )
        await _notify(factory, ephemeral_payload(event))
    except Exception as exc:
        log.warning("crdt.saving.announce_failed", doc=ref.key, error=safe_error(exc))


__all__ = ["CRDT_UPDATE_EVENT_TYPE", "broadcast", "broadcast_event", "saving"]
