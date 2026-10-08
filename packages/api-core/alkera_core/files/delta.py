"""The change feed: one page of "what changed in this drive since your token".

These properties make it a feed a client may trust rather than a listing it
has to reconcile.

*The watermark.* The outbox ``id`` is a ``bigserial``, so it is allocated when
the INSERT runs, not when the transaction commits: a writer that takes id 7 and
commits after a writer that took id 8 would, on a plain ``id > cursor`` read, be
skipped forever once the client's cursor moved past 8. So a row is only
delivered once its inserting transaction is *strictly below the oldest in-flight
one*: ``age(xmin) > age(pg_snapshot_xmin(pg_current_snapshot()))``, the
wraparound-safe spelling of "older than the snapshot's xmin". Rows at or above
that boundary stay behind until every transaction that could still commit under
them has finished.

*The cursor travels in the order the boundary filters on.* The boundary is a
statement about transactions, so the feed is ordered and cursored by
``(xmin, id)``, never by the serial alone. The two orders disagree the moment a
transaction that took its xid early takes its outbox id late: writer B (early
xid, late id) is deliverable while writer A (later xid, earlier id) is still
open, so an ``id``-cursor delivers B, advances past A's id, and loses A forever
once it commits. Under ``(xmin, id)`` the cursor lands on B's transaction, and
A, whose xid is *newer* than B's, still sorts after it. The deliverable set
only ever gains rows whose xid is at or above the boundary that held them back,
and the boundary only rises, so a row that becomes deliverable always sorts
after everything already delivered.

*The boundary is read in the order that keeps it rising.* The oldest in-flight
transaction is taken over the whole server, so the set is narrowed to the
transactions that could actually be sitting on an outbox row in THIS database
before the boundary is taken from it. Which backend is on which database is
therefore read one statement AHEAD of the snapshot it narrows, never inside it.
That order keeps the boundary monotone (see ``_LOCAL_XMIN``).

*Ids, not names.* Outbox rows carry ids only (they outlive the node they
describe), so a page joins the CURRENT node row by id in the same statement. A
node that was deleted becomes a tombstone ``{id, deleted: true}`` with no name
key at all, so a revoke cannot leak through the feed the way it would if the
page were rendered from the event payload.

*Only what the caller may see.* The feed never mentions a node the caller has
no claim to, not even as a tombstone: a tombstone for every private node in the
drive would hand each member the ids of everyone else's files and a signal each
time one changes. Each item is decided by :func:`shown_to`:

* a live node is delivered when the caller may read it;
* a trashed node is a tombstone when the caller may read it (trashing does not
  change who may read a node);
* a node whose row is gone is a tombstone when the feed knows the folder it
  was last in and the caller may read that folder. Access in Files only ever
  adds downward, so whoever may read the folder could read what was in it;
* a node the caller may no longer read is a tombstone only when one of its
  rows on the page records a withdrawn grant that named the caller (the
  ``withdrawn`` principals of the revoke's announcement). That caller had the
  node, so it is owed the news that the node is gone from them.

Anything else is left out of the page entirely. The one gap this leaves is
deliberate: a caller who lost a node some other way (left the team that held
the grant, a grant that expired) is not told, and finds out the next time it
lists or opens the node, which answers 404. Telling it would mean telling every
member, since the feed holds no record of who saw what.

*Duplicates, never gaps.* An item is the node's latest state, so applying a page
twice is a no-op and a page may repeat an id the client already has. What never
happens is a committed row that no read returns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import ARRAY, BigInteger, bindparam, text

from alkera_core.authz.principal import ActingContext
from alkera_core.db.tenant_session import stepped_out
from alkera_core.files.checkpoints import Checkpoints, NoopCheckpoints
from alkera_core.files.clock import Clock

# Re-exported under their own names: the token moved into `delta_token` but the
# feed is where callers look for it, and an explicit alias is what tells a type
# checker this module still offers them.
from alkera_core.files.delta_token import TOKEN_GENERATION as TOKEN_GENERATION
from alkera_core.files.delta_token import DeltaToken as DeltaToken
from alkera_core.files.errors import FilesError, InvalidRequest
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.scoped import DomainStore

#: The outbox ``entity`` the node feed reads. ``file_operation`` rows announce
#: bulk operations, not nodes, and belong to a different feed.
DELTA_ENTITY: Final = "file_node"

#: How long a token stays redeemable. The outbox is retained for the same
#: window, so a token older than this names a cursor whose rows may be gone; the
#: client is told to resync rather than handed a page with a hole in it.
DELTA_RETENTION: Final = timedelta(days=30)

#: What a 410 tells the client to wait before starting its resync, so a fleet
#: whose tokens expired together does not re-enumerate all at once.
DEFAULT_RETRY_AFTER: Final = 30

DEFAULT_LIMIT: Final = 200
DELTA_MAX_LIMIT: Final = 1000

#: The client must re-enumerate and apply what the server says.
RESYNC_APPLY: Final = "resync_apply_differences"
#: The client must additionally re-offer its local changes: the feed's identity
#: changed under it, so the server cannot say which of its writes landed.
RESYNC_UPLOAD: Final = "resync_upload_differences"

#: The default pause points: none. A test passes its own ``PausingCheckpoints``.
_NO_CHECKPOINTS: Checkpoints = NoopCheckpoints()

#: Rows are fetched and joined, and have not been authorized yet — a test that
#: revokes access arms this to land its revoke between the read and the decision.
CP_ROWS_READ: Final = "delta.rows_read_before_authorize"
#: The page is assembled and its links chosen; the caller has not seen it yet.
CP_PAGE_READY: Final = "delta.page_ready"

#: Every in-flight transaction that is somewhere else — the xids the boundary
#: may drop.
#:
#: Read as its own statement, BEFORE the one whose snapshot it narrows. A
#: backend holds an xid on exactly one database and an xid is never handed out
#: twice, so "this xid belongs to another database" stays true however old the
#: observation is: an early reading can only *miss* a transaction, never
#: misplace one.
#:
#: A prepared transaction keeps its xid with no backend left to report it, so
#: ``pg_stat_activity`` can place it nowhere; ``pg_prepared_xacts`` names the
#: database it was prepared on, which is the same positive placement.
_XIDS_ELSEWHERE = text(
    """
    SELECT coalesce(array_agg(xid), ARRAY[]::bigint[]) AS xids
      FROM (SELECT a.backend_xid::text::bigint AS xid
              FROM pg_stat_activity AS a
             WHERE a.backend_xid IS NOT NULL
               AND a.datid IS DISTINCT FROM (SELECT d.oid FROM pg_database AS d
                                              WHERE d.datname = current_database())
            UNION ALL
            SELECT p.transaction::text::bigint
              FROM pg_prepared_xacts AS p
             WHERE p.database IS DISTINCT FROM current_database()) AS other_databases
    """
)

#: The oldest transaction that could still take an outbox id in THIS database.
#:
#: ``pg_current_snapshot()``'s xmin is computed over every backend on the
#: server, not over this database: one long write transaction in an unrelated
#: database holds the boundary down and the feed hands back nothing at all,
#: for every drive, until that transaction ends. So the in-progress list is
#: narrowed by ``_XIDS_ELSEWHERE`` to the xids that could actually be sitting on
#: an outbox row here.
#:
#: **The two observations are ordered, and the order is the correctness.** The
#: placement is read one statement AHEAD of this snapshot. Read the other way
#: round — a ``pg_stat_activity`` scan inside this very statement, which is what
#: it looks like when the two are spelled as one query — and every transaction
#: that ENDED between the statement's snapshot and its scan can be placed
#: nowhere: it is still in the snapshot's in-progress list, but no backend
#: reports it any more. Those are old xids, so the boundary drops under rows the
#: feed had already handed back, and the next read withholds them again. On a
#: cluster busy enough that some transaction is always ending — sixty-four test
#: workers, each with its own database — that is most reads, and a consumer sees
#: the feed empty out and refill. Read ahead, and the only transaction that
#: cannot be placed is one that took its id AFTER the reading — newer than every
#: row already committed, and so holding nothing back.
#:
#: Safety runs the other way: an xid is dropped only when it is positively
#: placed elsewhere, so one that is merely unaccounted for stays in the set and
#: the boundary is only ever older — and only a boundary NEWER than a local
#: in-flight transaction could step over a row. That also makes the ordering
#: safe to be stale: it costs delivery latency, never a row.
#:
#: This assumes each statement takes its own snapshot, which is READ COMMITTED,
#: the isolation the feed runs under. Under a repeatable-read transaction the
#: snapshot predates every statement in it, including the placement, which puts
#: the boundary back to withholding — still safe, just slow.
#:
#: ``min`` over ``xid8`` exists only from PostgreSQL 16; the aggregate runs over
#: the decimal spelling instead so the same feed answers on the older servers
#: the test runners still carry (the value is a 64-bit transaction id, which
#: ``numeric`` holds exactly and ``xid`` reads back from its text). The
#: comparison narrows each 64-bit id to the 32-bit ``xid`` a backend reports.
_LOCAL_XMIN = """
        (SELECT coalesce(min(x::text::numeric)::text::xid,
                         pg_snapshot_xmax(pg_current_snapshot())::text::xid)
           FROM pg_snapshot_xip(pg_current_snapshot()) AS x
          WHERE x::text::xid::text::bigint <> ALL (:elsewhere))
"""

#: One row per delivered change, joined against the live node.
_PAGE_SQL = text(
    f"""
    SELECT o.id                    AS outbox_id,
           o.xmin::text::bigint    AS cursor_xid,
           o.entity_id             AS entity_id,
           n.id                    AS node_id,
           n.parent_id             AS parent_id,
           n.drive_id              AS node_drive_id,
           n.kind                  AS kind,
           n.name_display          AS name_display,
           n.etag                  AS etag,
           n.size                  AS size,
           n.trashed_at            AS trashed_at,
           o.payload->>'parent_id' AS payload_parent_id,
           o.payload->'withdrawn'  AS withdrawn,
           v.content_hash          AS content_hash,
           v.mime_sniffed          AS mime_sniffed,
           v.scan_state            AS scan_state,
           EXISTS (SELECT 1
                     FROM file_stars AS s
                    WHERE s.node_id = n.id
                      AND s.user_id = CAST(:me AS uuid)
                      AND s.org_team_id = :org)
                                   AS starred
      FROM event_outbox AS o
      LEFT JOIN file_nodes AS n
             ON n.id = o.entity_id::uuid
            AND n.org_team_id = :org
      LEFT JOIN file_versions AS v
             ON v.id = n.head_version_id
            AND v.org_team_id = :org
     WHERE o.org_id = :org
       AND o.entity = :entity
       AND o.payload->>'drive_id' = :drive
       AND age(o.xmin) > age({_LOCAL_XMIN})
       AND (CAST(:cursor_xid AS bigint) = 0
            OR age(o.xmin) < age(CAST(CAST(:cursor_xid AS bigint) AS text)::xid)
            OR (o.xmin = CAST(CAST(:cursor_xid AS bigint) AS text)::xid
                AND o.id > CAST(:cursor_id AS bigint)))
     ORDER BY age(o.xmin) DESC, o.id
     LIMIT :limit
    """  # noqa: S608 - the only interpolation is the module literal above; every value stays bound
).bindparams(bindparam("elsewhere", type_=ARRAY(BigInteger)))

#: The greatest deliverable row in ``(xmin, id)`` order — the boundary itself,
#: expressed as a cursor. Everything at or under it is deliverable now, so it is
#: where "caught up" points. Newest transaction first (``age`` ascending), and
#: within it the highest id.
_BOUNDARY_SQL = text(
    f"""
    SELECT o.xmin::text::bigint AS cursor_xid,
           o.id                 AS outbox_id
      FROM event_outbox AS o
     WHERE o.org_id = :org
       AND o.entity = :entity
       AND o.payload->>'drive_id' = :drive
       AND age(o.xmin) > age({_LOCAL_XMIN})
     ORDER BY age(o.xmin), o.id DESC
     LIMIT 1
    """  # noqa: S608 - the only interpolation is the module literal above; every value stays bound
).bindparams(bindparam("elsewhere", type_=ARRAY(BigInteger)))


class DeltaExpired(FilesError):  # noqa: N818 - the route layer maps these names; the Error suffix is noise here
    """The token names a cursor the feed can no longer honour (410).

    Carries the resync flavour the client must perform and the delay it should
    wait first; the route turns those into the body's ``code`` and the
    ``Retry-After`` header.
    """

    status = 410

    def __init__(
        self,
        code: str = RESYNC_APPLY,
        message: str = "",
        *,
        retry_after: int = DEFAULT_RETRY_AFTER,
    ) -> None:
        if code not in (RESYNC_APPLY, RESYNC_UPLOAD):
            raise ValueError(f"unknown resync code {code!r}")
        super().__init__(message or code, code=code)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class _Cursor:
    """Where a read resumes: a transaction, and a position within it."""

    xid: int
    outbox_id: int


#: Read everything in the retained window: no transaction half, no position.
_START: Final = _Cursor(xid=0, outbox_id=0)


@dataclass(frozen=True, slots=True)
class DeltaItem:
    """One node's latest state, or a name-free tombstone for it.

    ``starred`` is *this* caller's bookmark, not the node's "somebody starred
    it" bit, and the head version's hash, mime type and scan state ride along
    because a sync client decides whether to re-download from exactly those —
    all four come out of the page's own statement rather than a second read per
    row.
    """

    id: NodeId
    deleted: bool
    drive_id: DriveId | None = None
    parent_id: NodeId | None = None
    kind: str | None = None
    name: str | None = None
    etag: int | None = None
    size: int | None = None
    starred: bool = False
    content_hash: str | None = None
    mime_type: str | None = None
    scan_state: str | None = None
    #: The node whose readability decides whether this item may be shown at
    #: all: the node itself while its row exists, the folder it was last in once
    #: the row is gone, ``None`` when nothing can vouch for it. Never on the wire.
    vouch_id: NodeId | None = None
    #: The principals (``(kind, id)``) whose grant on this node a change on
    #: this page withdrew. Never on the wire.
    withdrawn: tuple[tuple[str, UUID], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """The wire shape. A tombstone carries an id and nothing else.

        Deliberately not ``asdict``: a tombstone rendering ``"name": null``
        would still confirm the node's existence to a caller who lost access to
        it, and a client applying the page would learn a key it must not have.
        """
        if self.deleted:
            return {"id": str(self.id), "deleted": True}
        return {
            "id": str(self.id),
            "deleted": False,
            "driveId": None if self.drive_id is None else str(self.drive_id),
            "parentId": None if self.parent_id is None else str(self.parent_id),
            "kind": self.kind,
            "name": self.name,
            "etag": self.etag,
            "size": self.size,
            "starred": self.starred,
            "contentHash": self.content_hash,
            "mimeType": self.mime_type,
            "scanState": self.scan_state,
        }


@dataclass(frozen=True, slots=True)
class DeltaPage:
    """Items plus exactly one link: more to come, or caught up."""

    items: tuple[DeltaItem, ...]
    next_link: DeltaToken | None
    delta_link: DeltaToken | None


#: Whether this principal may still see a node. Takes the id rather than an ORM
#: instance so the page stays one query; the backend passes a closure over
#: ``enforce()``, which is what makes a revoke effective on the very next page.
Readable = Callable[[NodeId], bool]


def _allow_all(node_id: NodeId) -> bool:
    return True


#: Whether a principal a withdrawn grant named is this caller.
NamesCaller = Callable[[str, UUID], bool]


def _names_nobody(kind: str, principal_id: UUID) -> bool:
    return False


def shown_to(
    item: DeltaItem, readable: Readable, names_caller: NamesCaller = _names_nobody
) -> DeltaItem | None:
    """What this caller's page carries for ``item``, or ``None`` to leave it out.

    The one place the feed's visibility rule is spelled (see the module
    docstring). A node the vouching node makes readable is shown as it is; a
    node whose grant was withdrawn from this caller is shown as a bare
    tombstone; anything else is not mentioned.
    """
    if item.vouch_id is not None and readable(item.vouch_id):
        return item
    if any(names_caller(kind, principal_id) for kind, principal_id in item.withdrawn):
        return DeltaItem(id=item.id, deleted=True)
    return None


@asynccontextmanager
async def _reading_the_outbox(repo: FilesRepo) -> AsyncIterator[None]:
    """Step out of the Files role for the join, then take it back.

    ``event_outbox`` is a platform table and ``alkera_files_app`` deliberately
    holds no grant on it; granting one would widen the role every Files
    statement runs as. The window goes to the session's outer role (the tenant
    role on a bound request, the login on a sweep; see
    :func:`alkera_core.db.tenant_session.stepped_out`). Both role changes are
    transaction-scoped, so nothing leaks onto the next borrower of a pooled
    connection — and because RLS does not bind under the login role, the
    statement carries the org predicate on BOTH sides of the join itself.

    The cleanup may never decide the outcome. A body that failed has usually
    aborted the transaction, so taking the role back answers ``25P02`` — and an
    exception raised while a context manager unwinds *replaces* the one being
    unwound, which is how a real feed error reaches the caller as an
    unattributable ``InFailedSQLTransactionError``. So the restore is
    best-effort whenever the body raised, and the body's exception is what
    propagates. It is still attempted, because a failure that did NOT poison
    the transaction (a plain Python error) would otherwise leave the rest of
    the unit of work running as the login role — no RLS, every grant.
    """
    repo._require_open()
    session = repo.session
    async with stepped_out(session):
        yield


class DeltaService:
    """One drive's change feed."""

    def __init__(
        self,
        repo: FilesRepo,
        ctx: ActingContext,
        clock: Clock,
        store: DomainStore | None = None,
        *,
        checkpoints: Checkpoints = _NO_CHECKPOINTS,
        signing_key: str | None = None,
    ) -> None:
        self._repo = repo
        self._ctx = ctx
        self._clock = clock
        self._store = store
        self._checkpoints = checkpoints
        self._key = signing_key

    @property
    def key(self) -> str:
        """The token signing key, resolved late so settings are read at use."""
        if self._key is not None:
            return self._key
        from alkera_core.config import settings

        return settings.effective_files_content_signing_key

    async def latest_token(self, drive_id: DriveId) -> DeltaToken:
        """A token meaning "everything so far": reading with it returns nothing."""
        boundary = await self._boundary(drive_id)
        return self._token(drive_id, boundary)

    async def read(
        self,
        drive_id: DriveId,
        *,
        token: DeltaToken | None,
        principal: ActingContext | None = None,
        limit: int = DEFAULT_LIMIT,
        readable: Readable = _allow_all,
        names_caller: NamesCaller = _names_nobody,
    ) -> DeltaPage:
        """One page of changes after ``token``, re-authorized item by item.

        ``token=None`` starts from the beginning of the retained window — a full
        catch-up. A token for another drive, from an older generation, or older
        than the retained window is refused rather than silently answered with a
        page that has a hole in it.
        """
        if limit < 1 or limit > DELTA_MAX_LIMIT:
            raise InvalidRequest(f"limit must be 1..{DELTA_MAX_LIMIT}, got {limit}")
        cursor = _START if token is None else self._checked(token, drive_id)

        me = (principal or self._ctx).effective_user_id
        rows = await self._rows(drive_id, cursor=cursor, limit=limit + 1, me=me)
        await self._checkpoints.reach(CP_ROWS_READ)

        overflowed = len(rows) > limit
        page = rows[:limit]
        # `dict` keeps first-seen order and takes the last value, which is
        # exactly "latest state by id" without reordering the feed. The folder a
        # node was last in is kept from the latest row that named one, because
        # the row that ends a node's life does not always carry it.
        seen: dict[NodeId, DeltaItem] = {}
        last_parent: dict[NodeId, NodeId] = {}
        withdrawn: dict[NodeId, tuple[tuple[str, UUID], ...]] = {}
        for row in page:
            node_id = NodeId(_uuid(row.entity_id))
            if row.payload_parent_id:
                last_parent[node_id] = NodeId(_uuid(row.payload_parent_id))
            withdrawn[node_id] = withdrawn.get(node_id, ()) + _withdrawn(row.withdrawn)
            if row.node_id is None:
                seen[node_id] = DeltaItem(
                    id=node_id,
                    deleted=True,
                    vouch_id=last_parent.get(node_id),
                    withdrawn=withdrawn[node_id],
                )
            elif row.trashed_at is not None:
                seen[node_id] = DeltaItem(
                    id=node_id, deleted=True, vouch_id=node_id, withdrawn=withdrawn[node_id]
                )
            else:
                seen[node_id] = DeltaItem(
                    id=node_id,
                    deleted=False,
                    drive_id=DriveId(row.node_drive_id),
                    parent_id=None if row.parent_id is None else NodeId(row.parent_id),
                    kind=row.kind,
                    name=row.name_display,
                    etag=row.etag,
                    size=row.size,
                    starred=bool(row.starred),
                    content_hash=row.content_hash,
                    mime_type=row.mime_sniffed,
                    scan_state=row.scan_state,
                    vouch_id=node_id,
                    withdrawn=withdrawn[node_id],
                )
        decided = (shown_to(item, readable, names_caller) for item in seen.values())
        shown = tuple(item for item in decided if item is not None)

        # The page is ordered by `(xmin, id)` and every row in it sorts after
        # the cursor, so the last row IS the new cursor — no max() to take.
        last = (
            _Cursor(xid=int(page[-1].cursor_xid), outbox_id=int(page[-1].outbox_id))
            if page
            else cursor
        )
        made = self._token(drive_id, last)
        result = DeltaPage(
            items=shown,
            next_link=made if overflowed else None,
            delta_link=None if overflowed else made,
        )
        await self._checkpoints.reach(CP_PAGE_READY)
        return result

    # ---- internals -------------------------------------------------------

    def _token(self, drive_id: DriveId, cursor: _Cursor) -> DeltaToken:
        return DeltaToken(
            drive_id=drive_id,
            outbox_id=cursor.outbox_id,
            issued_at=self._clock.now(),
            generation=TOKEN_GENERATION,
            cursor_xid=cursor.xid,
        )

    def _checked(self, token: DeltaToken, drive_id: DriveId) -> _Cursor:
        """The cursor ``token`` names, or ``DeltaExpired`` if it cannot be honoured."""
        if token.drive_id != drive_id:
            raise InvalidRequest("delta token names another drive")
        if token.generation != TOKEN_GENERATION:
            # The ids the token cursors over no longer mean what they did, so
            # the client cannot even tell which of its own writes landed.
            raise DeltaExpired(RESYNC_UPLOAD, "the feed was rebuilt under this token")
        if token.issued_at is None or self._clock.now() - token.issued_at > DELTA_RETENTION:
            # No mint time is treated as beyond the window rather than as "just
            # now": a token whose age cannot be established must not be honoured
            # against an outbox that only retains `DELTA_RETENTION` of history.
            raise DeltaExpired(RESYNC_APPLY, "the token is older than the retained window")
        if token.cursor_xid == 0:
            # An id-only cursor: either the drive had nothing deliverable when it
            # was minted, or it predates the transaction half. Neither can be
            # resumed from without risking a row whose id is below it and whose
            # transaction committed after — so it degrades to a catch-up over the
            # retained window. Duplicates are the feed's contract; gaps are not.
            return _START
        return _Cursor(xid=token.cursor_xid, outbox_id=token.outbox_id)

    async def _elsewhere(self) -> list[int]:
        """Which in-flight transactions are another database's business.

        Its own statement, and always the one before the read it narrows: see
        ``_LOCAL_XMIN`` for why that order is what keeps the boundary rising.
        """
        result = await self._repo.session.execute(_XIDS_ELSEWHERE)
        return [int(xid) for xid in result.scalar_one()]

    async def _rows(
        self, drive_id: DriveId, *, cursor: _Cursor, limit: int, me: UUID | None
    ) -> list[Any]:
        async with _reading_the_outbox(self._repo):
            elsewhere = await self._elsewhere()
            result = await self._repo.session.execute(
                _PAGE_SQL,
                {
                    "org": self._repo.scope.org_team_id,
                    "entity": DELTA_ENTITY,
                    "drive": str(drive_id),
                    "cursor_xid": cursor.xid,
                    "cursor_id": cursor.outbox_id,
                    "limit": limit,
                    "me": me,
                    "elsewhere": elsewhere,
                },
            )
            return list(result.all())

    async def _boundary(self, drive_id: DriveId) -> _Cursor:
        async with _reading_the_outbox(self._repo):
            elsewhere = await self._elsewhere()
            result = await self._repo.session.execute(
                _BOUNDARY_SQL,
                {
                    "org": self._repo.scope.org_team_id,
                    "entity": DELTA_ENTITY,
                    "drive": str(drive_id),
                    "elsewhere": elsewhere,
                },
            )
            row = result.one_or_none()
            if row is None:
                return _START
            return _Cursor(xid=int(row.cursor_xid), outbox_id=int(row.outbox_id))


def _uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _withdrawn(raw: Any) -> tuple[tuple[str, UUID], ...]:
    """The ``withdrawn`` principals a row carries; a malformed entry is skipped."""
    if not isinstance(raw, list):
        return ()
    found: list[tuple[str, UUID]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind, principal_id = entry.get("kind"), entry.get("id")
        if not isinstance(kind, str) or not isinstance(principal_id, str):
            continue
        try:
            found.append((kind, UUID(principal_id)))
        except ValueError:
            continue
    return tuple(found)


__all__ = [
    "CP_PAGE_READY",
    "CP_ROWS_READ",
    "DEFAULT_LIMIT",
    "DEFAULT_RETRY_AFTER",
    "DELTA_ENTITY",
    "DELTA_MAX_LIMIT",
    "DELTA_RETENTION",
    "RESYNC_APPLY",
    "RESYNC_UPLOAD",
    "DeltaExpired",
    "DeltaItem",
    "DeltaPage",
    "DeltaService",
    "NamesCaller",
    "Readable",
    "shown_to",
]
