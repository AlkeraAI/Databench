"""The one way a chat stops being served: every ending goes through here.

A chat is served by the box that holds its folder's lease. It stops being
served for many reasons — the box put it to sleep because it went quiet or
needed the slot, a person deleted it, the box went to sleep or left the plane,
placement moved it to another box, its owner lost access — and each of those
used to be its own code path, each deciding for itself what happened to the
folder lease. The one that forgot left a deleted chat's folder held by a box
for a whole TTL, and "Delete forever" was refused as somebody's local use of a
folder nobody could open.

So an ending is one transition, :func:`end_chat`, and the reasons are
registered rather than coded:

* **the lease ends, server-side, in the caller's transaction.** The row is
  stamped released, which is the fence: the holder's next beat is refused, its
  next fenced write is refused, and its re-take (``resume``) is refused as
  ``files.lease_ended`` — so a box that never heard about the ending finds out
  from the first thing it sends, and drops the chat without pushing;
* **the chat reads asleep**, and ``end_seq`` moves, so a box that reads the row
  (a frame, or its next discovery pass) knows the chat it holds was ended under
  it rather than slept by itself;
* **the chat's doorbell rings**, naming the box it is bound to, so that box
  hears about it at once;
* **a turn the box was running ends with it** when the box is gone with the
  ending, so the chat stops reading "Working…" for a turn nothing runs.

A reason is registered with :func:`register`. What varies between reasons is
data on the :class:`Ending`, never a branch at a call site: whether watchers
must be decided again (a deletion, an owner losing access), and whether a
holder that is still serving may keep its lease to finish (a move off a box
that is draining, whose last push is the only copy of its last turn).

Lock order: the chat row, then the lease rows under its folder — the same as a
chat write that then touches Files. The transition bumps no ``version``: an
ending is not an edit anybody can lose a race to.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.db.errors import sqlstate_of
from alkera_core.db.locking import LockRank, hold_place, holds_place, lock_rows
from alkera_core.events import ACCESS_CHANGED_KEY, BOUND_MACHINE_KEY, Entity, EventType, emit
from alkera_core.files.objects_bridge import WORKSPACE_TYPE
from alkera_core.files.workspace_identity import chat_workspace_id
from alkera_core.logging import get_logger
from alkera_core.models.realtime_doc import RealtimeDoc
from alkera_core.models.workspace_object import WorkspaceObject
from alkera_core.objects import chat_turn
from alkera_core.objects.workspaces import forget_box_report, workspace_spec_of

log = get_logger(__name__)

CHAT_TYPE = "chat"


class ChatEndReason(StrEnum):
    """Why a chat stopped being served. The wire spelling of each is its value."""

    #: The box closed it: nothing had run in it for the idle window.
    IDLE = "idle"
    #: The box closed it to free a slot for another chat.
    EVICTED = "evicted"
    #: The box closed it on its way out: draining, or stopping.
    DRAINED = "drained"
    #: A person put it to sleep.
    USER_SLEEP = "user_sleep"
    #: A person deleted it, or a spare nobody claimed was reaped.
    DELETED = "deleted"
    #: The box it is bound to was put to sleep.
    BOX_ASLEEP = "box_asleep"
    #: The box it is bound to left the plane: terminated, failed, lost.
    BOX_LOST = "box_lost"
    #: Placement moved it to another box.
    MOVED = "moved"
    #: Its owner was deactivated or removed.
    ACCESS_REMOVED = "access_removed"
    #: The machine it runs on was stopped because its funding ran out.
    CREDITS_EXHAUSTED = "credits_exhausted"
    #: The machine it runs on was stopped: by a person, for idling, for its
    #: spend cap, or because its free period ended.
    MACHINE_STOPPED = "machine_stopped"


@dataclass(frozen=True, slots=True)
class Ending:
    """What one registered reason does beyond the common transition."""

    reason: ChatEndReason
    #: Anyone watching the chat must be decided again, not just told to re-read:
    #: the chat went away, or the person it belonged to did.
    access_changed: bool = False
    #: A holder that is still serving may keep its lease to finish — only a
    #: move, whose departing box hands the folder back itself (its last push is
    #: the only copy of its last turn) and is fenced out by the next grant.
    spares_serving_holder: bool = False
    #: The chat reads asleep afterwards. Every ending but a move: a moved chat
    #: is bound to a box that has not said anything about it yet, and the
    #: rebind has already cleared the departed box's word — recording "asleep"
    #: would put a session state on a box that never held the chat.
    records_asleep: bool = True
    #: The box that ran the chat is gone with the ending (its machine stopped
    #: or left the plane), so nothing will write the end of a turn it was
    #: running: the ending writes it (:mod:`alkera_core.objects.chat_turn`).
    #: A box that closes a chat itself ends its own turn first.
    ends_turn: bool = False
    #: The chat's workspace goes with it: a move. The box it left reported
    #: holding the workspace, and that report is dropped once no live chat of
    #: the workspace is bound to that box any more, so the box it left is not
    #: still named the workspace's holder (which admits it to the workspace's
    #: connections and their credentials).
    moves_workspace: bool = False


_ENDINGS: dict[ChatEndReason, Ending] = {}


def register(ending: Ending) -> Ending:
    """Register what a reason does. A reason is registered once."""
    if ending.reason in _ENDINGS:
        raise ValueError(f"chat ending {ending.reason!r} is already registered")
    _ENDINGS[ending.reason] = ending
    return ending


def ending_for(reason: ChatEndReason | str) -> Ending:
    """The registered ending for ``reason``; an unregistered one is refused."""
    try:
        return _ENDINGS[ChatEndReason(reason)]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"no chat ending is registered for {reason!r}") from exc


def registered() -> Mapping[ChatEndReason, Ending]:
    return dict(_ENDINGS)


register(Ending(ChatEndReason.IDLE))
register(Ending(ChatEndReason.EVICTED))
register(Ending(ChatEndReason.DRAINED))
register(Ending(ChatEndReason.USER_SLEEP))
register(Ending(ChatEndReason.DELETED, access_changed=True))
register(Ending(ChatEndReason.BOX_ASLEEP, ends_turn=True))
register(Ending(ChatEndReason.BOX_LOST, ends_turn=True))
register(
    Ending(
        ChatEndReason.MOVED,
        spares_serving_holder=True,
        records_asleep=False,
        moves_workspace=True,
    )
)
register(Ending(ChatEndReason.ACCESS_REMOVED, access_changed=True))
register(Ending(ChatEndReason.CREDITS_EXHAUSTED, ends_turn=True))
register(Ending(ChatEndReason.MACHINE_STOPPED, ends_turn=True))


@dataclass(frozen=True, slots=True)
class ChatEnded:
    """What one transition did."""

    chat_id: uuid.UUID
    reason: ChatEndReason
    #: The folders whose lease this transition ended.
    released: tuple[uuid.UUID, ...] = ()
    #: Whether the chat's row changed (and its doorbell rang).
    changed: bool = False


async def lock_chat_for_write(
    db: AsyncSession, chat_id: uuid.UUID, *, org_team_id: uuid.UUID
) -> WorkspaceObject | None:
    """Hold everything one write to a chat touches, in the ONE order.

    A chat has two rows a writer serialises on: its realtime document, and the
    chat object itself (which is where a transcript sequence is assigned). The
    order is **the document first, then the chat** — the order a socket's op
    already takes them in, because the registry locks the document before the
    transcript write locks the chat.

    Every other writer has to take them the same way round, and that is why
    this exists rather than each caller locking what it happens to need. A
    reader's answer or Stop that took the chat row first and then reached for
    the document would hold exactly what a box appending over its socket was
    waiting for, while the box held what it was waiting for: Postgres breaks
    the cycle by aborting one of them, and the one it aborts is the request —
    so the reader is told their answer could not be sent, over a chat that was
    working perfectly.

    A caller that has been through here may call again rather than reason
    about who locked what: the second time takes the chat alone. It must not
    reach for the document again, because a chat that had none the first time
    may have been given one since, by a socket that now holds it and waits for
    the chat this transaction holds. Returns the locked chat, or ``None`` when
    there is no such chat; a chat with no document yet has nobody subscribed
    and nothing to lock on that side.
    """
    if not holds_place(db, LockRank.REALTIME_DOC, chat_id):
        await lock_rows(
            db,
            LockRank.REALTIME_DOC,
            select(RealtimeDoc)
            .where(
                RealtimeDoc.org_id == org_team_id,
                RealtimeDoc.doc_type == "chat",
                RealtimeDoc.doc_id == str(chat_id),
            )
            # Read the row as the lock finds it: a session that already loaded
            # the document would otherwise be handed the copy it read BEFORE
            # the lock, and build its write on a window a committed writer has
            # since moved.
            .execution_options(populate_existing=True),
        )
        # The document's step is done whether or not there was one to lock, so
        # the next chat this transaction ends is one more of the same order.
        hold_place(db, LockRank.REALTIME_DOC, chat_id)
    return (
        await lock_rows(
            db,
            LockRank.WORKSPACE_OBJECT,
            select(WorkspaceObject)
            .where(WorkspaceObject.id == chat_id, WorkspaceObject.deleted_at == 0)
            .execution_options(populate_existing=True),
        )
    ).scalar_one_or_none()


async def lock_chat(db: AsyncSession, chat_id: uuid.UUID) -> WorkspaceObject | None:
    """The live chat row, locked for update after its realtime document — the
    first locks every ending takes, in the order every writer of a chat takes
    them (:func:`lock_chat_for_write`). An ending goes on to stamp the
    document idle, and a chat locked before it would hold what a socket's op
    waits for while waiting for the document that op holds. A caller about to
    touch the chat's folder lease itself takes these first too, so the two
    orders cannot cross."""
    org_team_id = (
        await db.execute(
            select(WorkspaceObject.org_team_id).where(
                WorkspaceObject.id == chat_id, WorkspaceObject.type == CHAT_TYPE
            )
        )
    ).scalar_one_or_none()
    if org_team_id is None:
        return None
    chat = await lock_chat_for_write(db, chat_id, org_team_id=org_team_id)
    return chat if chat is not None and chat.type == CHAT_TYPE else None


async def _end_folder_lease(
    db: AsyncSession, chat_id: uuid.UUID, *, spare: uuid.UUID | None
) -> tuple[uuid.UUID, ...]:
    """End every live lease on the chat's folder and anything under it, except
    one held by ``spare`` as a machine. Returns the folders ended.

    The released stamp is the fence: a beat, a fenced write and a resuming
    re-take all match only a lease that is not released.
    """
    from alkera_core.files.repo import SUBTREE_DEPTH_BIND, FilesRepo, subtree_sql

    folder = await FilesRepo.chat_folder_anywhere(db, chat_id)
    if folder is None:
        return ()
    rows = (
        await db.execute(
            text(
                "UPDATE file_leases SET released_at = now(), grantable_after = now() "  # noqa: S608 - interpolates the subtree builder's column names only
                "WHERE org_team_id = :org "
                "AND released_at IS NULL AND reaped_at IS NULL AND expires_at > now() "
                "AND NOT (CAST(:spare AS uuid) IS NOT NULL AND holder_kind = 'machine' "
                "AND holder_principal_id = CAST(:spare AS uuid)) "
                "AND node_id IN (SELECT n.id FROM file_nodes n WHERE n.org_team_id = :org "
                f"AND {subtree_sql('n.path_ids', 'CAST(:path AS ltree)')}) "
                "RETURNING node_id"
            ),
            {
                "org": folder.org_team_id,
                "path": folder.path_ids,
                "spare": str(spare) if spare is not None else None,
                **SUBTREE_DEPTH_BIND,
            },
        )
    ).all()
    return tuple(uuid.UUID(str(row.node_id)) for row in rows)


async def end_chat(
    db: AsyncSession,
    chat_id: uuid.UUID,
    reason: ChatEndReason | str,
    *,
    actor: Mapping[str, Any] | None,
    spare_serving_holder: uuid.UUID | None = None,
    announce: bool = True,
    locked: WorkspaceObject | None = None,
) -> ChatEnded:
    """End ``chat_id``'s service for ``reason``, in the caller's transaction.

    Idempotent: a chat already asleep with no live lease changes nothing and
    rings nothing. A chat that is gone (deleted, or never a chat) is a no-op.

    ``spare_serving_holder`` names a machine whose lease is left to it because
    it is still serving and will hand the folder back itself; only a reason
    registered with ``spares_serving_holder`` may name one. The chat is then
    still recorded asleep, but ``end_seq`` does not move — the holder is
    finishing, not being told to stop.

    ``announce`` False is a caller that rings the chat's doorbell itself in the
    same transaction (a move, a machine restating its chats, a deletion): one
    change, one frame. ``locked`` is the chat's row when the caller already
    holds its lock (a move, which rebinds the row first), so a pass that moves
    many chats takes one lock per chat, not two.
    """
    ending = ending_for(reason)
    if spare_serving_holder is not None and not ending.spares_serving_holder:
        raise ValueError(f"a {ending.reason} ending ends every lease on the chat's folder")
    chat = locked if locked is not None and locked.id == chat_id else await lock_chat(db, chat_id)
    if chat is None:
        return ChatEnded(chat_id=chat_id, reason=ending.reason)
    released = await _end_folder_lease(db, chat_id, spare=spare_serving_holder)
    spec = dict(chat.spec or {})
    before = dict(spec)
    if ending.records_asleep:
        spec["mirror_state"] = "asleep"
        # The box held the chat until now, so any wake standing was answered;
        # left standing (its ``awake`` report lost) it re-took the chat after
        # every sleep with nobody reading.
        spec.pop("wake_requested_at", None)
    if released or (before.get("mirror_state") == "awake" and spare_serving_holder is None):
        spec["end_seq"] = int(before.get("end_seq") or 0) + 1
        spec["ended_reason"] = ending.reason.value
    changed = spec != before
    if changed:
        chat.spec = spec
        await db.flush()
    if ending.moves_workspace:
        await _drop_departed_report(db, chat)
    if ending.ends_turn and await chat_turn.end_turn_for(
        db, chat, reason=ending.reason.value, actor=actor
    ):
        # A turn was running and its box is gone: the chat says why it
        # stopped until somebody sends it something again.
        changed = True
    if changed and announce:
        payload: dict[str, Any] = {
            "team_id": str(chat.team_id) if chat.team_id else None,
            BOUND_MACHINE_KEY: spec.get("machine_id"),
        }
        if ending.access_changed:
            payload[ACCESS_CHANGED_KEY] = True
        await emit(
            db,
            org_id=chat.org_team_id,
            type=EventType.CHAT_UPDATED,
            entity=Entity.CHAT,
            entity_id=str(chat.id),
            version=chat.version,
            payload=payload,
            actor=dict(actor) if actor else None,
        )
    if released:
        log.info(
            "chat.ended",
            chat_id=str(chat_id),
            reason=ending.reason.value,
            leases_released=len(released),
        )
    return ChatEnded(chat_id=chat_id, reason=ending.reason, released=released, changed=changed)


#: How long a move waits for its workspace's row before it gives way: under
#: Postgres's default ``deadlock_timeout`` (one second), so a move crossed with
#: a workspace delete times out before the deadlock check would pick a victim.
REPORT_LOCK_WAIT_MS: Final = 500
#: A lock wait that timed out, and a deadlock the check broke on this side.
_GAVE_WAY: Final = frozenset({"55P03", "40P01"})


async def _drop_departed_report(db: AsyncSession, chat: WorkspaceObject) -> None:
    """Drop the report of the box ``chat`` was moved off, on the chat's
    workspace, once that box holds no live chat of the workspace.

    The chat is already rebound, so "no live chat of the workspace is bound to
    the reporter" is read off the rows as they stand.

    A workspace another writer holds is waited for, briefly, in a savepoint:
    a rename or a box's own report lets go in milliseconds, and the drop lands.
    The wait is shorter than Postgres's deadlock check on purpose: a delete
    takes the workspace's lock before each chat's, and the caller holds this
    chat's, so against a delete this wait is the one that gives way (the
    workspace is going anyway) and the delete is never the one aborted. A drop
    that still could not land is logged; the workspace's doors read the hold
    off the chats and the lease, and a box the workspace moved off cannot
    keep the lease (its beat forces it), so a report left behind admits
    nobody past one lease TTL.
    """
    workspace_id = chat_workspace_id(chat)
    if workspace_id is None:
        return
    try:
        async with db.begin_nested():
            workspace = (
                await lock_rows(
                    db,
                    LockRank.WORKSPACE_OBJECT,
                    select(WorkspaceObject)
                    .where(
                        WorkspaceObject.id == workspace_id,
                        WorkspaceObject.org_team_id == chat.org_team_id,
                        WorkspaceObject.type == WORKSPACE_TYPE,
                        WorkspaceObject.deleted_at == 0,
                    )
                    .execution_options(populate_existing=True),
                    timeout_ms=REPORT_LOCK_WAIT_MS,
                )
            ).scalar_one_or_none()
    except DBAPIError as exc:
        if sqlstate_of(exc) not in _GAVE_WAY:
            raise
        log.warning(
            "chat.moved.workspace_report_kept",
            chat_id=str(chat.id),
            workspace_id=str(workspace_id),
            sqlstate=sqlstate_of(exc),
        )
        return
    if workspace is None:
        return
    spec = workspace_spec_of(workspace.spec)
    reporter = spec.machine_id
    if spec.binding_authority != "workspace" or not reporter:
        return
    bound_to = WorkspaceObject.spec["machine_id"].astext
    still_held = (
        await db.execute(
            select(WorkspaceObject.id)
            .where(
                WorkspaceObject.org_team_id == chat.org_team_id,
                WorkspaceObject.type == CHAT_TYPE,
                WorkspaceObject.deleted_at == 0,
                WorkspaceObject.spec["workspace_id"].astext == str(workspace_id),
                bound_to == reporter,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if still_held is not None:
        return
    workspace.spec = forget_box_report(spec).model_dump(mode="json")
    await db.flush()


async def end_chats(
    db: AsyncSession,
    chat_ids: Sequence[uuid.UUID],
    reason: ChatEndReason | str,
    *,
    actor: Mapping[str, Any] | None,
    announce: bool = True,
) -> list[ChatEnded]:
    """:func:`end_chat` for each of ``chat_ids``, oldest id order so two
    callers ending overlapping sets lock their rows in the same order."""
    return [
        await end_chat(db, chat_id, reason, actor=actor, announce=announce)
        for chat_id in sorted(set(chat_ids), key=str)
    ]


async def live_chats_bound_to(db: AsyncSession, machine_id: uuid.UUID) -> list[uuid.UUID]:
    async with cross_tenant_write(db, reason="compute.machine_chats.live"):
        bound_to = WorkspaceObject.spec["machine_id"].astext
        rows = await db.execute(
            select(WorkspaceObject.id).where(
                WorkspaceObject.type == CHAT_TYPE,
                WorkspaceObject.deleted_at == 0,
                bound_to == str(machine_id),
            )
        )
        return list(rows.scalars().all())


async def live_chats_owned_by(
    db: AsyncSession, user_id: uuid.UUID, *, org_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """The live chats ``user_id`` owns: in ``org_id`` alone when one is named
    (an org ending a person's access ends only what they run there), else in
    every org (the identity itself going away)."""
    stmt = select(WorkspaceObject.id).where(
        WorkspaceObject.type == CHAT_TYPE,
        WorkspaceObject.deleted_at == 0,
        WorkspaceObject.owner_user_id == user_id,
    )
    if org_id is not None:
        stmt = stmt.where(WorkspaceObject.org_team_id == org_id)
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


__all__ = [
    "ChatEndReason",
    "ChatEnded",
    "Ending",
    "end_chat",
    "end_chats",
    "ending_for",
    "live_chats_bound_to",
    "live_chats_owned_by",
    "lock_chat",
    "register",
    "registered",
]
