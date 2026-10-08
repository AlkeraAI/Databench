"""The only place a Files table is queried.

Routes and services never touch an ``AsyncSession`` for Files rows: they hold a
``FilesRepo`` built with the tenant's ``OrgScope`` and every method adds the org
predicate itself. Row-level security in Postgres is the second net behind that
predicate, and it only binds once the transaction has assumed the unprivileged
``alkera_files_app`` role and stamped ``alkera.org_id`` — which is what
``transaction()`` does, transaction-scoped so a PgBouncer-pooled connection
cannot leak either setting into the next borrower.

A method called outside that transaction would run as the login role (a
superuser locally and on RDS) with no org setting, so it raises ``RepoUsageError``
rather than quietly reading the whole instance.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, TypeVar
from typing import cast as cast_typing

from sqlalchemy import (
    CursorResult,
    Select,
    Update,
    and_,
    cast,
    delete,
    false,
    func,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.expression import ColumnElement

from alkera_core.db.base import Base
from alkera_core.db.locking import LockRank, advisory_key, advisory_xact_lock, lock_rows
from alkera_core.db.tenant_session import restore_role, swap_role
from alkera_core.files.filters import starred_by
from alkera_core.files.ids import (
    AclId,
    DomainId,
    DriveId,
    NodeId,
    OperationId,
    OrgScope,
    SessionId,
    VersionId,
)
from alkera_core.files.path_labels import chain_inos
from alkera_core.models.files._types import LTREE
from alkera_core.models.files.acl import FileAcl, FileShare
from alkera_core.models.files.history import (
    FileConflict,
    FileDirStats,
    FileDirStatsDelta,
    FileHistory,
    FileStar,
    FileTrashOp,
)
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.stores import DedupDomain, FileDrive, FileStore
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.uploads import FileUploadPart, FileUploadSession
from alkera_core.models.files.versions import FileVersion

#: The role every Files statement runs as. It is NOLOGIN and holds no
#: ``BYPASSRLS``, so assuming it is what makes the policies bind even when the
#: connection logged in as a superuser.
APP_ROLE: Final = "alkera_files_app"
#: The GUC the RLS policy compares ``org_team_id`` against.
ORG_SETTING: Final = "alkera.org_id"

#: How many ids one statement addresses. A caller's list of nodes is unbounded —
#: a chat's attachments grow for as long as the conversation does — while a
#: PostgreSQL statement carries at most 32767 bound parameters and an id costs
#: one apiece, so a read spelled as a single ``IN`` stops answering at all once
#: the list passes that ceiling. Every read given a caller's list therefore goes
#: a batch at a time. It is well under the ceiling because a batch's follow-up
#: statements bind several parameters per id — the ancestor chains most of all.
ID_BATCH: Final = 1_000

_BatchItem = TypeVar("_BatchItem")


def id_batches(ids: Sequence[_BatchItem]) -> Iterator[Sequence[_BatchItem]]:
    """``ids`` in :data:`ID_BATCH`-sized slices, in order."""
    for start in range(0, len(ids), ID_BATCH):
        yield ids[start : start + ID_BATCH]


#: How many labels of ``path_ids`` the subtree index keys on. A GiST key holds
#: the whole indexed value, and a page cannot hold an unbounded path: an index
#: over the full column throws "failed to add item to index page" once a tree is
#: deep enough. Truncating the key bounds it, and a 1,024-deep tree then
#: indexes like any other; a root deeper than this still answers, through
#: the exact filter :meth:`FilesRepo.subtree_predicate` adds for it.
SUBTREE_INDEX_DEPTH: Final = 128


#: The bind every :func:`subtree_sql` fragment needs, spread into the parameter
#: dict of the statement that carries it (``{..., **SUBTREE_DEPTH_BIND}``). The
#: depth is a bind rather than a literal so the SQL text is identical whatever
#: the ceiling is, and a caller cannot get the fragment without also getting the
#: value it reads.
SUBTREE_DEPTH_BIND: Final = MappingProxyType({"subtree_depth": SUBTREE_INDEX_DEPTH})


def subtree_sql(descendant: str, ancestor: str) -> str:
    """Raw SQL for "``descendant`` is at or under ``ancestor``".

    The textual twin of :meth:`FilesRepo.subtree_predicate`, for the statements
    that are written as SQL rather than built from the ORM. Both spell the same
    two conjuncts — the truncated-prefix comparison the GiST index is built on,
    then the exact ancestry test as a filter over the rows it narrowed — so a
    change to the index can never leave one of them behind. Each argument is a
    ``ltree`` expression: a column (``"n.path_ids"``) or a cast bind
    (``"CAST(:path AS ltree)"``). ``@>`` is only ``<@`` with its operands the
    other way round, so asking for the ancestors of a path is this function with
    the arguments swapped rather than a second spelling.

    The statement must bind :data:`SUBTREE_DEPTH_BIND`.

    Every caller marks its statement ``# noqa: S608``. The rule fires on any SQL
    built by interpolation, and it is right to — but what is interpolated here
    is a column or bind NAME written in the source of this package, never a
    value: values reach Postgres only as bind parameters, so there is nothing
    for a caller's input to escape into.
    """
    depth = f":{next(iter(SUBTREE_DEPTH_BIND))}"
    return (
        f"(subpath({descendant}, 0, {depth}) <@ subpath({ancestor}, 0, {depth})"
        f" AND {descendant} <@ {ancestor})"
    )


def ancestor_chain_predicate(anchor: Any) -> ColumnElement[bool]:
    """An ORM predicate for "is on ``anchor``'s root-to-node chain".

    The other ltree ancestry question Files asks: not "everything under this
    root" but "the handful of rows above this one" — and, since ``@>`` is only
    ``<@`` with its operands the other way round, it is the same two conjuncts
    :meth:`FilesRepo.subtree_predicate` spells, swapped. The first compares the
    *truncated* paths, which is the expression ``ix_file_nodes_path_ids`` is
    built on, so the planner has an index to descend; the second is the exact
    ancestry test, which the truncation can only over-admit (a prefix of a path
    is a prefix of that path's first 128 labels), never miss.

    Spelling only the exact test leaves the planner nothing indexable, and it
    answers with a scan of every node that shares the anchor's drive — once per
    anchor. That is invisible on a small tree and quadratic on a real one: a
    page of five hundred items over a hundred-thousand-node drive read tens of
    millions of rows to decide who may see them.
    """
    truncated = func.subpath(FileNode.path_ids, 0, SUBTREE_INDEX_DEPTH)
    anchor_truncated = func.subpath(anchor.path_ids, 0, SUBTREE_INDEX_DEPTH)
    return and_(
        truncated.op("@>")(anchor_truncated),
        FileNode.path_ids.op("@>")(anchor.path_ids),
    )


def _within(node: Any, ancestor: Any) -> ColumnElement[bool]:
    """``node`` is at or under ``ancestor``, both aliases of ``file_nodes``:
    the index-shaped truncated comparison, then the exact test."""
    return and_(
        func.subpath(node.path_ids, 0, SUBTREE_INDEX_DEPTH).op("<@")(
            func.subpath(ancestor.path_ids, 0, SUBTREE_INDEX_DEPTH)
        ),
        node.path_ids.op("<@")(ancestor.path_ids),
    )


def _within_path(node: Any, path: str) -> ColumnElement[bool]:
    """``node`` is at or under the folder whose ``path_ids`` is ``path``."""
    predicate: ColumnElement[bool] = func.subpath(node.path_ids, 0, SUBTREE_INDEX_DEPTH).op("<@")(
        func.subpath(cast(path, LTREE), 0, SUBTREE_INDEX_DEPTH)
    )
    if path.count(".") + 1 > SUBTREE_INDEX_DEPTH:
        predicate = and_(predicate, node.path_ids.op("<@")(cast(path, LTREE)))
    return predicate


def _charging_folder(folder: Any) -> ColumnElement[bool]:
    """``folder`` is one whose contents are charged to whoever owns it: a
    member's home, or a folder that IS an object (a chat, a chat template)
    wherever it is filed. Ownership is ``created_by`` on the folder itself,
    which is what both are stamped with at birth.

    The registries are read on use: ``drives`` and ``objects_bridge`` import
    this module, so neither can be imported here at load time.
    """
    from alkera_core.files.drives import HOME_DEPTH, HOME_SUBTYPE
    from alkera_core.files.objects_bridge import FOLDER_OBJECT_KINDS

    return and_(
        folder.kind == "folder",
        or_(
            and_(folder.subtype == HOME_SUBTYPE, folder.depth == HOME_DEPTH),
            and_(
                folder.subtype.in_(tuple(FOLDER_OBJECT_KINDS)),
                folder.target_object_id.is_not(None),
            ),
        ),
    )


def charged_bytes(
    subject: uuid.UUID,
    *,
    drive_id: uuid.UUID,
    org_team_id: uuid.UUID,
    scope_path: str | None = None,
) -> ColumnElement[int]:
    """Σ size of the live files charged to ``subject`` in one drive, as a
    scalar expression — optionally only those at or under ``scope_path``.

    A member is charged for every file they created, and for every file in
    their home or inside a chat or template folder they own, whoever wrote it:
    a chat's output is written by the box on its machine credential and by
    platform jobs, and a queued copy runs as the platform too — never under
    the member's id — and it is still the member's storage. The storage card,
    the admin roster and the member-limit check all read this one figure, so
    what is shown is what is enforced.

    Two disjoint halves so each keeps its index: the member's own files by
    ``created_by``, then the files others wrote inside the member's charging
    folders, found through those (few) folders and a subtree join. A file
    under two of them — a chat in the home — is counted once.
    """
    own = FileNode
    own_conditions: list[ColumnElement[bool]] = [
        own.drive_id == drive_id,
        own.org_team_id == org_team_id,
        own.created_by == subject,
        own.trashed_at.is_(None),
        own.kind == "file",
    ]
    if scope_path is not None:
        own_conditions.append(_within_path(own, scope_path))
    created = select(func.coalesce(func.sum(own.size), 0)).where(*own_conditions)

    folder = aliased(FileNode, name="charged_folder")
    inside = aliased(FileNode, name="charged_inside")
    counted = aliased(FileNode, name="charged_counted")
    in_charging_folder = (
        select(inside.id)
        .join(
            folder,
            and_(
                folder.drive_id == inside.drive_id,
                folder.org_team_id == inside.org_team_id,
                _within(inside, folder),
            ),
        )
        .where(
            folder.drive_id == drive_id,
            folder.org_team_id == org_team_id,
            folder.created_by == subject,
            _charging_folder(folder),
            folder.trashed_at.is_(None),
            inside.kind == "file",
            inside.trashed_at.is_(None),
        )
    )
    others_conditions: list[ColumnElement[bool]] = [
        counted.drive_id == drive_id,
        counted.org_team_id == org_team_id,
        counted.created_by.is_distinct_from(subject),
        counted.id.in_(in_charging_folder),
    ]
    if scope_path is not None:
        others_conditions.append(_within_path(counted, scope_path))
    written_for = select(func.coalesce(func.sum(counted.size), 0)).where(*others_conditions)
    return created.scalar_subquery() + written_for.scalar_subquery()


def ancestor_chain_by_ino(anchors: Iterable[FileNode]) -> ColumnElement[bool]:
    """Every node on the chain of any of ``anchors``, addressed by ino.

    The ltree spelling of the same question — :func:`ancestor_chain_predicate` —
    is index-served for the table owner and NOT for the role the routes run as.
    Under ``alkera_files_app`` the ``file_nodes`` policy is FORCE'd, and
    PostgreSQL refuses to evaluate a qual ahead of a pending security qual
    unless that qual is leakproof; ``subpath()`` and the ltree containment
    operators are not, so however they are spelled they are demoted to a filter
    and the planner's only remaining handle is ``drive_id`` — a read of every
    node in the drive, once per anchor. ``uuid_eq`` and ``int8eq`` ARE
    leakproof, so this spelling keeps ``uq_file_nodes_drive_ino`` as the index
    condition under the role as well, which is the only place it matters.

    The caller must already hold the anchor rows, because the inos come from
    their ``path_ids``: this is a predicate over a page that has been read, not
    a join that reads it. That is the trade — one more statement, in exchange
    for a chain lookup that no longer grows with the drive.
    """
    by_drive: dict[uuid.UUID, set[int]] = {}
    for anchor in anchors:
        by_drive.setdefault(anchor.drive_id, set()).update(chain_inos(anchor.path_ids))
    if not by_drive:
        return false()
    return or_(
        *(
            and_(FileNode.drive_id == drive_id, FileNode.ino.in_(sorted(inos)))
            for drive_id, inos in by_drive.items()
        )
    )


@dataclass(frozen=True, slots=True)
class NodeRow:
    """A node with the head version and the caller's star it was loaded with.

    Kept beside the node rather than folded onto it because neither fact belongs
    to the row: the head is another table's row and the star is one caller's
    answer, so an ORM instance carrying them would be wrong for the next reader.
    """

    node: FileNode
    head: FileVersion | None
    starred: bool


class RepoUsageError(RuntimeError):
    """A repo method was called outside ``FilesRepo.transaction()``.

    Raised rather than run: outside the transaction the role and the org
    setting are not in force, so the statement would be unprotected by RLS.
    """


class ScopeError(RuntimeError):
    """A raw statement was handed to ``execute_scoped`` that it cannot scope.

    Either it selects from a table that is not a Files tenant table (so the org
    predicate would be meaningless) or from more than one, where the predicate
    would have to name which.
    """


def _tenant_tables() -> dict[str, type[Base]]:
    """Every Files ORM class that carries ``org_team_id``, by table name."""
    mapped: dict[str, type[Base]] = {}
    for mapper in Base.registry.mappers:
        model = mapper.class_
        table = model.__table__
        if not table.name.startswith(("file_", "dedup_domains", "manifest_terms")):
            continue
        if "org_team_id" not in table.c:
            continue
        mapped[table.name] = model
    return mapped


class FilesRepo:
    """Org-scoped access to the Files tables.

    Construct one per request with the acting context's org; hold it for the
    life of the unit of work. Nothing here caches rows across transactions.
    """

    def __init__(self, session: AsyncSession, scope: OrgScope) -> None:
        self._session = session
        self._scope = scope
        self._open = False
        self._joined = False

    @classmethod
    def joined(cls, session: AsyncSession, scope: OrgScope) -> FilesRepo:
        """A repo that borrows a caller-owned transaction instead of owning one.

        A bridge call (a team getting its folder) and a route that runs several
        collaborators inside one idempotency claim both need Files work to live
        or die with the caller's unit of work, so ``transaction()`` on this
        handle stamps the same role and org setting but opens a SAVEPOINT and
        never commits: the caller decides. Re-entering it is the same unit of
        work rather than a refusal, so a service that opens its own transaction
        composes with a route that already opened one.
        """
        repo = cls(session, scope)
        repo._joined = True
        return repo

    @property
    def scope(self) -> OrgScope:
        return self._scope

    @property
    def session(self) -> AsyncSession:
        """The underlying session, for the callers that own the unit of work.

        Reading rows through it is what the hygiene test forbids; it is exposed
        so a service can commit or flush the transaction it opened.
        """
        return self._session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[FilesRepo]:
        """Open the Files transaction and stamp the role and the org.

        ``SET LOCAL`` and ``set_config(..., true)`` are both transaction-scoped:
        they revert at COMMIT or ROLLBACK, so a connection handed back to
        PgBouncer carries neither into the next transaction. Committing here is
        what ends the elevated-but-scoped window, so the caller's own writes go
        through the same guard rails as the reads.
        """
        if self._open:
            if not self._joined:
                raise RepoUsageError("FilesRepo.transaction() is already open")
            yield self
            return
        if not self._session.in_transaction():
            await self._session.begin()
        savepoint = await self._session.begin_nested() if self._joined else None
        # The role in force is remembered, so a joined unit of work hands back
        # exactly that role (the request's tenant role, or the login inside a
        # cross-tenant window) rather than one this repo picked.
        prior_role = await swap_role(self._session, APP_ROLE)
        await self._session.execute(
            text("SELECT set_config(:name, :value, true)"),
            {"name": ORG_SETTING, "value": str(self._scope.org_team_id)},
        )
        self._open = True
        try:
            yield self
        except BaseException:
            self._open = False
            if savepoint is None:
                await self._session.rollback()
            elif savepoint.is_active:
                await savepoint.rollback()
            raise
        self._open = False
        if savepoint is None:
            await self._session.commit()
            return
        if savepoint.is_active:
            await savepoint.commit()
        # The role is handed back rather than left in force: the caller's next
        # statement is its own again, and RELEASE SAVEPOINT does not revert it.
        await restore_role(self._session, prior_role)

    def _require_open(self) -> None:
        if not self._open:
            raise RepoUsageError(
                "FilesRepo methods require an open transaction: "
                "use `async with repo.transaction():`"
            )

    def _scoped(self, model: Any) -> ColumnElement[bool]:
        predicate: ColumnElement[bool] = model.org_team_id == self._scope.org_team_id
        return predicate

    # ---- nodes -----------------------------------------------------------

    async def node(self, id: NodeId) -> FileNode | None:
        row = await self.node_row(id, starred_for=None)
        return None if row is None else row.node

    async def node_row(self, id: NodeId, *, starred_for: uuid.UUID | None) -> NodeRow | None:
        """The node plus the two facts a single-item read renders beside it.

        The head version and the caller's star are folded into the ONE statement
        that loads the node rather than fetched after it: a second round trip
        would render the same wire item at a higher latency, and — because the
        star is per caller and the head moves under a concurrent upload — a
        racier one. A node with no head version outer-joins to nothing, and a
        caller with no user behind it stars nothing, so both facts are cheap
        constants on the paths that have no answer.
        """
        self._require_open()
        head = aliased(FileVersion)
        stmt = (
            select(FileNode, head, starred_by(FileNode, starred_for).label("starred"))
            .outerjoin(
                head,
                and_(
                    head.id == FileNode.head_version_id,
                    head.org_team_id == FileNode.org_team_id,
                ),
            )
            .where(FileNode.id == id, self._scoped(FileNode))
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return NodeRow(node=row[0], head=row[1], starred=bool(row[2]))

    async def node_rows(
        self, ids: Iterable[NodeId], *, starred_for: uuid.UUID | None
    ) -> dict[uuid.UUID, NodeRow]:
        """:meth:`node_row` for a whole set, keyed by id, in one statement per
        :data:`ID_BATCH` ids.

        A batched item read renders the same wire item the single read does
        -- head version and caller's star included -- so it loads the same
        three facts in the same statement rather than paying a round trip per
        id, which is the shape it exists to replace. An id that is not this
        org's, or does not exist, is simply absent.
        """
        self._require_open()
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return {}
        head = aliased(FileVersion)
        found: dict[uuid.UUID, NodeRow] = {}
        for batch in id_batches(wanted):
            stmt = (
                select(FileNode, head, starred_by(FileNode, starred_for).label("starred"))
                .outerjoin(
                    head,
                    and_(
                        head.id == FileNode.head_version_id,
                        head.org_team_id == FileNode.org_team_id,
                    ),
                )
                .where(FileNode.id.in_(batch), self._scoped(FileNode))
            )
            for row in (await self._session.execute(stmt)).all():
                found[row[0].id] = NodeRow(node=row[0], head=row[1], starred=bool(row[2]))
        return found

    async def nodes(self, ids: Iterable[NodeId]) -> Sequence[FileNode]:
        self._require_open()
        wanted = list(ids)
        if not wanted:
            return []
        rows: list[FileNode] = []
        for batch in id_batches(wanted):
            stmt = select(FileNode).where(FileNode.id.in_(batch), self._scoped(FileNode))
            rows.extend((await self._session.execute(stmt)).scalars().all())
        return rows

    async def chain(self, node: FileNode) -> Sequence[FileNode]:
        """The node's ancestors and itself, root first, in one query.

        ``path_ids`` holds the ancestors' inos as ltree labels, and an ino is
        unique per drive, so the chain is one ``(drive_id, ino IN …)`` read
        rather than a recursive CTE; the rows come back unordered and are put
        back into path order here.
        """
        self._require_open()
        inos = chain_inos(node.path_ids)
        stmt = select(FileNode).where(
            FileNode.drive_id == node.drive_id,
            FileNode.ino.in_(inos),
            self._scoped(FileNode),
        )
        rows = {row.ino: row for row in (await self._session.execute(stmt)).scalars().all()}
        return [rows[ino] for ino in inos if ino in rows]

    async def chain_ids(self, node_id: NodeId) -> Sequence[uuid.UUID]:
        """The ids of ``node_id``'s ancestors and itself, root first, in one query.

        A listing needs the chain but not the rows, and its budget is three
        statements for the whole page — so this is one self-join on the derived
        path (``ancestor.path_ids @> node.path_ids``) rather than a read of the
        node followed by a read of its ancestors. An empty result means the node
        is not in this org.
        """
        self._require_open()
        anchor = aliased(FileNode, name="anchor")
        stmt = (
            select(FileNode.id)
            .select_from(anchor)
            .join(
                FileNode,
                and_(
                    FileNode.drive_id == anchor.drive_id,
                    ancestor_chain_predicate(anchor),
                ),
            )
            .where(anchor.id == node_id, self._scoped(anchor), self._scoped(FileNode))
            .order_by(func.nlevel(FileNode.path_ids))
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def children_page(
        self,
        parent_id: NodeId,
        *,
        after: tuple[str, uuid.UUID] | None,
        limit: int,
    ) -> Sequence[FileNode]:
        """One keyset page of a folder's live children.

        The key is ``(name_key, id)`` — ``name_key`` alone is not unique, since
        two byte-different names fold to the same key — so a page boundary that
        lands inside a fold group still resumes exactly once per row.
        """
        self._require_open()
        conditions: list[ColumnElement[bool]] = [
            FileNode.parent_id == parent_id,
            FileNode.trashed_at.is_(None),
            self._scoped(FileNode),
        ]
        if after is not None:
            key, last_id = after
            conditions.append(
                (FileNode.name_key > key) | and_(FileNode.name_key == key, FileNode.id > last_id)
            )
        stmt = (
            select(FileNode)
            .where(*conditions)
            .order_by(FileNode.name_key, FileNode.id)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def lock_node(self, id: NodeId) -> FileNode | None:
        """Read one node ``FOR NO KEY UPDATE``, after the lease folder above it.

        A node under a live lease is written by that lease's holder too, and
        the holder's tree report takes no drive row: it takes the leased
        folder, then the lease row, then rows anywhere beneath it. So every
        writer here takes the same folder first (:meth:`lock_lease_gate`) and
        the node after it, one queue per leased folder rather than one per
        org. A node under no lease locks only itself.

        No-key strength, as :meth:`lock_drive`: two writers of one node still
        exclude each other, while a transaction that merely references the
        node through a foreign key -- the holder's live batch inserting the
        node's in-flight row, the lease row re-checking its key to the leased
        folder, an upload session opening on the node -- takes ``FOR KEY
        SHARE``, which a no-key lock admits and a full one queues. Every such
        key share is taken after the lease row, which every writer here takes
        last, so at full strength it was an inversion the fixed order could not
        express: the holder's first report of a file its own move already held
        deadlocked against that move, the move holding the node and waiting on
        the lease row, the batch holding the lease row and waiting on the node.

        ``populate_existing`` is what makes this a read *under* the lock.
        Every Files mutation is a ``text()`` statement the mapper never sees,
        so an instance the session already holds keeps the ``path_ids``,
        ``etag`` and ``trashed_at`` it was loaded with — and without this the
        row the lock just fetched is discarded in favour of exactly those. A
        create that derives its child's path from one hangs the node off a path
        no ancestor carries, where every read that asks by path prefix — the
        subtree sweeps included — walks straight past it.
        """
        self._require_open()
        await self.lock_lease_gate(id)
        stmt = (
            select(FileNode)
            .where(FileNode.id == id, self._scoped(FileNode))
            .execution_options(populate_existing=True)
        )
        locked = await lock_rows(self._session, LockRank.FILES_NODE, stmt, strength="no_key_update")
        return locked.scalar_one_or_none()

    async def lock_lease_gate(self, id: NodeId) -> NodeId | None:
        """Lock the folder that gates writes under ``id``'s lease, and name it.

        The gate is the OUTERMOST folder at or above ``id`` that carries a live
        lease, not the nearest: a holder may take a lease strictly inside one it
        already holds, and the outer lease's report writes rows under the inner
        one. Two reports and every writer beneath either must meet on one row,
        or the outer report and an inner writer each hold a row the other wants.

        Found by a plain read and then locked by id, so a node no lease covers
        -- nearly every one -- costs one read and takes no lock at all. The read
        is one statement whatever the depth: the org's live leases are few, and
        each is tested against ``id``'s path with the subtree builder. A lease
        that ends between the read and the lock leaves a folder locked that
        needed no lock, which costs a wait and never an inversion.

        Taken at no-key strength like every node lock here (:meth:`lock_node`),
        so a key share on the folder -- the lease row re-checking its key to
        it, an entry the holder reports on the folder itself -- never queues
        behind the gate.
        """
        self._require_open()
        found = (
            await self._session.execute(
                text(
                    "SELECT g.id FROM file_nodes g "  # noqa: S608 - interpolates the builder's text
                    "JOIN file_leases l ON l.node_id = g.id AND l.org_team_id = g.org_team_id "
                    "JOIN file_nodes t ON t.org_team_id = g.org_team_id AND t.id = :id "
                    "WHERE g.org_team_id = :org AND l.released_at IS NULL "
                    "AND l.reaped_at IS NULL AND l.expires_at > now() "
                    f"AND {subtree_sql('t.path_ids', 'g.path_ids')} "
                    "ORDER BY g.depth ASC LIMIT 1"
                ),
                {"org": self.scope.org_team_id, "id": id, **SUBTREE_DEPTH_BIND},
            )
        ).scalar_one_or_none()
        if found is None:
            return None
        await lock_rows(
            self._session,
            LockRank.FILES_NODE,
            select(FileNode.id).where(FileNode.id == found, self._scoped(FileNode)),
            strength="no_key_update",
        )
        return NodeId(found)

    async def siblings(self, parent_id: NodeId) -> Sequence[FileNode]:
        """Every live child of one folder.

        Read whole rather than paged because the callers — the conflict rename
        and the case/normalization fold report — are answers about the folder as
        a set, not about a page of it.
        """
        self._require_open()
        stmt = select(FileNode).where(
            FileNode.parent_id == parent_id,
            FileNode.trashed_at.is_(None),
            self._scoped(FileNode),
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def charging_owners_above(
        self, drive_id: DriveId, paths: Iterable[str]
    ) -> list[tuple[str, uuid.UUID]]:
        """``(path_ids, created_by)`` of every charging folder (a member's home,
        a folder that IS an object) at or above any of ``paths``: the folders
        whose owner the bytes under them are charged to, by :func:`charged_bytes`'
        own predicate. One statement, each path served by the ancestor index."""
        self._require_open()
        above = [
            and_(
                func.subpath(FileNode.path_ids, 0, SUBTREE_INDEX_DEPTH).op("@>")(
                    func.subpath(cast(path, LTREE), 0, SUBTREE_INDEX_DEPTH)
                ),
                FileNode.path_ids.op("@>")(cast(path, LTREE)),
            )
            for path in set(paths)
        ]
        if not above:
            return []
        stmt = select(FileNode.path_ids, FileNode.created_by).where(
            self._scoped(FileNode),
            FileNode.drive_id == drive_id,
            FileNode.created_by.is_not(None),
            _charging_folder(FileNode),
            or_(*above),
        )
        return [(str(row[0]), row[1]) for row in (await self._session.execute(stmt)).all()]

    async def children_of_container(
        self, root_id: NodeId, container: bytes, *, created_by: uuid.UUID
    ) -> Sequence[FileNode]:
        """Every live child of the top-level folder named ``container`` that
        ``created_by`` created, oldest first.

        Every kind, not only folders: a container's children are not all what
        the skeleton put there. An org admin outranks the container by descent,
        so a founder who was landed on ``/home`` before the drive named her home
        has the receipts she dropped there and the folders she made there
        sitting beside her real folder, every one stamped with her id. The
        caller tells the home apart from the rest by the home's identity
        (:func:`alkera_core.files.drives.lookup_home`), and the rest is what it
        has to tidy — so one read answers both.

        One statement for a two-level answer. Reading the container first and
        then listing it is two round trips for something the database joins in
        one, and this is on the drive read — the first request every client
        makes and the one whose cost every later screen waits on.
        """
        self._require_open()
        parent = aliased(FileNode)
        stmt = (
            select(FileNode)
            .join(parent, parent.id == FileNode.parent_id)
            .where(
                FileNode.created_by == created_by,
                FileNode.trashed_at.is_(None),
                parent.parent_id == root_id,
                parent.name == container,
                parent.trashed_at.is_(None),
                self._scoped(FileNode),
            )
            .order_by(FileNode.created_at, FileNode.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def live_operation_for(self, node_id: NodeId, *, kind: str) -> FileOp | None:
        """The queued or running operation of ``kind`` whose result is ``node_id``.

        A queued subtree move names the node it carries on ``result_node_id``
        while nothing on the node itself says so yet — the runner is what
        stamps the subtree ``moving``. A caller that would queue that move
        again asks here first, so the same node is never carried by two
        operations.
        """
        self._require_open()
        stmt = (
            select(FileOp)
            .where(
                FileOp.result_node_id == node_id,
                FileOp.kind == kind,
                FileOp.state.in_(("queued", "running")),
                self._scoped(FileOp),
            )
            .order_by(FileOp.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def nodes_named(self, drive_id: DriveId, wanted: Iterable[bytes]) -> Sequence[FileNode]:
        """Every live node in one drive whose name is one of ``wanted``.

        Path resolution's batched read: one statement for a whole path, so a
        256-segment path costs the same round trips as a 2-segment one and
        neither needs a recursive CTE.
        """
        self._require_open()
        segments = list(wanted)
        if not segments:
            return []
        stmt = select(FileNode).where(
            FileNode.drive_id == drive_id,
            FileNode.name.in_(segments),
            FileNode.trashed_at.is_(None),
            self._scoped(FileNode),
        )
        return list((await self._session.execute(stmt)).scalars().all())

    def subtree_predicate(self, root: FileNode) -> ColumnElement[bool]:
        """The one way to ask "at or under this node".

        Every subtree query in Files goes through here so the predicate and the
        index that serves it can never drift apart. The first conjunct compares
        the *truncated* paths, which is exactly the expression
        ``ix_file_nodes_path_ids`` is built on, so the planner can use it. For a
        root at or above :data:`SUBTREE_INDEX_DEPTH` the truncation is the whole
        path and that conjunct is the complete answer. For a deeper root it is
        only a prefix, so it admits cousins that share the first 128 labels;
        the exact ``path_ids <@ root.path_ids`` is then added as a filter over
        the (already index-narrowed) rows. Deep roots therefore work — they are
        merely slower, never wrong.
        """
        return self._subtree_predicate_for(root.path_ids)

    async def count_in_subtree(self, root: FileNode) -> int:
        """How many nodes sit at or under ``root``. One statement, no rows.

        The size of a subtree is a decision input — whether a caller's request
        can be answered by enumerating it or has to be answered by size alone —
        and asking it by counting the rows a ``SELECT`` returned is the very
        cost the answer is supposed to avoid.
        """
        self._require_open()
        stmt = (
            select(func.count())
            .select_from(FileNode)
            .where(self.subtree_predicate(root), self._scoped(FileNode))
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def subtree_page(
        self, root: FileNode, *, limit: int, after: tuple[int, uuid.UUID] | None = None
    ) -> Sequence[FileNode]:
        """One page of ``root``'s subtree, shallowest first, from ``after``.

        The predicate is :meth:`subtree_predicate`, ordered shallowest first;
        a walk over it costs one page of rows at a time rather than the whole
        subtree at once. The cursor is the order key — ``(depth,
        id)`` — because that pair is unique and because the walk's own
        correctness depends on the order: a node's parent is always on an
        earlier page than the node, so a refused folder is known to be refused
        before any of its children is decided.
        """
        self._require_open()
        stmt = select(FileNode).where(self.subtree_predicate(root), self._scoped(FileNode))
        if after is not None:
            depth, node_id = after
            stmt = stmt.where(
                or_(
                    FileNode.depth > depth,
                    and_(FileNode.depth == depth, FileNode.id > node_id),
                )
            )
        stmt = stmt.order_by(FileNode.depth, FileNode.id).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def nodes_by_path_prefix(self, prefix: str) -> Sequence[FileNode]:
        """Every node at or under one ``path_ids`` prefix, shallowest first.

        The path-addressed spelling of :meth:`subtree_page`, for the callers
        that hold a path rather than the row it came from. Both build the same
        predicate, so neither can outlive the index the other is planned against.
        """
        self._require_open()
        stmt = (
            select(FileNode)
            .where(self._subtree_predicate_for(prefix), self._scoped(FileNode))
            .order_by(FileNode.depth, FileNode.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def owned_bytes(self, drive_id: DriveId, subject: uuid.UUID) -> int:
        """Σ size of the live files charged to ``subject`` anywhere in one
        drive (see :func:`charged_bytes`)."""
        self._require_open()
        stmt = select(
            charged_bytes(subject, drive_id=drive_id, org_team_id=self._scope.org_team_id)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def owned_bytes_under(self, drive_id: DriveId, subject: uuid.UUID, path: str) -> int:
        """Σ size of the live files charged to ``subject`` at or under ``path``.

        The subtree half is the one predicate every subtree read is built on,
        so a member's usage inside a team folder is served by the path index
        like any other subtree question.
        """
        self._require_open()
        stmt = select(
            charged_bytes(
                subject,
                drive_id=drive_id,
                org_team_id=self._scope.org_team_id,
                scope_path=path,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    def _subtree_predicate_for(self, path: str) -> ColumnElement[bool]:
        """:meth:`subtree_predicate`, given only the root's ``path_ids``."""
        truncated = func.subpath(FileNode.path_ids, 0, SUBTREE_INDEX_DEPTH)
        root_truncated = func.subpath(cast(path, LTREE), 0, SUBTREE_INDEX_DEPTH)
        predicate: ColumnElement[bool] = truncated.op("<@")(root_truncated)
        if path.count(".") + 1 > SUBTREE_INDEX_DEPTH:
            predicate = and_(predicate, FileNode.path_ids.op("<@")(cast(path, LTREE)))
        return predicate

    async def lock_chain(
        self,
        drive_id: DriveId,
        parent_id: NodeId | None,
        node_id: NodeId | None,
        *,
        rewrites_subtree: bool = False,
    ) -> tuple[FileDrive | None, FileNode | None, FileNode | None]:
        """Take the write locks in the one order every Files mutation uses.

        namespace → lease gate → parent → node → lease row, always, because
        two mutations that take the same rows in opposite orders deadlock:

        * **the drive's namespace** first (:meth:`lock_namespace`), shared by a
          write that works under a folder (a create, a version, a share) and
          exclusive for one that ``rewrites_subtree`` (a move, a trash, a
          restore). Exclusive is what makes two moves in one drive mutually
          exclusive, so a pair of moves that would each pass its own cycle
          check cannot interleave between the check and the write, and what
          keeps a create from deriving its path under a folder a move is
          re-rooting. Shared writers never wait on each other: a create, an
          upload and a share in the same org go side by side. A holder's tree report takes it
          only when its batch trashes or moves something.
        * **the lease gate** -- the outermost live-leased folder above the
          parent or the node (:meth:`lock_lease_gate`), taken by
          :meth:`lock_node` before either. The tree report takes it with no
          namespace, so it is the row that orders every writer inside a lease
          against the holder's batches.
        * **the parent, then the node.**
        * **the lease row** last, by the fence (``leases.fenced_write``), which
          takes the gate itself first for a writer that locked no node.

        The drive row itself is read, not locked: only the quota decision
        takes it (:meth:`lock_drive`).
        """
        self._require_open()
        await self.lock_namespace(drive_id, exclusive=rewrites_subtree)
        drive = await self.drive(drive_id)
        parent = None if parent_id is None else await self.lock_node(parent_id)
        node = None if node_id is None else await self.lock_node(node_id)
        return drive, parent, node

    async def lock_namespace(self, drive_id: DriveId, *, exclusive: bool = False) -> None:
        """Hold the drive's tree shape until the transaction ends.

        Shared for a write under a folder: it reads the paths above it and
        needs them not to move. Exclusive for a write that rewrites the paths
        of a subtree. A transaction takes the strongest mode it will need
        first: a shared holder that asks for exclusive waits for every other
        shared holder, and two doing it at once deadlock.
        """
        self._require_open()
        await advisory_xact_lock(
            self._session,
            advisory_key("files-namespace", self._scope.org_team_id, drive_id),
            shared=not exclusive,
            rank=LockRank.FILES_NAMESPACE,
        )

    # ---- drives and domains ---------------------------------------------

    async def drive(self, id: DriveId) -> FileDrive | None:
        self._require_open()
        stmt = select(FileDrive).where(FileDrive.id == id, self._scoped(FileDrive))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def reload_drive(self, id: DriveId) -> FileDrive | None:
        """:meth:`drive`, overwriting the instance the session already holds.

        A rollback expires every instance its session loaded, and a caller
        that closed over one before it would otherwise refresh it on the next
        column read — synchronous IO an async session cannot do. Reloading
        rewrites that same instance, so every holder of it is fresh again.
        """
        self._require_open()
        stmt = (
            select(FileDrive)
            .where(FileDrive.id == id, self._scoped(FileDrive))
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # ---- which org a request addressed, before any scope exists ----------
    #
    # A box on its own machine credential has no org of its own: it addresses
    # the drive of an org it serves, and the org is not known until the drive
    # named in the request has been read. These reads run BEFORE a scoped repo
    # exists, as the platform, and answer the one question a scope needs — which
    # org this drive (or the drive behind this node or upload session) belongs
    # to. They are static and take the session explicitly so nothing about
    # them can be mistaken for a scoped read, and they hand back the drive row
    # or a drive id and never a node's content: whether the caller may act in
    # the org they name is the context builder's ``serves`` check and then the
    # policy's, both still to come.

    @staticmethod
    async def drive_anywhere(session: AsyncSession, id: DriveId) -> FileDrive | None:
        """The drive with this id, whichever org it belongs to."""
        return (
            await session.execute(select(FileDrive).where(FileDrive.id == id))
        ).scalar_one_or_none()

    @staticmethod
    async def org_drive_anywhere(session: AsyncSession, org_team_id: uuid.UUID) -> FileDrive | None:
        """``org_team_id``'s own drive, read without a scope."""
        return (
            await session.execute(
                select(FileDrive).where(
                    FileDrive.org_team_id == org_team_id, FileDrive.kind == "org"
                )
            )
        ).scalar_one_or_none()

    @staticmethod
    async def drive_of_node_anywhere(session: AsyncSession, node_id: NodeId) -> DriveId | None:
        """The drive the node with this id is on, or ``None``; the id alone."""
        found = (
            await session.execute(select(FileNode.drive_id).where(FileNode.id == node_id))
        ).scalar_one_or_none()
        return None if found is None else DriveId(found)

    @staticmethod
    async def drive_of_upload_anywhere(
        session: AsyncSession, upload_id: uuid.UUID
    ) -> DriveId | None:
        """The drive the upload session with this id was opened on, or ``None``."""
        found = (
            await session.execute(
                select(FileUploadSession.drive_id).where(FileUploadSession.id == upload_id)
            )
        ).scalar_one_or_none()
        return None if found is None else DriveId(found)

    @staticmethod
    async def chat_folder_anywhere(session: AsyncSession, chat_id: uuid.UUID) -> FileNode | None:
        """The live folder node of the chat with this id, whichever org it is
        in, or ``None``; the id alone. A trashed folder is no chat's folder
        any more. Unverified like the reads above: a caller that named a chat
        is decided on this node afterwards, by the policy, in the org the
        node's drive belongs to."""
        from alkera_core.files.authz.decider import CHAT_SUBTYPE

        return (
            (
                await session.execute(
                    select(FileNode).where(
                        FileNode.target_object_id == chat_id,
                        FileNode.subtype == CHAT_SUBTYPE,
                        FileNode.trashed_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )

    async def drive_for_org(self) -> FileDrive | None:
        self._require_open()
        stmt = select(FileDrive).where(self._scoped(FileDrive), FileDrive.kind == "org")
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def live_children_named(self, parent_id: NodeId, names: Sequence[bytes]) -> int:
        """How many of ``names`` are live children of ``parent_id``.

        A count rather than the rows: the caller that asks this on every
        request — "is the drive's skeleton already whole?" — wants a yes or a
        no from one index probe, and must not take a lock or write to learn it.
        """
        self._require_open()
        stmt = (
            select(func.count())
            .select_from(FileNode)
            .where(
                FileNode.parent_id == parent_id,
                FileNode.trashed_at.is_(None),
                FileNode.name.in_(list(names)),
                self._scoped(FileNode),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def lock_drive(self, id: DriveId) -> FileDrive | None:
        """Read the drive ``FOR NO KEY UPDATE``, after its namespace (shared).

        The quota decision is taken under it: two byte reservations must each
        see the other's hold. It is the org's one row, and a reservation holds
        it until its request ends, so uploads opened together queue on it;
        nothing but that decision takes it (a create or a move takes the
        namespace, :meth:`lock_chain`). Its columns are advanced by raw
        statements, which is why this read populates the instance the session
        holds, exactly as :meth:`lock_node` does.

        No-key strength, not ``FOR UPDATE``: two drive lockers still exclude
        each other, but a transaction that merely inserts a node -- whose
        foreign key takes ``FOR KEY SHARE`` on this row until it commits -- no
        longer queues behind one or holds one up. The holder's tree report
        inserts nodes without taking this lock, and at full strength every
        create, move and upload in the org would still have waited for its
        batch through that key share.
        """
        self._require_open()
        await self.lock_namespace(id)
        stmt = (
            select(FileDrive)
            .where(FileDrive.id == id, self._scoped(FileDrive))
            .execution_options(populate_existing=True)
        )
        locked = await lock_rows(
            self._session, LockRank.FILES_DRIVE, stmt, strength="no_key_update"
        )
        return locked.scalar_one_or_none()

    async def domain(self, id: DomainId) -> DedupDomain | None:
        self._require_open()
        stmt = select(DedupDomain).where(DedupDomain.id == id, self._scoped(DedupDomain))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # ---- versions --------------------------------------------------------

    async def version(self, id: VersionId) -> FileVersion | None:
        self._require_open()
        stmt = select(FileVersion).where(FileVersion.id == id, self._scoped(FileVersion))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def versions_by_id(self, ids: Sequence[VersionId]) -> dict[uuid.UUID, FileVersion]:
        """The named versions, keyed by id, in one statement for the whole set.

        A listing needs every row's head so a client can tell whether it
        already holds the bytes; asking per row would make the page cost a
        query per item, which is exactly what the listing budget forbids.
        """
        self._require_open()
        wanted = {uuid.UUID(str(id)) for id in ids}
        if not wanted:
            return {}
        stmt = select(FileVersion).where(FileVersion.id.in_(wanted), self._scoped(FileVersion))
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.id: row for row in rows}

    async def versions_of(self, node_id: NodeId) -> Sequence[FileVersion]:
        """A node's version chain, newest first."""
        self._require_open()
        stmt = (
            select(FileVersion)
            .where(FileVersion.node_id == node_id, self._scoped(FileVersion))
            .order_by(FileVersion.seq.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def shares_of(self, node_id: NodeId) -> Sequence[FileShare]:
        """Every live grant on one node."""
        self._require_open()
        stmt = select(FileShare).where(
            FileShare.node_id == node_id,
            FileShare.revoked_at.is_(None),
            self._scoped(FileShare),
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def acl(self, id: AclId) -> FileAcl | None:
        """One interned ACL body. It is a cache of the chain, so a caller that
        reads it must already know the node is not ``acl_rewriting``."""
        self._require_open()
        stmt = select(FileAcl).where(FileAcl.id == id, self._scoped(FileAcl))
        return (await self._session.execute(stmt)).scalars().first()

    async def trash_op(self, id: uuid.UUID) -> FileTrashOp | None:
        """One trash operation, org-scoped.

        The sweep and the restore both need the row back after writing it, and
        a ``session.get`` in the caller would reach the table with no org
        predicate at all — the identity map answers before RLS ever sees the id.
        """
        self._require_open()
        stmt = select(FileTrashOp).where(FileTrashOp.id == id, self._scoped(FileTrashOp))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # ---- writes ----------------------------------------------------------

    async def add(self, obj: Base) -> None:
        """Stage one row. RLS's ``WITH CHECK`` refuses a foreign ``org_team_id``."""
        self._require_open()
        self._session.add(obj)

    async def flush(self) -> None:
        self._require_open()
        await self._session.flush()

    async def insert_history(
        self,
        *,
        node_id: NodeId,
        kind: str,
        acting_principal: uuid.UUID,
        delegating_user: uuid.UUID | None,
        agent_session_id: uuid.UUID | None,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        op_id: OperationId | None,
    ) -> FileHistory:
        """Append one ``file_history`` row, ``seq`` computed inside the INSERT.

        The dense per-node sequence is ``max(seq) + 1`` as a scalar subquery
        rather than a value Python read first, so no read-then-write window
        exists here; the caller owns the savepoint that turns the losing side of
        a race on ``uq_file_history_node_seq`` into a retry.
        """
        self._require_open()
        next_seq = (
            select(func.coalesce(func.max(FileHistory.seq), 0) + 1)
            .where(FileHistory.node_id == node_id)
            .scalar_subquery()
        )
        stmt = (
            insert(FileHistory)
            .values(
                id=uuid.uuid4(),
                org_team_id=self._scope.org_team_id,
                node_id=node_id,
                seq=next_seq,
                kind=kind,
                acting_principal=acting_principal,
                delegating_user=delegating_user,
                agent_session_id=agent_session_id,
                before=before,
                after=after,
                op_id=op_id,
            )
            .returning(FileHistory)
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def history_page(
        self,
        root: FileNode,
        *,
        subtree: bool,
        before: datetime | None,
        before_id: uuid.UUID | None,
        limit: int,
    ) -> Sequence[FileHistory]:
        """One page of activity for a node, or for everything at or under it.

        Newest first, keyed on ``(at, id)`` so the marker the caller hands back
        resumes exactly where the page stopped. The subtree form goes through
        :meth:`subtree_predicate`, so the scope predicate and the index that
        serves it cannot drift apart.
        """
        self._require_open()
        stmt = select(FileHistory)
        if subtree:
            under = select(FileNode.id).where(self.subtree_predicate(root))
            stmt = stmt.where(FileHistory.node_id.in_(under))
        else:
            stmt = stmt.where(FileHistory.node_id == root.id)
        if before is not None and before_id is not None:
            stmt = stmt.where(
                (FileHistory.at < before)
                | ((FileHistory.at == before) & (FileHistory.id < before_id))
            )
        stmt = stmt.order_by(FileHistory.at.desc(), FileHistory.id.desc()).limit(limit)
        return list((await self.execute_scoped(stmt)).scalars().all())

    async def add_stats_delta(
        self,
        *,
        node_id: NodeId,
        bytes_delta: int,
        files_delta: int,
        direct_children_delta: int,
        child_change_at: datetime | None,
    ) -> None:
        """Append one ``file_dir_stats_deltas`` increment for a folder.

        An INSERT and never an UPDATE of the folder's own row: that is what
        keeps a thousand concurrent writers under one folder off each other's
        row lock.
        """
        self._require_open()
        await self._session.execute(
            insert(FileDirStatsDelta).values(
                id=uuid.uuid4(),
                org_team_id=self._scope.org_team_id,
                node_id=node_id,
                bytes_delta=bytes_delta,
                files_delta=files_delta,
                direct_children_delta=direct_children_delta,
                child_change_at=child_change_at,
            )
        )

    async def claim_stats_deltas(self, *, limit: int) -> Sequence[uuid.UUID]:
        """Lock up to ``limit`` of this org's pending deltas, oldest first.

        ``skip_locked`` so two folders' passes never queue behind one another.
        """
        self._require_open()
        stmt = (
            select(FileDirStatsDelta.id)
            .where(self._scoped(FileDirStatsDelta))
            .order_by(FileDirStatsDelta.at, FileDirStatsDelta.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def drop_stats_deltas(self, ids: Sequence[uuid.UUID]) -> None:
        """Delete the deltas a fold has just accounted for."""
        self._require_open()
        if not ids:
            return
        await self._session.execute(
            delete(FileDirStatsDelta).where(FileDirStatsDelta.id.in_(list(ids)))
        )

    async def folder_last_child_change(self, node: FileNode) -> datetime | None:
        """The aggregated time a folder's children last changed, if it is known."""
        self._require_open()
        stmt = select(FileDirStats.last_child_change_at).where(
            FileDirStats.node_id == node.id, self._scoped(FileDirStats)
        )
        found: datetime | None = (await self._session.execute(stmt)).scalar_one_or_none()
        return found

    # ---- statement builders ----------------------------------------------

    def select_nodes(self) -> Select[tuple[FileNode]]:
        """A node SELECT for a caller that adds its own clauses.

        The hygiene rule is that a Files model is named in a ``select()`` only
        here, while a caller like the listing has to build its own WHERE, ORDER
        BY and LIMIT — the two are reconciled by handing out the statement. The
        org predicate is already on it, so the statement is safe even if the
        caller runs it on the session rather than through
        :meth:`execute_scoped` (which adds the same predicate again, harmlessly).
        """
        return select(FileNode).where(self._scoped(FileNode))

    async def set_star_row(self, *, node_id: NodeId, user_id: uuid.UUID, wanted: bool) -> bool:
        """Add or drop one user's bookmark row; ``True`` when it actually moved.

        ``RETURNING`` rather than ``rowcount``: a conflicting insert returns no
        row, which is exactly "this caller already had it" and is the one answer
        every driver spells the same way.
        """
        self._require_open()
        org_team_id = self._scope.org_team_id
        if wanted:
            added = await self._session.execute(
                pg_insert(FileStar)
                .values(org_team_id=org_team_id, user_id=user_id, node_id=node_id)
                .on_conflict_do_nothing()
                .returning(FileStar.node_id)
            )
            return added.first() is not None
        removed = await self._session.execute(
            delete(FileStar)
            .where(
                FileStar.org_team_id == org_team_id,
                FileStar.user_id == user_id,
                FileStar.node_id == node_id,
            )
            .returning(FileStar.node_id)
        )
        return removed.first() is not None

    async def intern_acl_row(self, *, acl_id: AclId, body: Any, body_hash: str) -> bool:
        """Insert one interned ACL body; ``False`` when another interner won.

        The empty ``RETURNING`` on the conflict path is how a concurrent second
        interner learns not to duplicate the chain's rows.
        """
        self._require_open()
        created = (
            await self._session.execute(
                pg_insert(FileAcl)
                .values(
                    id=acl_id,
                    org_team_id=self._scope.org_team_id,
                    body=body,
                    body_hash=body_hash,
                )
                .on_conflict_do_nothing(constraint="uq_file_acls_body_hash")
                .returning(FileAcl.id)
            )
        ).scalar_one_or_none()
        return created is not None

    async def insert_upload_part(
        self, *, session_id: SessionId, part_no: int, size: int, checksum: bytes
    ) -> bool:
        """Record one uploaded part; ``False`` when that part number was already in."""
        self._require_open()
        result = cast_typing(
            "CursorResult[Any]",
            await self._session.execute(
                pg_insert(FileUploadPart)
                .values(
                    session_id=session_id,
                    part_no=part_no,
                    org_team_id=self._scope.org_team_id,
                    size=size,
                    checksum=checksum,
                )
                .on_conflict_do_nothing(index_elements=["session_id", "part_no"])
            ),
        )
        return int(result.rowcount) == 1

    def select_versions(self) -> Select[tuple[FileVersion]]:
        """A version SELECT for a caller that adds its own clauses, org-scoped.

        Reading the head of every node on a page is one statement with an ``IN``
        the caller spells, which is why the statement is handed out rather than
        the rows: the id set is the caller's, the org predicate never is.
        """
        return select(FileVersion).where(self._scoped(FileVersion))

    def select_acls(self) -> Select[tuple[FileAcl]]:
        """An ACL SELECT for a caller that adds its own clauses, org-scoped."""
        return select(FileAcl).where(self._scoped(FileAcl))

    def select_shares(self) -> Select[tuple[FileShare]]:
        """A share SELECT for a caller that adds its own clauses, org-scoped."""
        return select(FileShare).where(self._scoped(FileShare))

    def select_ops(self) -> Select[tuple[FileOp]]:
        """An operation SELECT for a caller that adds its own clauses, org-scoped."""
        return select(FileOp).where(self._scoped(FileOp))

    def select_conflicts(self) -> Select[tuple[FileConflict]]:
        """A conflict SELECT for a caller that adds its own clauses, org-scoped."""
        return select(FileConflict).where(self._scoped(FileConflict))

    @staticmethod
    def select_stores() -> Select[tuple[FileStore]]:
        """A store SELECT for a caller that adds its own clauses.

        ``file_stores`` names a bucket, which belongs to the deployment and not
        to a tenant, so the row carries no ``org_team_id``: there is no org
        predicate to add and no scope to build the statement from, which is why
        this one is static. It is spelled here anyway because the rule is about
        where a Files table may be named at all, not only about scoping.
        """
        return select(FileStore)

    def update_nodes(self) -> Update:
        """A node UPDATE for a caller that adds its own WHERE and VALUES.

        The write half of :meth:`select_nodes`, and for the same reason: the
        ACL rewrite's compare-and-swap has to spell its own predicate, but the
        org predicate must not be one the caller can forget. It is already on
        the statement, so a caller that runs it straight on the session still
        cannot cross a tenant.
        """
        return update(FileNode).where(self._scoped(FileNode))

    def update_shares(self) -> Update:
        """A share UPDATE for a caller that adds its own WHERE and VALUES, org-scoped."""
        return update(FileShare).where(self._scoped(FileShare))

    def update_ops(self) -> Update:
        """An operation UPDATE for a caller that adds its own WHERE and VALUES, org-scoped."""
        return update(FileOp).where(self._scoped(FileOp))

    def update_conflicts(self) -> Update:
        """A conflict UPDATE for a caller that adds its own WHERE and VALUES, org-scoped."""
        return update(FileConflict).where(self._scoped(FileConflict))

    # ---- escape hatch ----------------------------------------------------

    async def execute_scoped(self, stmt: Select[Any]) -> Any:
        """Run a hand-written SELECT with the org predicate appended.

        The statement must read exactly one Files tenant table, so there is one
        unambiguous ``org_team_id`` to constrain; anything else raises rather
        than running unscoped.
        """
        self._require_open()
        tenant = _tenant_tables()
        froms = {table.name for table in stmt.get_final_froms() if hasattr(table, "name")}
        named = froms & tenant.keys()
        if len(named) != 1 or named != froms:
            raise ScopeError(
                "execute_scoped needs exactly one Files tenant table, got "
                f"{sorted(froms) or ['none']}"
            )
        model = tenant[named.pop()]
        return await self._session.execute(stmt.where(self._scoped(model)))


async def named_folder_paths(
    session: AsyncSession,
    *,
    org_team_id: uuid.UUID,
    root_node_id: uuid.UUID,
    container_name: bytes,
    names: Iterable[bytes],
) -> dict[bytes, str]:
    """``path_ids`` of ``/<container>/<name>`` for each live ``name``, in one read.

    Runs as the application on a plain session — it is how the org-level
    storage limits find a team's folder before a write's transaction opens —
    which is why it is a function here rather than a method needing the
    Files transaction.
    """
    wanted = list(names)
    if not wanted:
        return {}
    container = aliased(FileNode)
    stmt = (
        select(FileNode.name, FileNode.path_ids)
        .join(container, container.id == FileNode.parent_id)
        .where(
            container.parent_id == root_node_id,
            container.name == container_name,
            container.trashed_at.is_(None),
            container.org_team_id == org_team_id,
            FileNode.name.in_(wanted),
            FileNode.trashed_at.is_(None),
            FileNode.org_team_id == org_team_id,
        )
    )
    return {name: path for name, path in (await session.execute(stmt)).all()}


__all__ = [
    "APP_ROLE",
    "ORG_SETTING",
    "SUBTREE_DEPTH_BIND",
    "SUBTREE_INDEX_DEPTH",
    "FilesRepo",
    "NodeRow",
    "RepoUsageError",
    "ScopeError",
    "ancestor_chain_by_ino",
    "ancestor_chain_predicate",
    "named_folder_paths",
    "subtree_sql",
]
