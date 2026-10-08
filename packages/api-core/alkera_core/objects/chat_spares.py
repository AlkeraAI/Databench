"""The chat warmed ahead of a person's first message, at the row level.

A spare is an ordinary chat row whose spec carries ``spare = true``: the
backend warms it (creates, places, announces it so the box opens its session)
while its owner is on the chat page, the owner's first send claims it, and
whatever is left unclaimed is reaped. What lives here is the part that has no
request around it — the row reads and the row writes both the routes and the
worker's sweep need — so the two cannot disagree about what a spare is.

At most one spare per owner in each org is a database rule (a partial unique
index over ``(owner_user_id, org_team_id)`` where the flag is set), so two warm
calls racing each other leave one row, and a claim is a locked read of that one
row. A person who belongs to several orgs keeps one spare in each, and a read
here always names the org: one org's page never sees another org's spare.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from alkera_core.db.cross_tenant import cross_tenant_write
from alkera_core.logging import get_logger
from alkera_core.models import WorkspaceObject
from alkera_core.objects.workspaces import ADOPTED_NAMESPACE

log = get_logger(__name__)

#: A spare whose owner has sent no heartbeat for this long is reaped: the page
#: beats every minute while visible, so three misses is a closed tab, a
#: hidden tab, or a laptop lid, and not a slow network.
IDLE_CUTOFF = timedelta(minutes=3)
#: No spare outlives this, heartbeat or not: a tab left open over a weekend
#: must not hold an agent session and a folder lease for the weekend.
MAX_AGE = timedelta(hours=2)

CHAT_TYPE = "chat"


def is_spare(chat: WorkspaceObject) -> bool:
    """Whether this chat row is a spare: warmed, unclaimed, hidden."""
    return bool((chat.spec or {}).get("spare"))


def _spares_of(owner_id: UUID) -> Any:
    return select(WorkspaceObject).where(
        WorkspaceObject.owner_user_id == owner_id,
        WorkspaceObject.type == CHAT_TYPE,
        WorkspaceObject.deleted_at == 0,
        WorkspaceObject.spec["spare"].as_boolean().is_(True),
    )


def _spare_of(owner_id: UUID, org_id: UUID) -> Any:
    return select(WorkspaceObject).where(
        WorkspaceObject.owner_user_id == owner_id,
        WorkspaceObject.org_team_id == org_id,
        WorkspaceObject.type == CHAT_TYPE,
        WorkspaceObject.deleted_at == 0,
        WorkspaceObject.spec["spare"].as_boolean().is_(True),
    )


async def find_spare(db: AsyncSession, *, owner_id: UUID, org_id: UUID) -> WorkspaceObject | None:
    """The owner's standing spare in ``org_id``, unlocked, or ``None``."""
    return (await db.execute(_spare_of(owner_id, org_id))).scalar_one_or_none()


async def spares_of_owner(db: AsyncSession, *, owner_id: UUID) -> list[WorkspaceObject]:
    """Every spare the owner has standing, one per org at most. Only signing
    out reads across orgs: the session it ends spans every org it switched to."""
    return list((await db.execute(_spares_of(owner_id))).scalars())


async def lock_spare(db: AsyncSession, *, owner_id: UUID, org_id: UUID) -> WorkspaceObject | None:
    """The owner's spare in ``org_id``, locked for this transaction — or
    ``None`` when there is none, or when another transaction holds it.

    ``SKIP LOCKED`` is what makes two sends from the same person at the same
    moment both succeed: the first locks the spare and claims it, the second
    finds nothing to lock and creates a chat of its own, and neither waits on
    the other. Re-read into the session (``populate_existing``) so a caller
    that loaded the row earlier does not decide on a stale copy.
    """
    return (
        await db.execute(
            _spare_of(owner_id, org_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def stamp_active(db: AsyncSession, chat: WorkspaceObject, *, now: datetime) -> None:
    """The owner was just seen on the page: move the spare's idle clock."""
    spec = dict(chat.spec)
    spec["spare_active_at"] = now.isoformat()
    chat.spec = spec
    await db.flush()


async def mark_claimed(db: AsyncSession, chat: WorkspaceObject) -> None:
    """Turn the spare into the chat it was warmed to be: the flag and the
    idle clock go, the row bumps its version as a create-by-use, and from the
    next read on it is listed, titled by its first prompt, and counted."""
    spec = dict(chat.spec)
    spec.pop("spare", None)
    spec.pop("spare_active_at", None)
    chat.spec = spec
    chat.version += 1
    await db.flush()


def _instant(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def is_stale(chat: WorkspaceObject, *, now: datetime) -> bool:
    """Whether a spare has outlived its owner's presence or its maximum age.

    A spare that was never stamped is judged from its creation, so a warm
    whose page died before its first heartbeat does not stand forever.
    """
    spec = chat.spec or {}
    created = datetime.fromtimestamp(chat.created_at.timestamp(), tz=UTC)
    if now - created >= MAX_AGE:
        return True
    seen = _instant(spec.get("spare_active_at")) or created
    return now - seen >= IDLE_CUTOFF


async def stale_spares(db: AsyncSession, *, now: datetime) -> list[WorkspaceObject]:
    """Every spare that is due to be reaped, across orgs. The set of spares is
    at most one per person on the page, so reading them all and judging each
    in Python costs one indexed statement."""
    rows = (
        await db.execute(
            select(WorkspaceObject).where(
                WorkspaceObject.type == CHAT_TYPE,
                WorkspaceObject.deleted_at == 0,
                WorkspaceObject.spec["spare"].as_boolean().is_(True),
            )
        )
    ).scalars()
    return [chat for chat in rows if is_stale(chat, now=now)]


async def spares_bound_to(db: AsyncSession, *, machine_id: UUID) -> list[WorkspaceObject]:
    """The spares placed on one machine — what a box's loss orphans."""
    async with cross_tenant_write(db, reason="compute.machine_chats.spares"):
        rows = (
            await db.execute(
                select(WorkspaceObject).where(
                    WorkspaceObject.type == CHAT_TYPE,
                    WorkspaceObject.deleted_at == 0,
                    WorkspaceObject.spec["spare"].as_boolean().is_(True),
                    WorkspaceObject.spec["machine_id"].as_string() == str(machine_id),
                )
            )
        ).scalars()
        return list(rows)


async def erase_row(db: AsyncSession, chat: WorkspaceObject) -> None:
    """Delete the spare's row for good — never a tombstone.

    A deleted chat keeps its row so its transcript stays for the audit trail;
    a spare has no transcript and was never shown to anyone, so a tombstone
    would only be a row the trash and every "deleted chats" count had to learn
    to skip. Its transcript table is empty by construction; the delete is
    guarded on the flag so a claim that won the race keeps its chat.
    """
    # The workspace of one the spare was given goes with it, and only when the
    # spare itself did: a claim that won the race keeps both.
    await db.execute(
        text(
            "WITH gone AS (DELETE FROM workspace_objects WHERE id = :id AND type = 'chat' "
            "AND (spec->>'spare') = 'true' RETURNING id) "
            "DELETE FROM workspace_objects AS w USING gone "
            "WHERE w.type = 'workspace' AND w.namespace = :adopted "
            "AND w.logical_id = gone.id::text"
        ),
        {"id": chat.id, "adopted": ADOPTED_NAMESPACE},
    )
    db.expunge(chat)


# ---------------------------------------------------------------------------
# Reaping: the row goes at once, the folder once nothing holds it
# ---------------------------------------------------------------------------


async def purge_node_of(db: AsyncSession, *, org_id: UUID, chat_id: UUID, owner_id: UUID) -> bool:
    """Remove the spare's folder from the drive for good — never into the trash.

    ``True`` when the node is gone (or there never was one). ``False`` when it
    is still leased: the box that warmed the spare holds its folder until it
    sees the row gone and stops the mirror, and a purge inside a live lease is
    refused by the same fence every write meets. The next sweep finds the
    folder unleased and takes it then — :func:`purge_orphaned_nodes`.
    """
    from alkera_core.authz import ActingContext, CredentialKind
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.errors import FilesError
    from alkera_core.files.ids import NodeId, OrgScope
    from alkera_core.files.objects_bridge import live_node_for
    from alkera_core.files.repo import FilesRepo
    from alkera_core.files.trash import Trash

    ctx = ActingContext.for_service(
        token_id=owner_id, org_id=org_id, label="chat_spares", credential=CredentialKind.CI_TOKEN
    )
    repo = FilesRepo.joined(db, OrgScope(org_team_id=org_id))
    try:
        async with repo.transaction():
            node = await live_node_for(repo, chat_id)
            if node is None:
                return True
            await Trash(repo, ctx, SystemClock()).purge(NodeId(node.id))
    except FilesError:
        return False
    return True


async def orphaned_spare_nodes(db: AsyncSession) -> list[tuple[UUID, UUID, UUID]]:
    """``(org_id, node_id, owner_id)`` for every live chat folder whose chat row
    no longer exists at all — a reaped spare's folder, left for the lease to
    lapse. A deleted chat keeps its row (tombstoned), so its folder is never
    one of these.

    ``file_nodes.created_by`` is nullable, so the owner falls back to the node
    itself: the value is only the principal the purge acts under, and one pass
    sweeps every org, so a single folder that records no creator must not be
    allowed to end the pass and strand every other orphan behind it.
    """
    rows = await db.execute(
        text(
            "SELECT n.org_team_id, n.id, COALESCE(n.created_by, n.id) FROM file_nodes n "
            "WHERE n.trashed_at IS NULL AND n.subtype = 'chat' "
            "AND n.target_object_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM workspace_objects o WHERE o.id = n.target_object_id)"
        )
    )
    return [(UUID(str(org)), UUID(str(node)), UUID(str(owner))) for org, node, owner in rows.all()]


async def purge_orphaned_nodes(db: AsyncSession) -> int:
    """Take away every reaped spare's folder that its lease has since let go.
    Returns how many were purged; the ones still leased wait another pass.

    One pass sweeps every org, so no single folder may end it. A folder still
    leased is the expected refusal and waits quietly; anything else a purge
    raises — a half-torn-down node whose rows are already partly gone, say — is
    logged against its id and skipped, because a folder nobody can explain must
    not strand every other orphan behind it.
    """
    from alkera_core.authz import ActingContext, CredentialKind
    from alkera_core.files.clock import SystemClock
    from alkera_core.files.errors import FilesError
    from alkera_core.files.ids import NodeId, OrgScope
    from alkera_core.files.repo import FilesRepo
    from alkera_core.files.trash import Trash

    purged = 0
    for org_id, node_id, owner_id in await orphaned_spare_nodes(db):
        ctx = ActingContext.for_service(
            token_id=owner_id,
            org_id=org_id,
            label="chat_spares",
            credential=CredentialKind.CI_TOKEN,
        )
        repo = FilesRepo.joined(db, OrgScope(org_team_id=org_id))
        try:
            async with repo.transaction():
                await Trash(repo, ctx, SystemClock()).purge(NodeId(node_id))
        except FilesError:
            continue
        except Exception as exc:
            log.warning(
                "chat.spares.orphan_purge_failed",
                node_id=str(node_id),
                org_id=str(org_id),
                error=repr(exc),
            )
            continue
        purged += 1
    return purged


async def reap(db: AsyncSession, chat: WorkspaceObject) -> None:
    """Reap one spare: its row goes now, its folder now or on a later sweep.

    Order matters. The row first, because the box's discovery reads the row:
    a chat gone from its list is a mirror the box stops, and stopping it is
    what hands the folder's lease back. The folder second, and only if nothing
    holds it — otherwise it is the orphan pass's, once the lease is gone.
    """
    org_id, chat_id, owner_id = chat.org_team_id, chat.id, chat.owner_user_id
    # A reaped spare is a deleted chat: it ends through the one transition
    # first, so its folder's lease is gone before the purge below asks.
    from alkera_core.objects import chat_end

    await chat_end.end_chat(db, chat_id, chat_end.ChatEndReason.DELETED, actor=None)
    await erase_row(db, chat)
    await purge_node_of(db, org_id=org_id, chat_id=chat_id, owner_id=owner_id)


async def reap_stale(db: AsyncSession, *, now: datetime) -> int:
    """The sweep's pass: every spare past its idle cutoff or its maximum age
    is reaped, then every folder an earlier reap left leased is tried again.
    Returns how many spares were reaped."""
    stale = await stale_spares(db, now=now)
    for chat in stale:
        await reap(db, chat)
    await purge_orphaned_nodes(db)
    return len(stale)


__all__ = [
    "CHAT_TYPE",
    "IDLE_CUTOFF",
    "MAX_AGE",
    "erase_row",
    "find_spare",
    "is_spare",
    "is_stale",
    "lock_spare",
    "mark_claimed",
    "orphaned_spare_nodes",
    "purge_node_of",
    "purge_orphaned_nodes",
    "reap",
    "reap_stale",
    "spares_bound_to",
    "spares_of_owner",
    "stale_spares",
    "stamp_active",
]
