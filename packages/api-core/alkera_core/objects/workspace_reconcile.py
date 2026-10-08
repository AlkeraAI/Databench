"""The periodic pass that keeps every chat in a workspace and no workspace of
one outliving its chat.

Reading or listing a chat heals a stray on the spot, but a chat nobody opens is
never read. During a roll a task on the previous build can still create a chat
with no workspace, or delete one without ending its workspace of one, and an
old worker can erase a chat row the same way. This pass catches both, a
bounded batch at a time:

* a live chat that names no workspace is adopted into a workspace of one,
  minted in the ``workspace_of_chat`` namespace keyed by the chat's id, the same
  idempotent write the application makes, and announced with ``chat.updated``
  as a heal on read is;
* a live workspace of one whose chat is gone (tombstoned or erased) is
  tombstoned. It owns no folder, so nothing in Files moves.

This pass is also how the chats that existed before workspaces are adopted:
the schema revision that brings workspaces in only widens the table, because
a task of the previous build cannot list a workspace row, and the deploy keeps
``workspaces_adoption_enabled`` off until no such task is serving.
:func:`drain` walks that backlog a committed batch at a time. With the switch
off nothing is adopted; orphaned workspaces of one are still retired, since
retiring makes no workspace row.

Plain SQL over ``workspace_objects`` so the worker, which does not carry the
backend, runs exactly what it says.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.config import get_settings
from alkera_core.events import BOUND_MACHINE_KEY, Entity, EventType, emit
from alkera_core.objects.workspaces import ADOPTED_NAMESPACE

#: How many rows one pass touches per step, so a backlog drains over passes
#: rather than holding locks on thousands of rows at once.
BATCH: Final = 500

#: How many committed passes one :func:`drain` runs at most: a backlog of
#: twenty thousand chats a run, the rest left for the next run.
MAX_PASSES: Final = 40

_ADOPT_STRAYS = text(
    f"""
    WITH batch AS (
        SELECT c.id, c.org_team_id, c.owner_user_id, c.team_id, c.visibility_scope,
               c.title, c.content_updated_at, c.created_at
          FROM workspace_objects AS c
         WHERE c.type = 'chat' AND c.deleted_at = 0 AND (c.spec ->> 'workspace_id') IS NULL
           -- A chat whose workspace of one was deleted is not brought back (deleting
           -- a workspace ends its chats), and leaving it out keeps it from holding
           -- a place in every batch: every chat selected here is adopted.
           AND NOT EXISTS (
               SELECT 1 FROM workspace_objects AS d
                WHERE d.org_team_id = c.org_team_id AND d.namespace = '{ADOPTED_NAMESPACE}'
                  AND d.logical_id = c.id::text AND d.deleted_at <> 0
           )
         ORDER BY c.id
         LIMIT :batch
    ),
    made AS (
        INSERT INTO workspace_objects (
            id, org_team_id, logical_id, namespace, type, title, version, status, spec,
            owner_user_id, team_id, visibility_scope, content_updated_at, deleted_at,
            created_at, updated_at
        )
        SELECT gen_random_uuid(), b.org_team_id, b.id::text, '{ADOPTED_NAMESPACE}', 'workspace',
               b.title, 1, 'ready',
               jsonb_build_object(
                   'schema_version', '1.0.0',
                   'kind', 'project',
                   'layout', 'adopted',
                   'adopted_chat_id', b.id::text
               ),
               b.owner_user_id, b.team_id, b.visibility_scope, b.content_updated_at, 0,
               b.created_at, now()
          FROM batch AS b
        ON CONFLICT (org_team_id, namespace, logical_id) DO NOTHING
        RETURNING id, org_team_id, logical_id
    ),
    linked AS (
        SELECT b.id AS chat_id, COALESCE(m.id, w.id) AS workspace_id
          FROM batch AS b
          LEFT JOIN made AS m
            ON m.org_team_id = b.org_team_id AND m.logical_id = b.id::text
          LEFT JOIN workspace_objects AS w
            ON w.org_team_id = b.org_team_id AND w.namespace = '{ADOPTED_NAMESPACE}'
           AND w.logical_id = b.id::text AND w.type = 'workspace' AND w.deleted_at = 0
    )
    UPDATE workspace_objects AS c
       SET spec = c.spec || jsonb_build_object('workspace_id', l.workspace_id::text)
      FROM linked AS l
     WHERE c.id = l.chat_id AND l.workspace_id IS NOT NULL
    RETURNING c.id, c.org_team_id, c.version, c.team_id, c.spec ->> 'machine_id' AS machine_id
    """  # noqa: S608 - the one interpolated value is this package's own constant
)

#: A logical id that is a chat id, so the anti-join below is a primary-key
#: lookup per workspace rather than a comparison against every chat as text.
_UUID_TEXT = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

_RETIRE_ORPHANS = text(
    f"""
    UPDATE workspace_objects AS w
       SET deleted_at = :now, version = w.version + 1, updated_at = now()
     WHERE w.id IN (
        SELECT o.id
          FROM workspace_objects AS o
         WHERE o.type = 'workspace' AND o.namespace = '{ADOPTED_NAMESPACE}'
           AND o.deleted_at = 0
           -- Keyed by its chat's id (the logical id), read off the primary key:
           -- a logical id that is not a uuid finds no chat, so a row a writer
           -- mangled is retired instead of failing the cast and every later pass.
           AND NOT EXISTS (
               SELECT 1 FROM workspace_objects AS c
                WHERE c.id = (CASE WHEN o.logical_id ~ '{_UUID_TEXT}'
                                   THEN CAST(o.logical_id AS uuid) END)
                  AND c.org_team_id = o.org_team_id
                  AND c.type = 'chat' AND c.deleted_at = 0
           )
         ORDER BY o.id
         LIMIT :batch
     )
    """  # noqa: S608 - the one interpolated value is this package's own constant
)


@dataclass(frozen=True, slots=True)
class Reconciled:
    """What one pass did."""

    adopted: int
    retired: int


async def reconcile(
    db: AsyncSession, *, now: datetime, batch: int = BATCH, adopt: bool | None = None
) -> Reconciled:
    """One bounded pass in the caller's transaction: adopt up to ``batch``
    chats with no workspace and retire up to ``batch`` orphaned workspaces of
    one. Safe to run twice: a chat adopted once names its workspace, and a
    retired workspace is no longer live.

    ``adopt`` defaults to ``workspaces_adoption_enabled``; off, the pass only
    retires."""
    if adopt is None:
        adopt = get_settings().workspaces_adoption_enabled
    adopted = (await db.execute(_ADOPT_STRAYS, {"batch": batch})).all() if adopt else []
    for chat in adopted:
        # The chat's row changed: anyone holding it re-reads it, as after a
        # heal on read. A doorbell naming the chat, no content.
        await emit(
            db,
            org_id=chat.org_team_id,
            type=EventType.CHAT_UPDATED,
            entity=Entity.CHAT,
            entity_id=str(chat.id),
            version=chat.version,
            payload={
                "team_id": str(chat.team_id) if chat.team_id else None,
                BOUND_MACHINE_KEY: chat.machine_id,
            },
            flush=False,
        )
    if adopted:
        await db.flush()
    retired = await db.execute(_RETIRE_ORPHANS, {"batch": batch, "now": now.timestamp()})
    return Reconciled(
        adopted=len(adopted),
        retired=int(getattr(retired, "rowcount", 0) or 0),
    )


async def drain(
    sessions: Callable[[], AsyncSession],
    *,
    now: datetime,
    batch: int = BATCH,
    max_passes: int = MAX_PASSES,
    adopt: bool | None = None,
) -> Reconciled:
    """Run :func:`reconcile` pass after pass, each in a session of its own and
    committed, until a pass finds less than a full batch of either kind or
    ``max_passes`` ran: what they did between them.

    Each pass commits before the next starts, so no lock outlives one batch
    and a failure keeps what the earlier passes did."""
    adopted = retired = 0
    for _ in range(max_passes):
        async with sessions() as session:
            done = await reconcile(session, now=now, batch=batch, adopt=adopt)
            await session.commit()
        adopted += done.adopted
        retired += done.retired
        if done.adopted < batch and done.retired < batch:
            break
    return Reconciled(adopted=adopted, retired=retired)


__all__ = ["BATCH", "MAX_PASSES", "Reconciled", "drain", "reconcile"]
