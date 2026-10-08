"""Creating, listing, editing and announcing workspace objects.

Three rules this module exists to hold in one place:

* **A create is idempotent on the client's own id.** ``(org, namespace,
  logical_id)`` is unique, so a retried POST lands on the row the first one
  made instead of a second one — which is what makes a create safe to retry
  over a flaky tunnel.
* **A write says which row it read.** ``expected_version`` is compared under
  the row's lock and ``0`` never matches a live row (version starts at 1), so a
  client that forgot to read cannot silently overwrite one that did.
* **Every change announces itself once**, as a content-free invalidation: the
  payload names the type and the new version and nothing else, because the
  truth is the domain table the client is about to refetch (N2.2).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from alkera_core.authz import CredentialKind, scope_for_team
from alkera_core.authz.principal import ActingContext
from alkera_core.config import settings
from alkera_core.db.locking import LockRank, lock_rows
from alkera_core.events import Entity, EventType, emit
from alkera_core.files import objects_bridge
from alkera_core.files.ids import NodeId
from alkera_core.logging import get_logger
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.workspace_object import DEFAULT_NAMESPACE
from alkera_core.observability.errors import ValidationFailedError
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.org import teams as team_service

log = get_logger(__name__)


class VersionConflictError(Exception):
    """``expected_version`` did not match the row. Carries the live object so
    the caller can hand the client back what it should have read."""

    def __init__(self, current: WorkspaceObject) -> None:
        super().__init__(f"expected version {current.version}")
        self.current = current


class LogicalIdRetiredError(Exception):
    """A deleted object still holds this logical id. The row is a tombstone —
    never returned as live, never resurrected by a retried create — and the
    unique constraint keeps the id, so a new object needs a new one."""

    def __init__(self, logical_id: str) -> None:
        super().__init__(f"logical id {logical_id!r} belongs to a deleted object")
        self.logical_id = logical_id


@dataclass(frozen=True, slots=True)
class Page:
    """One page of a listing and the cursor that continues it."""

    items: list[WorkspaceObject]
    next_cursor: str | None


def cursor_scope(*, org_team_id: UUID, type: str | None) -> str:
    """The listing a cursor belongs to: one org's rows of one type.

    A cursor is a position, never an authorization — the query it resumes is
    re-scoped to the caller's own org on every page, so replaying a stranger's
    cursor never returned a stranger's rows. It did resume from a position in
    somebody else's listing, though: a page silently skipped or repeated, with
    nothing telling the client. A cursor from another listing is a client bug or
    an attack either way, so it is refused exactly the way a mangled one is.
    Digested rather than spelled out, so the cursor still says nothing about
    which org minted it.
    """
    raw = f"{org_team_id}|{type or '*'}".encode()
    return hashlib.sha256(raw).hexdigest()[:12]


def encode_cursor(obj: WorkspaceObject, *, scope: str) -> str:
    """A keyset cursor over ``(created_at, id)`` — both immutable, so a cursor
    stays valid however often the row is edited — bound to the listing that
    minted it."""
    raw = f"{scope}|{obj.created_at.astimezone(UTC).isoformat()}|{obj.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, *, scope: str) -> tuple[datetime, UUID]:
    """``cursor`` as ``(created_at, id)`` when it is one of THIS listing's.

    Anything else — mangled, truncated, not base64 at all, or minted for
    another org or another type — is the caller's 422, raised here rather than
    left to each route to translate. A cursor is caller-supplied text and
    arrives in every shape a caller can write, so a listing that forgot the
    translation answered a hostile one with a 500 (`/chat-templates` did).
    """
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        minted, _, rest = raw.partition("|")
        stamp, _, ident = rest.partition("|")
        # Compare as bytes: `compare_digest` rejects non-ASCII str operands
        # with a TypeError.
        if not hmac.compare_digest(minted.encode("utf-8"), scope.encode("utf-8")):
            raise ValueError("cursor is from another listing")
        return datetime.fromisoformat(stamp), UUID(ident)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValidationFailedError("malformed cursor") from exc


#: What a listing hands its rows to: the ones of a batch the caller may read,
#: in the order given.
Cut = Callable[[list[WorkspaceObject]], Awaitable[list[WorkspaceObject]]]

#: The largest batch one read of a listing takes while it looks for readable
#: rows. The first read asks for exactly the page (plus one, to know whether
#: there is more); each further read doubles, up to this.
MAX_SCAN_BATCH = 5_000


async def _batch_after(
    db: AsyncSession,
    *,
    org_team_id: UUID,
    type: str | None,
    after: tuple[datetime, UUID] | None,
    size: int,
) -> list[WorkspaceObject]:
    stmt = select(WorkspaceObject).where(
        WorkspaceObject.org_team_id == org_team_id, WorkspaceObject.deleted_at == 0
    )
    if type is not None:
        stmt = stmt.where(WorkspaceObject.type == type)
    stmt = _after(stmt, after)
    stmt = stmt.order_by(WorkspaceObject.created_at.desc(), WorkspaceObject.id.desc()).limit(size)
    return list((await db.execute(stmt)).scalars().all())


async def list_objects(
    db: AsyncSession,
    *,
    org_team_id: UUID,
    type: str | None = None,
    limit: int,
    cursor: str | None = None,
    cut: Cut,
) -> Page:
    """Newest first, keyset-paged, and cut to what the caller may read BEFORE
    it is paged. Scoped to one org by construction.

    ``cut`` is the caller's read answer for a batch of rows. The page is the
    first ``limit`` rows it keeps, and the cursor is minted from the last of
    THOSE — never from a row the cut dropped. A cursor made from the last row
    fetched named a hidden row (its id and its creation time are the cursor),
    and a page cut to nothing that still said "there is more" told the caller
    a hidden row was there: walking ``?limit=1`` enumerated every private chat
    in the org. So a page reads on, a batch at a time, until it holds one row
    more than it returns (proof that a readable row follows, so the cursor is
    owed) or the rows run out; the last page carries no cursor.

    Paging stays stable under concurrent inserts: the keyset is
    ``(created_at, id)``, both immutable, and a new row is newer than every
    cursor already handed out. The read is bounded by
    ``settings.objects_list_scan_ceiling`` rows a page; a page that reaches it
    before finding what it owes returns the readable rows it found, with a
    cursor from the last of them when there is one, and ends the listing when
    there is none.
    """

    async def batch(after: Keyset | None, size: int) -> list[WorkspaceObject]:
        return await _batch_after(db, org_team_id=org_team_id, type=type, after=after, size=size)

    return await _page(
        scope=cursor_scope(org_team_id=org_team_id, type=type),
        listing={"org_id": str(org_team_id), "type": type or "*"},
        limit=limit,
        cursor=cursor,
        cut=cut,
        batch=batch,
    )


def machine_cursor_scope(*, machine_id: str) -> str:
    """The listing a box's cursor belongs to: the chats bound to one machine.
    The machine id plays the part the org plays for a person's listing — a
    cursor minted for one box's listing is refused on another's, exactly as
    an org listing refuses a stranger's."""
    raw = f"machine|{machine_id}|chat".encode()
    return hashlib.sha256(raw).hexdigest()[:12]


async def list_chats_on_machine(
    db: AsyncSession,
    *,
    machine_id: str,
    limit: int,
    cursor: str | None = None,
    cut: Cut,
) -> Page:
    """The chats bound to ``machine_id``, newest first, keyset-paged and cut
    before paging exactly like :func:`list_objects`.

    Scoped to the machine rather than to an org by construction: a box on its
    own credential is a member of no org — a pool box serves every org and a
    dedicated box the one it is assigned to — so the org is not what bounds
    what it may list. What bounds it is the binding, here, and the tenancy
    floor, which the ``cut`` enforces per row through the policy the chat
    route decides with. The cursor is bound to this machine's listing.
    """

    async def batch(after: Keyset | None, size: int) -> list[WorkspaceObject]:
        return await _bound_batch_after(db, machine_id=machine_id, after=after, size=size)

    return await _page(
        scope=machine_cursor_scope(machine_id=machine_id),
        listing={"machine_id": machine_id, "type": "chat"},
        limit=limit,
        cursor=cursor,
        cut=cut,
        batch=batch,
    )


async def chats_bound_to(db: AsyncSession, *, machine_id: str) -> list[tuple[UUID, UUID]]:
    """Every live chat bound to ``machine_id``, as ``(chat id, org id)`` pairs.

    The box's whole reach, read in one statement: the socket and the event
    stream a box holds on its credential decide from this set which frames
    are its own, and re-read it on their tick so a chat rebound elsewhere
    stops reaching the box within one tick. The predicate is the listing's
    (:func:`list_chats_on_machine`), spelled on the same column.
    """
    rows = await db.execute(
        select(WorkspaceObject.id, WorkspaceObject.org_team_id).where(
            WorkspaceObject.type == "chat",
            WorkspaceObject.deleted_at == 0,
            WorkspaceObject.spec["machine_id"].astext == machine_id,
        )
    )
    return [(chat_id, org_id) for chat_id, org_id in rows.all()]


#: A keyset position: the ``(created_at, id)`` of the last row read.
Keyset = tuple[datetime, UUID]

#: How a listing reads its next batch after a position.
Batch = Callable[[Keyset | None, int], Awaitable[list[WorkspaceObject]]]


async def _page(
    *,
    scope: str,
    listing: Mapping[str, str],
    limit: int,
    cursor: str | None,
    cut: Cut,
    batch: Batch,
) -> Page:
    """The one paging loop every listing runs: read on, a batch at a time,
    until the page holds one readable row more than it returns or the rows
    run out, bounded by the scan ceiling. ``listing`` names the listing in
    the ceiling's log line."""
    after = decode_cursor(cursor, scope=scope) if cursor else None
    kept: list[WorkspaceObject] = []
    size = limit + 1
    scanned = 0
    ceiling = settings.objects_list_scan_ceiling
    truncated = False
    while True:
        rows = await batch(after, size)
        scanned += len(rows)
        if rows:
            kept.extend(await cut(rows))
        if len(kept) > limit or len(rows) < size:
            break
        if scanned >= ceiling:
            truncated = True
            break
        after = (rows[-1].created_at, rows[-1].id)
        size = min(size * 2, max(MAX_SCAN_BATCH, limit + 1))
    if len(kept) > limit:
        page = kept[:limit]
        return Page(items=page, next_cursor=encode_cursor(page[-1], scope=scope))
    if truncated:
        log.warning("objects.list.scan_ceiling", scanned=scanned, kept=len(kept), **listing)
        if kept:
            return Page(items=kept, next_cursor=encode_cursor(kept[-1], scope=scope))
    return Page(items=kept, next_cursor=None)


def _after(stmt: Select[tuple[WorkspaceObject]], after: Keyset | None) -> Any:
    if after is None:
        return stmt
    created_at, ident = after
    return stmt.where(
        (WorkspaceObject.created_at < created_at)
        | ((WorkspaceObject.created_at == created_at) & (WorkspaceObject.id < ident))
    )


async def _bound_batch_after(
    db: AsyncSession, *, machine_id: str, after: Keyset | None, size: int
) -> list[WorkspaceObject]:
    stmt = select(WorkspaceObject).where(
        WorkspaceObject.type == "chat",
        WorkspaceObject.deleted_at == 0,
        WorkspaceObject.spec["machine_id"].astext == machine_id,
    )
    stmt = _after(stmt, after)
    stmt = stmt.order_by(WorkspaceObject.created_at.desc(), WorkspaceObject.id.desc()).limit(size)
    return list((await db.execute(stmt)).scalars().all())


async def _holder_of(
    db: AsyncSession, *, org_team_id: UUID, namespace: str, logical_id: str
) -> WorkspaceObject | None:
    """Whatever row holds ``logical_id`` in this namespace — live or tombstoned."""
    return (
        await db.execute(
            select(WorkspaceObject).where(
                WorkspaceObject.org_team_id == org_team_id,
                WorkspaceObject.namespace == namespace,
                WorkspaceObject.logical_id == logical_id,
            )
        )
    ).scalar_one_or_none()


async def find_by_logical_id(
    db: AsyncSession, *, org_team_id: UUID, namespace: str, logical_id: str
) -> WorkspaceObject | None:
    """The LIVE object ``logical_id`` names, or ``None``. A tombstone holds
    its id (the unique constraint is not partial) but is not an object any
    caller may be handed."""
    holder = await _holder_of(
        db, org_team_id=org_team_id, namespace=namespace, logical_id=logical_id
    )
    return holder if holder is not None and holder.deleted_at == 0 else None


async def create_object(
    db: AsyncSession,
    *,
    owner: User,
    org_id: UUID,
    type: str,
    title: str,
    spec: dict[str, Any],
    logical_id: str | None = None,
    namespace: str = DEFAULT_NAMESPACE,
    status: str = "ready",
    team_id: UUID | None = None,
    object_id: UUID | None = None,
    owner_user_id: UUID | None = None,
    visibility_scope: str | None = None,
    parent_node_id: UUID | None = None,
    with_node: bool = True,
) -> tuple[WorkspaceObject, bool]:
    """``(object, created)``.

    ``visibility_scope`` overrides the audience ``team_id`` implies — a chat is
    created ``private`` (its owner's alone until its node is shared) rather
    than addressed to the org — and must agree with ``team_id`` the way every
    reader checks the two: ``private`` and ``org`` with no team, ``team:<id>``
    with that team.

    ``created`` is ``False`` when the client's ``logical_id`` already names an
    object in this namespace: the caller gets the row it made the first time,
    unchanged, and no second announcement goes out.

    ``object_id`` lets the caller name the row before it exists, so the
    authorization decision that admitted the create is filed under the id the
    row then has. The audience is spelled in the authorization package's scope
    grammar (``org`` | ``team:<uuid>``), which is what both the REST policies
    and the socket parse. ``owner_user_id`` names an owner other than the
    caller — a promoted result is owned by the chat's owner, whoever promoted
    it. ``org_id`` is the org the row lives in: the request's org, or the
    org of the object it is made from, never one read off a person.

    ``parent_node_id`` files the object's node under that folder instead of
    the place its kind names in the owner's home (a chat started in a
    workspace goes in the workspace's ``.chats``). ``with_node=False`` makes
    no node at all, for an object whose folder is another object's: a
    workspace of one adopts its chat's folder rather than owning one.
    """
    chosen = logical_id or str(uuid4())
    holder = await _holder_of(db, org_team_id=org_id, namespace=namespace, logical_id=chosen)
    if holder is not None:
        if holder.deleted_at != 0:
            raise LogicalIdRetiredError(chosen)
        return holder, False
    obj = WorkspaceObject(
        id=object_id or uuid4(),
        org_team_id=org_id,
        logical_id=chosen,
        namespace=namespace,
        type=type,
        title=title,
        version=1,
        status=status,
        spec=spec,
        owner_user_id=owner_user_id or owner.id,
        team_id=team_id,
        visibility_scope=visibility_scope or scope_for_team(team_id),
        content_updated_at=datetime.now(UTC).timestamp(),
    )
    try:
        async with db.begin_nested():
            db.add(obj)
            await db.flush()
    except IntegrityError:
        # A concurrent create with the same logical id committed between the
        # read above and this insert: the unique (org, namespace, logical id)
        # refused this one, and the retry is handed the row the winner made.
        raced = await _holder_of(db, org_team_id=org_id, namespace=namespace, logical_id=chosen)
        if raced is None:
            raise
        if raced.deleted_at != 0:
            raise LogicalIdRetiredError(chosen) from None
        return raced, False
    if settings.files_enabled and with_node:
        # Inside the create's own transaction, so there is no window in which
        # an object exists without the node the drive shows it as.
        ctx = ActingContext.for_user(user_id=owner.id, org_id=obj.org_team_id, email=owner.email)
        async with team_service.files_transaction(db, ctx) as repo:
            await objects_bridge.node_for_object(
                repo,
                ctx,
                obj,
                parent_id=NodeId(parent_node_id) if parent_node_id is not None else None,
            )
    return obj, True


async def lock(db: AsyncSession, object_id: UUID) -> WorkspaceObject | None:
    """The object row, locked for update — the serialization point for a
    version bump and for assigning a transcript sequence.

    The row is re-read from the database into the session's instance
    (``populate_existing``): a route has usually loaded this object already,
    and without the refresh the identity map would hand back the copy it
    read *before* taking the lock — so a writer that committed in between
    would go unseen and the version check below it would pass against a
    stale number. The lock is only a serialization point if what it returns
    is what the lock now protects.
    """
    return (
        await lock_rows(
            db,
            LockRank.WORKSPACE_OBJECT,
            select(WorkspaceObject)
            .where(WorkspaceObject.id == object_id, WorkspaceObject.deleted_at == 0)
            .execution_options(populate_existing=True),
        )
    ).scalar_one_or_none()


async def apply_update(
    db: AsyncSession,
    *,
    obj: WorkspaceObject,
    expected_version: int,
    title: str | None = None,
    spec: dict[str, Any] | None = None,
    status: str | None = None,
) -> WorkspaceObject:
    """Bump ``obj`` under its lock, or raise :class:`VersionConflictError`."""
    locked = await lock(db, obj.id)
    if locked is None:
        raise VersionConflictError(obj)
    if locked.version != expected_version:
        raise VersionConflictError(locked)
    if title is not None:
        locked.title = title
    if spec is not None:
        locked.spec = spec
    if status is not None:
        locked.status = status
    locked.version += 1
    locked.content_updated_at = datetime.now(UTC).timestamp()
    await db.flush()
    await bump_object_node(db, locked)
    return locked


async def bump_object_node(db: AsyncSession, obj: WorkspaceObject) -> None:
    """Move the file surface's change token for ``obj`` in the caller's txn.

    One outbox row per edit, emitted where the object's own write happens, so a
    client watching the drive and one watching the object see the same edit at
    the same instant."""
    if not settings.files_enabled:
        return
    ctx = _system_ctx(obj)
    async with team_service.files_transaction(db, ctx) as repo:
        await objects_bridge.bump_for_object_update(repo, ctx, obj.id)


async def tombstone(db: AsyncSession, obj: WorkspaceObject, *, trash_node: bool = True) -> None:
    """Tombstone ``obj`` in the caller's transaction: ``deleted_at`` stamped, the
    version bumped (a deletion is a write), and its node trashed unless the
    object owns none (``trash_node=False``, a workspace of one)."""
    obj.deleted_at = datetime.now(UTC).timestamp()
    obj.version += 1
    await db.flush()
    if trash_node:
        await tombstone_object_node(db, obj)


async def tombstone_object_node(db: AsyncSession, obj: WorkspaceObject) -> None:
    """Trash the node of a deleted object, in the deletion's own transaction.

    One direction only: deleting the object trashes its node, trashing the node
    never touches the object row."""
    if not settings.files_enabled:
        return
    ctx = _system_ctx(obj)
    async with team_service.files_transaction(db, ctx) as repo:
        await objects_bridge.tombstone_for_object(repo, ctx, obj.id)


def _system_ctx(obj: WorkspaceObject) -> ActingContext:
    """The context an update or a delete files its node history under.

    The object's owner, not the caller: an update may arrive from a socket, a
    worker or a promotion, and the node's history should read as the object's
    own history rather than as whoever happened to touch it."""
    return ActingContext.for_service(
        token_id=obj.owner_user_id,
        org_id=obj.org_team_id,
        label="object_service",
        credential=CredentialKind.CI_TOKEN,
    )


async def announce(db: AsyncSession, *, obj: WorkspaceObject, actor: dict[str, Any] | None) -> None:
    """One content-free invalidation for ``obj``: the type and the version, so
    a client can tell whether it already holds this one, and nothing more."""
    await emit(
        db,
        org_id=obj.org_team_id,
        type=EventType.WORKSPACE_OBJECT_CHANGED,
        entity=Entity.WORKSPACE_OBJECT,
        entity_id=str(obj.id),
        version=obj.version,
        payload={"type": obj.type, "version": obj.version},
        actor=actor,
    )


__all__ = [
    "Cut",
    "LogicalIdRetiredError",
    "Page",
    "VersionConflictError",
    "announce",
    "apply_update",
    "bump_object_node",
    "chats_bound_to",
    "create_object",
    "cursor_scope",
    "decode_cursor",
    "encode_cursor",
    "find_by_logical_id",
    "list_chats_on_machine",
    "list_objects",
    "lock",
    "machine_cursor_scope",
    "tombstone",
    "tombstone_object_node",
]
