"""Deleting a workspace that holds chats: unreachable at once, finished in batches.

A workspace can hold thousands of chats, and ending each one is real work: its
folder's lease ended, its box told, its folder put in the trash, its doorbell
rung. Doing all of it inside the delete request held a lock on every chat row
and every lease for as long as that took, in one transaction that either
committed everything or nothing. So the delete is split in two.

**The request** (:func:`begin`, in the caller's transaction, holding the
workspace row's lock) makes the workspace and every chat in it unreachable in
one statement each: every live chat is tombstoned, stamped with
``ending_workspace_id`` and has its doorbell rung (naming the box that serves
it, with the deletion as the reason), and the workspace is stamped too. Every
reader already hides a tombstone, so nothing can open, list, send to or file
into any of them from the moment the request commits. The caller ends the leases under the
workspace's folder in the same transaction, so a box still serving one of the
chats is fenced on its next beat.

**The background pass** (:func:`finish`, the ``workspace.finish_deletions``
drain) then does the rest, a committed batch at a time: each stamped chat is
ended the way a deleted chat is (asleep, its own folder trashed) and
unstamped; a workspace whose every
chat is finished has its own folder trashed and is unstamped. Every step is
idempotent, so a pass that dies half way is simply run again, and the stamp is
what the progress read counts.

Plain SQL over ``workspace_objects`` where the planner must see the partial
index's predicate literally, so the worker, which does not carry the backend,
runs exactly what it says.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from alkera_core.authz import ActingContext, CredentialKind
from alkera_core.authz.actor_chain import ActorChainRecord
from alkera_core.config import get_settings
from alkera_core.events import (
    ACCESS_CHANGED_KEY,
    BOUND_MACHINE_KEY,
    CHAT_DELETED_REASON,
    Entity,
    EventType,
)
from alkera_core.events.actor import actor_system
from alkera_core.events.types import VISIBILITY_ORG
from alkera_core.logging import get_logger
from alkera_core.models.workspace_object import WorkspaceObject
from alkera_core.objects import chat_end

log = get_logger(__name__)

#: The spec key a row being finished carries: the id of the workspace whose
#: deletion it belongs to. ``ix_workspace_objects_ending`` is partial on it.
ENDING_KEY: Final = "ending_workspace_id"

#: How many chats one pass finishes, each pass committed on its own.
BATCH: Final = 100

#: Every live chat of the workspace tombstoned and stamped, and each one's
#: doorbell rung, in one statement: the doorbell names the box that serves the
#: chat and says the chat was deleted, so that box drops it and leaves nothing
#: of it on its disk, exactly as one deleted on its own is announced.
_TOMBSTONE_CHATS = text(
    """
    WITH ended AS (
        UPDATE workspace_objects
           SET deleted_at = :now, version = version + 1, updated_at = now(),
               spec = spec || jsonb_build_object('ending_workspace_id', CAST(:workspace AS text))
         WHERE org_team_id = :org AND type = 'chat' AND deleted_at = 0
           AND (spec ->> 'workspace_id') = CAST(:workspace AS text)
        RETURNING id, org_team_id, team_id, version, spec ->> 'machine_id' AS machine_id
    )
    INSERT INTO event_outbox (event_id, org_id, type, entity, entity_id, version, actor,
                              visibility, payload)
    SELECT gen_random_uuid(), ended.org_team_id, :event_type, :entity, ended.id::text,
           ended.version, CAST(:actor AS jsonb), :visibility,
           jsonb_build_object(
               'team_id', ended.team_id::text,
               CAST(:machine_key AS text), ended.machine_id,
               'reason', CAST(:reason AS text),
               CAST(:access_key AS text), true
           )
      FROM ended
    """
)

_STAMP_WORKSPACE = text(
    """
    UPDATE workspace_objects
       SET spec = spec || jsonb_build_object('ending_workspace_id', CAST(:workspace AS text))
     WHERE id = :workspace
    RETURNING spec
    """
)

_REMAINING = text(
    """
    SELECT count(*) FROM workspace_objects
     WHERE type = 'chat' AND (spec ->> 'ending_workspace_id') IS NOT NULL
       AND (spec ->> 'ending_workspace_id') = CAST(:workspace AS text)
    """
)

_CHATS_TO_FINISH = text(
    """
    SELECT * FROM workspace_objects
     WHERE type = 'chat' AND (spec ->> 'ending_workspace_id') IS NOT NULL
     ORDER BY id
     LIMIT :batch
       FOR UPDATE SKIP LOCKED
    """
)

_WORKSPACES_TO_FINISH = text(
    """
    SELECT * FROM workspace_objects AS w
     WHERE w.type = 'workspace' AND (w.spec ->> 'ending_workspace_id') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM workspace_objects AS c
            WHERE c.type = 'chat' AND (c.spec ->> 'ending_workspace_id') IS NOT NULL
              AND (c.spec ->> 'ending_workspace_id') = w.id::text
       )
     ORDER BY w.id
     LIMIT :batch
       FOR UPDATE SKIP LOCKED
    """
)


async def begin(
    db: AsyncSession,
    *,
    workspace: WorkspaceObject,
    now: datetime,
    actor: Mapping[str, Any] | None,
) -> int:
    """Tombstone and stamp every live chat in ``workspace`` and ring each
    one's doorbell, then stamp the workspace itself; how many chats were
    ended. In the caller's transaction, which already holds the workspace
    row's lock and tombstones the row.

    One statement for the chats, served by the workspace's chat index, so the
    request is bounded however many chats the workspace holds.
    """
    ended = await db.execute(
        _TOMBSTONE_CHATS,
        {
            "now": now.timestamp(),
            "org": workspace.org_team_id,
            "workspace": str(workspace.id),
            "event_type": EventType.CHAT_UPDATED.value,
            "entity": str(Entity.CHAT),
            "actor": json.dumps(_actor_document(actor)),
            "visibility": VISIBILITY_ORG,
            "machine_key": BOUND_MACHINE_KEY,
            "reason": CHAT_DELETED_REASON,
            "access_key": ACCESS_CHANGED_KEY,
        },
    )
    count = int(getattr(ended, "rowcount", 0) or 0)
    if count:
        stamped = (await db.execute(_STAMP_WORKSPACE, {"workspace": workspace.id})).one()
        set_committed_value(workspace, "spec", stamped.spec)
    return count


def _actor_document(actor: Mapping[str, Any] | None) -> dict[str, Any]:
    """The actor the doorbells carry, in the outbox's own document shape."""
    if actor is None:
        return actor_system("workspace_end")
    return ActorChainRecord.model_validate(dict(actor)).model_dump(mode="json")


async def chats_remaining(db: AsyncSession, workspace_id: UUID) -> int:
    """How many chats of ``workspace_id``'s deletion the background pass has
    yet to finish; ``0`` once it has finished them all (or there were none)."""
    return int(await db.scalar(_REMAINING, {"workspace": str(workspace_id)}) or 0)


def is_ending(workspace: WorkspaceObject) -> bool:
    """Whether ``workspace`` was deleted and its deletion is still being finished."""
    return bool((workspace.spec or {}).get(ENDING_KEY))


@dataclass(frozen=True, slots=True)
class Finished:
    """What one pass did."""

    chats: int = 0
    workspaces: int = 0
    failed: int = 0


def _ctx_for(obj: WorkspaceObject) -> ActingContext:
    """The context a trashed folder's history is filed under: the object's
    owner, as an ordinary delete files it, not the worker that ran it."""
    return ActingContext.for_service(
        token_id=obj.owner_user_id,
        org_id=obj.org_team_id,
        label="workspace_end",
        credential=CredentialKind.CI_TOKEN,
    )


async def _trash_folder(db: AsyncSession, obj: WorkspaceObject) -> None:
    """Trash ``obj``'s own folder the way a deleted object's goes: restorable
    from its owner's Trash as an ordinary folder. Nothing when Files is off or
    the folder is already gone."""
    if not get_settings().files_enabled:
        return
    from alkera_core.files.ids import OrgScope
    from alkera_core.files.objects_bridge import tombstone_for_object
    from alkera_core.files.repo import FilesRepo

    repo = FilesRepo.joined(db, OrgScope(org_team_id=obj.org_team_id))
    async with repo.transaction():
        await tombstone_for_object(repo, _ctx_for(obj), obj.id)


def _unstamped(spec: dict[str, object] | None) -> dict[str, object]:
    return {key: value for key, value in (spec or {}).items() if key != ENDING_KEY}


async def _finish_chat(db: AsyncSession, chat: WorkspaceObject) -> None:
    """Finish one chat of a deleted workspace as a deleted chat ends: asleep
    with every lease on its folder ended, its folder trashed; then unstamped.
    Its doorbell already rang in the request that deleted it."""
    await chat_end.end_chat(
        db, chat.id, chat_end.ChatEndReason.DELETED, actor=None, announce=False, locked=chat
    )
    await _trash_folder(db, chat)
    chat.spec = _unstamped(chat.spec)
    await db.flush()


async def _finish_workspace(db: AsyncSession, workspace: WorkspaceObject) -> None:
    """Trash the folder of a deleted workspace whose every chat is finished,
    and unstamp it: its deletion is complete."""
    await _trash_folder(db, workspace)
    workspace.spec = _unstamped(workspace.spec)
    await db.flush()


async def finish(
    db: AsyncSession,
    *,
    batch: int = BATCH,
    heartbeat: Callable[[], None] | None = None,
) -> Finished:
    """One bounded pass in the caller's transaction: finish up to ``batch``
    stamped chats, then every deleted workspace with none left.

    Rows another pass holds are skipped, not waited on. Each chat runs in a
    savepoint of its own: one that fails is logged, keeps its stamp for the
    next pass, and does not undo the others.
    """
    done = failed = 0
    chats = (
        (
            await db.execute(
                select(WorkspaceObject).from_statement(_CHATS_TO_FINISH), {"batch": batch}
            )
        )
        .scalars()
        .all()
    )
    for chat in chats:
        try:
            async with db.begin_nested():
                await _finish_chat(db, chat)
        except Exception as exc:
            failed += 1
            log.warning(
                "workspace.end.chat_failed",
                chat_id=str(chat.id),
                error=str(exc) or type(exc).__name__,
            )
            await db.refresh(chat)
        else:
            done += 1
        if heartbeat is not None:
            heartbeat()
    workspaces = (
        (
            await db.execute(
                select(WorkspaceObject).from_statement(_WORKSPACES_TO_FINISH), {"batch": batch}
            )
        )
        .scalars()
        .all()
    )
    for workspace in workspaces:
        await _finish_workspace(db, workspace)
    return Finished(chats=done, workspaces=len(workspaces), failed=failed)


__all__ = [
    "BATCH",
    "ENDING_KEY",
    "Finished",
    "begin",
    "chats_remaining",
    "finish",
    "is_ending",
]
