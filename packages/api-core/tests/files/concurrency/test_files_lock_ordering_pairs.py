"""Every pair of mutating Files operations, driven in both orders, never deadlocks.

**The fixed lock order is drive → parent → node → version.** Every mutating
statement in Files takes the rows it will write in exactly that order —
``FilesRepo.lock_chain`` is the one place that spells it — because two
transactions that take the same two rows in opposite orders are the textbook
Postgres deadlock: each holds what the other is waiting for and the detector
kills one with ``40P01``.

So this module is the proof, not the statement of intent. For every pair of
mutating operations (create, rename, move, trash, restore, put_version, grant,
acquire lease) it runs both orders on two real sessions, each on its own
connection against the real database:

1. the first operation is **parked after it has taken its first row lock** — a
   statement gate on that session's own connection, not a sleep — so it is
   holding a lock while the second one starts;
2. the second operation is started, and the test waits until Postgres itself
   reports it **blocked** on a lock (an ungranted row in ``pg_locks`` for its
   backend), which is what "the second waits" means observably;
3. the first is released, and both are awaited.

The assertions are then: the second one completes, or fails with a documented
Files error (409/412/404) — never a raw driver error; and the Postgres
**deadlock detector never fires**: no ``40P01`` anywhere in the run, which is a
test failure, never a retry. Each session's own row-lock statements are also
checked to be non-decreasing in the documented rank, so an operation that took
a node lock before its drive lock fails here even on a run that happened not to
deadlock.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import acl, conflict_resolution
from alkera_core.files.clock import SystemClock
from alkera_core.files.content import ContentService
from alkera_core.files.errors import FilesError
from alkera_core.files.ids import DomainId, DriveId, NodeId, OrgScope, TrashOpId, VersionId
from alkera_core.files.lease_live import LiveEntriesService, LiveReport
from alkera_core.files.lease_tree import LeaseTreeService, TreeChange
from alkera_core.files.leases import LeaseService
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.files.trash import Trash
from alkera_core.models.files.history import FileConflict
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.util import await_only
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files._kit.fencing import held_by
from tests.files.concurrency._gate import (
    BLOCKED_TIMEOUT,
    LOCK_RANK,
    StatementGate,
    _backend_pid,
    _mentions_deadlock,
    _wait_blocked,
)

pytestmark = pytest.mark.asyncio

#: Pairs whose second operation is known to come back as a raw driver error
#: rather than a documented Files conflict, or to fire the deadlock detector.
#: Recorded as strict xfails so the day one is fixed this module turns it back
#: into a pass instead of quietly agreeing with it. Empty on purpose: the two
#: gaps this matrix first surfaced — ``acl.grant`` locking the node without the
#: drive, and two concurrent ``put_version``s racing the version sequence — are
#: fixed, and both pairs are plain passes below.
KNOWN_GAPS: dict[frozenset[str], str] = {}


def _ctx(org: FilesOrg, user_id: uuid.UUID) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(user_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


@dataclass
class Rig:
    """One drive holding everything the eight operations need."""

    org: FilesOrg
    drive_id: DriveId
    folder: FileNode
    other_folder: FileNode
    target: FileNode
    trashed_op: TrashOpId
    conflict_id: uuid.UUID
    domain_id: DomainId
    root: Path
    counter: Any = field(default_factory=lambda: itertools.count(1))

    def unique(self, stem: str) -> bytes:
        return f"{stem}-{next(self.counter)}-{uuid.uuid4().hex[:6]}".encode()


async def _stream(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


@dataclass(frozen=True, slots=True)
class Services:
    """One session's view of the library."""

    namespace: Namespace
    trash: Trash
    content: ContentService
    leases: LeaseService
    ctx: ActingContext


async def _etag(repo: FilesRepo, node_id: NodeId) -> int:
    node = await repo.node(node_id)
    assert node is not None
    return int(node.etag)


async def _op_create(repo: FilesRepo, rig: Rig, services: Services) -> object:
    async with repo.transaction():
        return await services.namespace.create(
            rig.drive_id, NodeId(rig.folder.id), "file", rig.unique("made")
        )


async def _op_rename(repo: FilesRepo, rig: Rig, services: Services) -> object:
    node_id = NodeId(rig.target.id)
    async with repo.transaction():
        return await services.namespace.rename(
            node_id, rig.unique("renamed"), if_match=await _etag(repo, node_id)
        )


async def _op_move(repo: FilesRepo, rig: Rig, services: Services) -> object:
    node_id = NodeId(rig.target.id)
    async with repo.transaction():
        return await services.namespace.move(
            node_id, NodeId(rig.other_folder.id), if_match=await _etag(repo, node_id)
        )


async def _op_trash(repo: FilesRepo, rig: Rig, services: Services) -> object:
    node_id = NodeId(rig.target.id)
    async with repo.transaction():
        return await services.trash.trash(node_id, if_match=await _etag(repo, node_id))


async def _op_restore(repo: FilesRepo, rig: Rig, services: Services) -> object:
    async with repo.transaction():
        return await services.trash.restore(rig.trashed_op)


async def _op_put_version(repo: FilesRepo, rig: Rig, services: Services) -> object:
    """The one operation that owns its own transactions, so none is opened here."""
    node_id = NodeId(rig.target.id)
    payload = rig.unique("bytes")
    async with repo.transaction():
        etag = await _etag(repo, node_id)
    return await services.content.put_version(
        node_id, _stream(payload), size_declared=len(payload), if_match=etag
    )


async def _op_restore_version(repo: FilesRepo, rig: Rig, services: Services) -> object:
    """Make the target's oldest version its head again.

    The rig's target already carries the two divergent versions the conflict is
    built from, so this restores one that is there rather than writing a new one
    first — a prelude would take its own locks and the gate would park on those.
    """
    node_id = NodeId(rig.target.id)
    async with repo.transaction():
        rows = await repo.versions_of(node_id)
        oldest = min(rows, key=lambda row: row.seq)
        return await services.content.restore_version(
            node_id, VersionId(oldest.id), if_match=await _etag(repo, node_id)
        )


async def _op_grant(repo: FilesRepo, rig: Rig, services: Services) -> object:
    principal = Principal(
        kind=PrincipalKind.USER,
        id=str(rig.org.member_id),
        org_id=rig.org.org_team_id,
        credential=CredentialKind.JWT,
    )
    async with repo.transaction():
        node = await repo.node(NodeId(rig.target.id))
        assert node is not None
        return await acl.grant(repo, services.ctx, node, principal, "reader")


async def _op_lease(repo: FilesRepo, rig: Rig, services: Services) -> object:
    async with repo.transaction():
        return await services.leases.acquire(
            NodeId(rig.folder.id),
            instance_id=f"i-{uuid.uuid4().hex[:8]}",
            machine_id=f"m-{uuid.uuid4().hex[:8]}",
        )


async def _op_resolve(repo: FilesRepo, rig: Rig, services: Services) -> object:
    """``choice="both"`` on purpose: that is the branch that creates a sibling.

    ``Namespace.create`` takes the drive, so a resolve that opened on the node
    alone would ask for drive-after-node while every operation beside it asks
    for drive-before-node.
    """
    async with repo.transaction():
        return await conflict_resolution.resolve_conflict(
            repo,
            services.ctx,
            rig.conflict_id,
            choice="both",
            if_match=None,
            clock=SystemClock(),
        )


@dataclass(frozen=True, slots=True)
class Operation:
    """One mutating operation, runnable on any session against one rig."""

    name: str
    run: Callable[[FilesRepo, Rig, Services], Awaitable[object]]


OPERATIONS: tuple[Operation, ...] = (
    Operation("create", _op_create),
    Operation("rename", _op_rename),
    Operation("move", _op_move),
    Operation("trash", _op_trash),
    Operation("restore", _op_restore),
    Operation("put_version", _op_put_version),
    Operation("restore_version", _op_restore_version),
    Operation("grant", _op_grant),
    Operation("lease", _op_lease),
    Operation("resolve", _op_resolve),
)

PAIRS = tuple(itertools.combinations_with_replacement(OPERATIONS, 2))


@pytest.fixture
def make_rig(
    files_session: AsyncSession,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    tmp_path: Path,
) -> Callable[[], Awaitable[Rig]]:
    """A fresh tenant and drive per order, not the first order's leftovers.

    A new org each time because an org has exactly one ``org`` drive, and each
    order has to start from a tree nothing has renamed, moved or trashed yet.
    """

    async def build() -> Rig:
        org = await files_org_factory()
        return await _build_rig(files_session, org, files_factory, tmp_path)

    return build


async def _build_rig(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    tmp_path: Path,
) -> Rig:
    drive = await files_factory.drive(org=files_org)
    tree = await files_factory.tree("d/ e/ d/a.bin d/gone.bin", drive=drive)
    repo = FilesRepo(files_session, OrgScope(org_team_id=files_org.org_team_id))
    trash = Trash(repo, _ctx(files_org, files_org.admin_id), SystemClock())
    doomed = tree["d/gone.bin"]
    async with repo.transaction():
        op = await trash.trash(NodeId(doomed.id), if_match=int(doomed.etag))
    conflict_id = await _open_conflict(files_session, tree["d/a.bin"])
    return Rig(
        org=files_org,
        drive_id=DriveId(drive.id),
        folder=tree["d"],
        other_folder=tree["e"],
        target=tree["d/a.bin"],
        trashed_op=TrashOpId(op.id),
        conflict_id=conflict_id,
        domain_id=DomainId(drive.dedup_domain_id),
        root=tmp_path,
    )


async def _open_conflict(session: AsyncSession, node: FileNode) -> uuid.UUID:
    """Two divergent versions on ``node`` and the open conflict between them.

    Built with rows rather than through the upload path because what this module
    drives is the resolve, and a real divergence needs two writers that never saw
    each other — which is exactly what the sync layer hands the resolver.
    """
    versions = [
        FileVersion(
            id=uuid.uuid4(),
            org_team_id=node.org_team_id,
            node_id=node.id,
            seq=seq,
            size_bytes=size,
            content_hash=digest * 32,
            source="upload",
            store_key=f"objects/{digest * 32}",
        )
        for seq, size, digest in ((1, 11, "aa"), (2, 22, "bb"))
    ]
    for version in versions:
        session.add(version)
    await session.flush()
    row = FileConflict(
        id=uuid.uuid4(),
        org_team_id=node.org_team_id,
        node_id=node.id,
        base_version_id=None,
        theirs_version_id=versions[0].id,
        mine_version_id=versions[1].id,
        actor=node.created_by if node.created_by is not None else uuid.uuid4(),
        state="open",
    )
    session.add(row)
    await session.commit()
    return row.id


def _services(repo: FilesRepo, rig: Rig) -> Services:
    clock = SystemClock()
    ctx = _ctx(rig.org, rig.org.admin_id)
    store = _RootedDomainStore(
        FilesystemStore(rig.root / "domains" / str(rig.domain_id), clock=clock.now),
        rig.domain_id,
    )
    return Services(
        namespace=Namespace(repo, ctx, clock, store),
        trash=Trash(repo, ctx, clock, store),
        content=ContentService(repo, ctx, clock, store),
        leases=LeaseService(repo, ctx, clock, store),
        ctx=ctx,
    )


def _outcome(result: object) -> str:
    """A documented Files outcome, or a failure — a driver error is never one."""
    if isinstance(result, BaseException):
        assert not _mentions_deadlock(result), f"the deadlock detector fired: {result!r}"
        assert isinstance(result, FilesError), f"undocumented failure {result!r}"
        return type(result).__name__
    return "ok"


def _param(a: Operation, b: Operation) -> Any:
    gap = KNOWN_GAPS.get(frozenset({a.name, b.name}))
    marks = [pytest.mark.xfail(strict=True, reason=gap)] if gap else []
    return pytest.param(a, b, id=f"{a.name}-then-{b.name}", marks=marks)


@pytest.mark.parametrize(("first", "second"), [_param(a, b) for a, b in PAIRS])
async def test_pair_in_both_orders_never_deadlocks(
    first: Operation,
    second: Operation,
    make_rig: Callable[[], Awaitable[Rig]],
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    """Both orders of one pair: the second waits, then completes or conflicts."""
    for holder, starter in ((first, second), (second, first)):
        rig = await make_rig()
        holder_session, starter_session, observer = await sessions(3)
        scope = OrgScope(org_team_id=rig.org.org_team_id)
        holder_repo = FilesRepo(holder_session, scope)
        starter_repo = FilesRepo(starter_session, scope)
        gate = StatementGate(holder_session)
        starter_gate = StatementGate(starter_session)
        starter_pid = await _backend_pid(starter_session)
        started: list[asyncio.Task[Any]] = []
        try:
            gate.arm()
            holder_task = asyncio.ensure_future(
                holder.run(holder_repo, rig, _services(holder_repo, rig))
            )
            started.append(holder_task)
            parked = asyncio.ensure_future(gate.parked.wait())
            await asyncio.wait({holder_task, parked}, return_when=asyncio.FIRST_COMPLETED)
            starter_task = asyncio.ensure_future(
                starter.run(starter_repo, rig, _services(starter_repo, rig))
            )
            started.append(starter_task)
            assert gate.parked.is_set(), (
                f"{holder.name} never took a row lock, so it could not hold one "
                f"while {starter.name} ran"
            )
            # Whether these two touch the *same row* is a property of the pair,
            # not something a pair test may assume — two `file_nodes` locks on
            # different nodes rightly do not queue, and two `FOR SHARE` lease
            # checks never do. That the queueing is real is proved once, on a
            # pair that must contend, by the test below this one.
            await _wait_blocked(observer, starter_pid, starter_task)
            gate.release()
            parked.cancel()
            results = await asyncio.gather(holder_task, starter_task, return_exceptions=True)
            for session in (holder_session, starter_session):
                try:
                    await session.commit()
                except DBAPIError as exc:  # pragma: no cover — a deadlock lands here
                    assert not _mentions_deadlock(exc), f"the deadlock detector fired: {exc!r}"
                    await session.rollback()
        finally:
            gate.close()
            starter_gate.close()
            for task in started:
                if not task.done():  # pragma: no cover — only after an assertion above
                    task.cancel()

        for result in results:
            _outcome(result)
        for tap, name in ((gate, holder.name), (starter_gate, starter.name)):
            ranks = tap.ranks()
            assert ranks == sorted(ranks), (
                f"{name} took row locks out of the fixed drive → parent → node → "
                f"version order: {tap.tables}"
            )


async def test_second_create_queues_behind_the_first_folder_lock(
    make_rig: Callable[[], Awaitable[Rig]],
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    """The pair matrix's mechanism, proved on a pair that must contend.

    Two creates in one folder both take ``lock_chain``'s ``FOR UPDATE`` on that
    folder's row, so with the first parked holding it the second has to be
    ungranted in ``pg_locks``. If it is not, the folder lock stopped being
    taken and every "no deadlock" result in this module would be vacuous. The
    drive row is not what they queue on: a create does not take it.
    """
    rig = await make_rig()
    first_session, second_session, observer = await sessions(3)
    scope = OrgScope(org_team_id=rig.org.org_team_id)
    first_repo = FilesRepo(first_session, scope)
    second_repo = FilesRepo(second_session, scope)
    gate = StatementGate(first_session)
    second_pid = await _backend_pid(second_session)
    try:
        gate.arm()
        first = asyncio.ensure_future(_op_create(first_repo, rig, _services(first_repo, rig)))
        await asyncio.wait_for(gate.parked.wait(), BLOCKED_TIMEOUT)
        assert gate.tables[0] == "file_nodes", (
            f"create's first row lock was on {gate.tables[0]}, not the folder's row"
        )
        second = asyncio.ensure_future(_op_create(second_repo, rig, _services(second_repo, rig)))
        blocked = await _wait_blocked(observer, second_pid, second)
        gate.release()
        results = await asyncio.gather(first, second, return_exceptions=True)
    finally:
        gate.close()

    assert blocked, "the second create never queued behind the first one's folder lock"
    assert [_outcome(result) for result in results] == ["ok", "ok"]


class InsertGate:
    """Parks one session right after it inserts its upload-session row.

    The reserve phase's out-of-order lock is not a statement a ``FOR UPDATE``
    tap can see: the row-level ``FOR KEY SHARE`` on the drive and the node is
    taken by Postgres for the INSERT's foreign keys. So this gate keys on the
    INSERT itself and holds the session there, with whatever that INSERT locked
    still held, while the other transaction is let at the drive.
    """

    def __init__(self, session: AsyncSession) -> None:
        bind = session.bind
        assert bind is not None
        sync = bind.sync_connection  # type: ignore[union-attr]
        assert sync is not None
        self._sync = sync
        self.parked = asyncio.Event()
        self._release: asyncio.Event = asyncio.Event()
        self._armed = True
        event.listen(self._sync, "after_cursor_execute", self._after)

    def close(self) -> None:
        event.remove(self._sync, "after_cursor_execute", self._after)
        self._release.set()

    def release(self) -> None:
        self._release.set()

    def _after(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if not self._armed or "file_upload_sessions" not in statement.lower():
            return
        if not statement.lstrip().lower().startswith("insert"):
            return
        self._armed = False
        self.parked.set()
        await_only(self._release.wait())


async def test_two_reserve_phases_never_upgrade_the_same_drive_row(
    make_rig: Callable[[], Awaitable[Rig]],
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    """Two ``put_version``s on one drive never deadlock on it.

    Opening the upload session is an INSERT into ``file_upload_sessions`` whose
    foreign keys make Postgres take ``FOR KEY SHARE`` on the drive row, and a
    put opens its session before it takes the drive for the fence and the
    quota hold (every store call happens in between, so the drive is not held
    across them). That is safe only because the drive is then taken at no-key
    strength, which a key share admits: a writer that asked for ``FOR UPDATE``
    instead would wait on the other's share while the other waited on its
    own, and Postgres would cut the cycle with ``40P01``.

    Parking the first put right after its INSERT, holding that share, makes the
    question deterministic: the second put must run to the end meanwhile, drive
    and all. Then the first finishes too, refused on the etag the second moved.
    """
    rig = await make_rig()
    first_session, second_session = await sessions(2)
    scope = OrgScope(org_team_id=rig.org.org_team_id)
    first_repo = FilesRepo(first_session, scope)
    second_repo = FilesRepo(second_session, scope)
    gate = InsertGate(first_session)
    try:
        first = asyncio.ensure_future(_op_put_version(first_repo, rig, _services(first_repo, rig)))
        await asyncio.wait_for(gate.parked.wait(), BLOCKED_TIMEOUT)
        try:
            second: object = await asyncio.wait_for(
                _op_put_version(second_repo, rig, _services(second_repo, rig)), BLOCKED_TIMEOUT
            )
        except Exception as exc:
            second = exc
        assert not first.done(), "the first put moved past its parked INSERT"
        gate.release()
        results = [*(await asyncio.gather(first, return_exceptions=True)), second]
    finally:
        gate.close()

    for result in results:
        assert not isinstance(result, BaseException) or not _mentions_deadlock(result), (
            "the reserve phase asked the drive row for more than its session INSERT's "
            f"key share admits: {result!r}"
        )
    assert not isinstance(second, TimeoutError), (
        "the second put queued behind the first one's session INSERT"
    )
    assert [_outcome(result) for result in results] == ["PreconditionFailed", "ok"]


#: A rename's own write. Not a ``FOR UPDATE``, so the gate is told to park on it
#: by name: what the two-writer test needs is a session holding the row it has
#: just renamed while a second rename starts on the row beside it.
_RENAME_WRITE = re.compile(r"UPDATE\s+file_nodes\s+SET\s+name\s*=", re.IGNORECASE)


async def test_two_renames_in_one_folder_never_deadlock(
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    """Two renames of *different* siblings, one folder, both orders overlapping.

    The pair matrix drives both renames at the same node, so they queue on one
    row and can never deadlock. This is the shape that can: a rename rewrites
    its own row and then re-derives ``macos_safe`` for the whole sibling set,
    so with two writers each holding the row it renamed, each one's recompute
    has to write the row the other holds. ``a.bin`` and ``A.bin`` fold onto one
    ``name_key``, so dissolving that pair makes both recomputes touch both rows
    — the guaranteed collision, not a probable one.

    RED before the folder gate: the first rename comes back with a raw
    ``40P01`` DeadlockDetectedError, which ``_outcome`` refuses.
    """
    org = await files_org_factory()
    drive = await files_factory.drive(org=org)
    tree = await files_factory.tree("d/ d/a.bin d/A.bin", drive=drive)
    lower, upper = tree["d/a.bin"], tree["d/A.bin"]
    scope = OrgScope(org_team_id=org.org_team_id)
    ctx = _ctx(org, org.admin_id)

    first_session, second_session, observer = await sessions(3)
    first_repo = FilesRepo(first_session, scope)
    second_repo = FilesRepo(second_session, scope)

    async def rename(repo: FilesRepo, node: FileNode, name: bytes) -> object:
        namespace = Namespace(repo, ctx, SystemClock())
        async with repo.transaction():
            return await namespace.rename(NodeId(node.id), name, if_match=int(node.etag))

    gate = StatementGate(first_session, park_on=_RENAME_WRITE)
    second_pid = await _backend_pid(second_session)
    try:
        gate.arm()
        first = asyncio.ensure_future(rename(first_repo, lower, b"w1.bin"))
        await asyncio.wait_for(gate.parked.wait(), BLOCKED_TIMEOUT)
        second = asyncio.ensure_future(rename(second_repo, upper, b"w2.bin"))
        blocked = await _wait_blocked(observer, second_pid, second)
        gate.release()
        results = await asyncio.gather(first, second, return_exceptions=True)
        for session in (first_session, second_session):
            try:
                await session.commit()
            except DBAPIError as exc:  # pragma: no cover — a deadlock lands here
                assert not _mentions_deadlock(exc), f"the deadlock detector fired: {exc!r}"
                await session.rollback()
    finally:
        gate.close()

    assert [_outcome(result) for result in results] == ["ok", "ok"]
    assert blocked, "the second rename never queued behind the first one's folder gate"
    # Outside every lease the folder is the whole gate: the drive row belongs
    # to the org's tree reports and uploads, and a rename queued behind them
    # answered 503 instead of renaming.
    ranked = [table for table in gate.tables if table in LOCK_RANK]
    assert ranked[:1] == ["file_nodes"] and "file_drives" not in ranked, (
        f"the rename did not take the folder, and only the folder, before its write: {gate.tables}"
    )


# ---- the holder's batches against the writers they meet ---------------------
#
# The tree report takes no drive row: the leased folder, then the lease row,
# then rows under the folder -- and the drive first, by starting again from a
# savepoint, only when its batch trashes or moves. The live batch takes no
# folder at all: the lease row, through its fence, and then the plane's rows
# under it. Every writer under the lease takes the leased folder before any
# row in it and the lease row after them. These pairs are the proof that the
# orders agree: each runs both ways round, the first parked on its first row
# lock, and neither may meet the deadlock detector. The rank tap is not asked:
# the report that starts again under the drive has, by design, locked the
# folder once before it -- inside a savepoint it rolled back.
#
# The live batch is here because of what it did on the box: it updated the
# lease row twice -- the synced stamp, then the sequence bump -- and the second
# update of a row inside one transaction makes Postgres re-check that row's
# foreign key to ``file_nodes``, ``FOR KEY SHARE`` on the leased folder. With
# the holder's own tree report holding that folder and waiting on the lease
# row, the batch held the row and waited on the folder, and the detector
# answered the box's ``applied`` with ``40P01``. The batch may write the lease
# row once, so the pair below fails the moment a second statement returns.
#
# The batch's own rows take a key share too: inserting an entry takes ``FOR
# KEY SHARE`` on ``file_nodes`` for the node it names, after the lease row,
# and a move holds that node before it asks for the lease row. With the node
# held ``FOR UPDATE`` the key share queued behind the move while the move
# queued behind the batch's fence -- the same ``40P01``, one row down. The
# node locks every writer takes are no-key locks, which a key share does not
# queue behind; the moved-file pair below fails the day they are not.


@dataclass
class LeaseRig:
    org: FilesOrg
    drive_id: DriveId
    tree: dict[str, FileNode]
    epoch: int
    instance: str = "box-1"

    @property
    def lease_node(self) -> NodeId:
        return NodeId(self.tree["d"].id)


LeaseOp = Callable[[FilesRepo, LeaseRig], Awaitable[object]]


def _held(rig: LeaseRig) -> Any:
    return held_by(_ctx(rig.org, rig.org.admin_id), rig.epoch, rig.instance)


async def _lease_etag(repo: FilesRepo, node: FileNode) -> int:
    return await _etag(repo, NodeId(node.id))


def _tree_report(changes: Callable[[], list[Any]]) -> LeaseOp:
    async def run(repo: FilesRepo, rig: LeaseRig) -> object:
        async with repo.transaction():
            return await LeaseTreeService(
                repo, _ctx(rig.org, rig.org.admin_id), SystemClock()
            ).apply(
                rig.lease_node,
                drive_id=rig.drive_id,
                epoch=rig.epoch,
                instance_id=rig.instance,
                batch_id=uuid.uuid4(),
                changes=changes(),
            )

    return run


def _upserts() -> list[Any]:
    return [
        TreeChange(op="upsert", path=f"sub/n{index}.py".encode(), kind="file", size=1, mtime_ns=1)
        for index in range(3)
    ]


def _trash_and_upserts() -> list[Any]:
    return [TreeChange(op="delete", path=b"old"), *_upserts()]


async def _lease_rename_other(repo: FilesRepo, rig: LeaseRig) -> object:
    node = rig.tree["e/a.bin"]
    async with repo.transaction():
        return await Namespace(repo, _ctx(rig.org, rig.org.admin_id), SystemClock()).rename(
            NodeId(node.id), b"renamed.bin", if_match=await _lease_etag(repo, node)
        )


async def _lease_rename_inside(repo: FilesRepo, rig: LeaseRig) -> object:
    node = rig.tree["d/sub/x.bin"]
    async with repo.transaction():
        return await Namespace(repo, _ctx(rig.org, rig.org.admin_id), SystemClock()).rename(
            NodeId(node.id),
            b"renamed.bin",
            if_match=await _lease_etag(repo, node),
            lease=_held(rig),
        )


async def _lease_move_inside(repo: FilesRepo, rig: LeaseRig) -> object:
    node = rig.tree["d/sub/x.bin"]
    async with repo.transaction():
        return await Namespace(repo, _ctx(rig.org, rig.org.admin_id), SystemClock()).move(
            NodeId(node.id),
            NodeId(rig.tree["d"].id),
            if_match=await _lease_etag(repo, node),
            lease=_held(rig),
        )


async def _lease_create_inside(repo: FilesRepo, rig: LeaseRig) -> object:
    async with repo.transaction():
        return await Namespace(repo, _ctx(rig.org, rig.org.admin_id), SystemClock()).create(
            rig.drive_id, NodeId(rig.tree["d/sub"].id), "folder", b"made", lease=_held(rig)
        )


async def _lease_live_batch(repo: FilesRepo, rig: LeaseRig) -> object:
    """The holder's live batch: what its machine is doing to a file it leases.

    The batch names ``d/live.bin``, a file no writer in this matrix locks, so
    the only rows it can meet a partner on are the lease row (its fence) and
    the leased folder -- which it never asks for, unless it updates the lease
    row a second time and the foreign-key re-check asks for it.
    """
    async with repo.transaction():
        return await LiveEntriesService(repo, _ctx(rig.org, rig.org.admin_id)).upsert(
            rig.lease_node,
            epoch=rig.epoch,
            instance_id=rig.instance,
            entries=[LiveReport(node_id=NodeId(rig.tree["d/live.bin"].id), state="writing")],
        )


async def _lease_live_batch_on_the_moved_file(repo: FilesRepo, rig: LeaseRig) -> object:
    """The holder's first report of the very file the move beside it holds.

    Inserting the plane's row takes ``FOR KEY SHARE`` on ``file_nodes`` for
    ``d/sub/x.bin``, after the lease row; the move holds that node before it
    asks for the lease row. The pair lands only because the move's node lock
    is one a key share does not queue behind.
    """
    async with repo.transaction():
        return await LiveEntriesService(repo, _ctx(rig.org, rig.org.admin_id)).upsert(
            rig.lease_node,
            epoch=rig.epoch,
            instance_id=rig.instance,
            entries=[LiveReport(node_id=NodeId(rig.tree["d/sub/x.bin"].id), state="writing")],
        )


LEASE_OPS: dict[str, LeaseOp] = {
    "tree_report": _tree_report(_upserts),
    "trashing_tree_report": _tree_report(_trash_and_upserts),
    "live_batch": _lease_live_batch,
    "live_batch_on_the_moved_file": _lease_live_batch_on_the_moved_file,
    "rename_in_another_folder": _lease_rename_other,
    "move_within_the_leased_folder": _lease_move_inside,
    "create_in_the_leased_folder": _lease_create_inside,
    "rename_in_the_lease": _lease_rename_inside,
}

#: The holder's own batches. Each must land beside every partner: a writer
#: may be refused on the other's etag, a batch may not.
HOLDER_BATCHES = (
    "tree_report",
    "trashing_tree_report",
    "live_batch",
    "live_batch_on_the_moved_file",
)

LEASE_WRITERS = (
    "rename_in_another_folder",
    "move_within_the_leased_folder",
    "create_in_the_leased_folder",
    "rename_in_the_lease",
)


def _lease_pairs() -> list[Any]:
    """Each holder batch against every writer, the live batch and itself."""
    pairs: list[tuple[str, str]] = []
    for report in HOLDER_BATCHES:
        for other in (*LEASE_WRITERS, "live_batch", report):
            if (report, other) not in pairs:
                pairs.append((report, other))
    return [pytest.param(report, other, id=f"{report}-with-{other}") for report, other in pairs]


LEASE_PAIRS = _lease_pairs()


async def _lease_rig(session: AsyncSession, org: FilesOrg, files_factory: FilesFactory) -> LeaseRig:
    drive = await files_factory.drive(org=org)
    tree = await files_factory.tree(
        "d/ d/sub/ d/sub/x.bin d/old/ d/old/y.bin d/live.bin e/ e/a.bin", drive=drive
    )
    repo = FilesRepo(session, OrgScope(org_team_id=org.org_team_id))
    async with repo.transaction():
        grant = await LeaseService(repo, _ctx(org, org.admin_id), SystemClock()).acquire(
            NodeId(tree["d"].id), instance_id="box-1", machine_id="box-mbp"
        )
        await session.execute(
            text("UPDATE file_leases SET live_cadence = CAST(:c AS jsonb) WHERE node_id = :n"),
            {"c": '{"metadataEveryMs": 300}', "n": tree["d"].id},
        )
    return LeaseRig(org=org, drive_id=DriveId(drive.id), tree=tree, epoch=grant.epoch)


@pytest.mark.parametrize(("report", "other"), LEASE_PAIRS)
async def test_tree_report_pair_in_both_orders_never_deadlocks(
    report: str,
    other: str,
    files_session: AsyncSession,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    files_factory: FilesFactory,
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    for first, second in ((report, other), (other, report)):
        org = await files_org_factory()
        rig = await _lease_rig(files_session, org, files_factory)
        first_session, second_session, observer = await sessions(3)
        scope = OrgScope(org_team_id=org.org_team_id)
        first_repo, second_repo = FilesRepo(first_session, scope), FilesRepo(second_session, scope)
        gate = StatementGate(first_session)
        second_pid = await _backend_pid(second_session)
        started: list[asyncio.Task[Any]] = []
        try:
            gate.arm()
            first_task = asyncio.ensure_future(LEASE_OPS[first](first_repo, rig))
            started.append(first_task)
            parked = asyncio.ensure_future(gate.parked.wait())
            await asyncio.wait({first_task, parked}, return_when=asyncio.FIRST_COMPLETED)
            assert gate.parked.is_set(), f"{first} never took a row lock"
            second_task = asyncio.ensure_future(LEASE_OPS[second](second_repo, rig))
            started.append(second_task)
            await _wait_blocked(observer, second_pid, second_task)
            gate.release()
            parked.cancel()
            results = await asyncio.gather(first_task, second_task, return_exceptions=True)
            for session in (first_session, second_session):
                try:
                    await session.commit()
                except DBAPIError as exc:  # pragma: no cover — a deadlock lands here
                    assert not _mentions_deadlock(exc), f"the deadlock detector fired: {exc!r}"
                    await session.rollback()
        finally:
            gate.close()
            for task in started:
                if not task.done():  # pragma: no cover — only after an assertion above
                    task.cancel()
        outcomes = [_outcome(result) for result in results]
        # Two writers of one node may each be refused on the other's etag; that
        # the holder's batches themselves always land is the point of a batch.
        for name, partner, outcome in zip((first, second), (second, first), outcomes, strict=True):
            if name in HOLDER_BATCHES:
                assert outcome == "ok", f"{name} did not land beside {partner}: {results!r}"
