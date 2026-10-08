"""The live plane's writers never deadlock on the lease row, in any interleaving.

Two kinds of writer meet on one lease. The holder's live batch (``upsert``)
fences on the ``file_leases`` row and then writes ``file_lease_live_entries``;
every write that lands bytes under the lease clears the entries it settles and
bumps the same lease row. On the box this is a heartbeat-cadence batch racing
the uploads landing under it, and the dev stack showed what an inversion costs:
one side deleted the child and then waited for the parent, the other held the
parent and waited for the child, the detector killed one, and every heartbeat
queued behind the wreck until its lock timeout.

Each test parks one session on its own connection *while it holds its first
lock*, starts the other, waits until Postgres itself reports the second one
queued (an ungranted lock in ``pg_locks``, never a sleep), releases the first
and asserts both complete — no ``40P01``, no raw driver error. The interleaving
is the one that deadlocked before the lease row was locked parent-first and at
update strength; each test failed on that code.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, NodeId, OrgScope
from alkera_core.files.lease_live import LiveEntriesService, LiveReport
from alkera_core.files.lease_tree import LeaseTreeService, TreeChange
from alkera_core.files.leases import Lease, LeaseService
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.files.uploads import UploadService
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files._kit.fencing import held_by
from tests.files.concurrency._gate import (
    StatementGate,
    _backend_pid,
    _mentions_deadlock,
    _wait_blocked,
)

pytestmark = pytest.mark.asyncio

INSTANCE = "box-1"
MACHINE = "box-mbp"

#: Where a clear first holds something another writer wants. On the fixed code
#: that is the lease row, taken ``FOR NO KEY UPDATE`` before any entry goes; on
#: the code that deadlocked it was the entry row, held by the ``DELETE`` that
#: came first. Parking on either is what lets the same test fail there and pass
#: here, rather than only describe the fixed order.
CLEAR_HOLDS_ITS_FIRST_LOCK = re.compile(
    r"DELETE FROM file_lease_live_entries|FROM file_leases\b.*FOR NO KEY UPDATE", re.S
)


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


class Plane:
    """A leased folder with one file reported in flight, as plain ids."""

    def __init__(self, org: FilesOrg, lease_node_id: uuid.UUID, file_id: uuid.UUID, epoch: int):
        self.org = org
        self.lease = NodeId(lease_node_id)
        self.file = NodeId(file_id)
        self.epoch = epoch

    @property
    def scope(self) -> OrgScope:
        return OrgScope(org_team_id=self.org.org_team_id)


@pytest.fixture
async def plane(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> Plane:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("team/ team/notes.md", drive=drive)
    ctx = _ctx(files_org)
    async with repo.transaction():
        grant = await LeaseService(repo, ctx, clock).acquire(
            NodeId(nodes["team"].id), instance_id=INSTANCE, machine_id=MACHINE
        )
    async with repo.transaction():
        await LiveEntriesService(repo, ctx).upsert(
            NodeId(nodes["team"].id),
            epoch=grant.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=NodeId(nodes["team/notes.md"].id), state="writing")],
        )
    return Plane(files_org, nodes["team"].id, nodes["team/notes.md"].id, grant.epoch)


async def _upsert(session: Any, plane: Plane, *, state: str) -> int:
    repo = FilesRepo(session, plane.scope)
    async with repo.transaction():
        return await LiveEntriesService(repo, _ctx(plane.org)).upsert(
            plane.lease,
            epoch=plane.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=plane.file, state=state)],
        )


async def _clear(session: Any, plane: Plane) -> int:
    repo = FilesRepo(session, plane.scope)
    async with repo.transaction():
        return await LiveEntriesService(repo, _ctx(plane.org)).clear([plane.file], reason="saved")


async def _heartbeat(session: Any, plane: Plane, clock: FakeClock) -> Lease:
    repo = FilesRepo(session, plane.scope)
    async with repo.transaction():
        return await LeaseService(repo, _ctx(plane.org), clock).heartbeat(
            plane.lease, epoch=plane.epoch, instance_id=INSTANCE
        )


async def _live_seq(session: Any, plane: Plane) -> int:
    return int(
        (
            await session.execute(
                text("SELECT live_seq FROM file_leases WHERE node_id = :node"),
                {"node": uuid.UUID(str(plane.lease))},
            )
        ).scalar_one()
    )


async def _interleave(
    sessions: Callable[..., Awaitable[list[Any]]],
    *,
    first: Callable[[Any], Awaitable[Any]],
    second: Callable[[Any], Awaitable[Any]],
    park_first_on: re.Pattern[str] | None = None,
) -> tuple[Any, Any, bool]:
    """Park ``first`` holding its first lock, start ``second``, report whether
    Postgres saw it queue, release, and return both outcomes.

    An exception is returned rather than raised so the caller can say which
    failure it is: the deadlock detector firing is the regression this module
    exists to catch, and it must be named as such rather than surface as a
    driver error inside ``gather``.
    """
    first_session, second_session, observer = await sessions(3)
    gate = StatementGate(first_session, park_on=park_first_on)
    second_pid = await _backend_pid(second_session)
    started: list[asyncio.Task[Any]] = []
    try:
        gate.arm()
        first_task = asyncio.ensure_future(first(first_session))
        started.append(first_task)
        parked = asyncio.ensure_future(gate.parked.wait())
        await asyncio.wait({first_task, parked}, return_when=asyncio.FIRST_COMPLETED)
        assert gate.parked.is_set(), "the first writer never took the lock it was to hold"
        second_task = asyncio.ensure_future(second(second_session))
        started.append(second_task)
        queued = await _wait_blocked(observer, second_pid, second_task)
        gate.release()
        parked.cancel()
        outcomes = await asyncio.gather(first_task, second_task, return_exceptions=True)
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
    return outcomes[0], outcomes[1], queued


def _completed(outcome: Any, what: str) -> Any:
    if isinstance(outcome, BaseException):
        assert not _mentions_deadlock(outcome), f"{what}: the deadlock detector fired: {outcome!r}"
        raise AssertionError(f"{what} failed: {outcome!r}") from outcome
    return outcome


async def test_a_clear_holding_its_first_lock_never_deadlocks_the_holders_batch(
    plane: Plane, sessions: Callable[..., Awaitable[list[Any]]]
) -> None:
    """The report's cycle: bytes land under the lease while the holder's batch
    settles the same node.

    The clear is parked holding its first lock; the batch is started and queues
    behind it; the clear finishes and the batch finishes. Before the clear took
    the lease row first, this was the batch holding the lease and waiting for
    the entry the clear had deleted, the clear waiting for the lease to bump it
    — and ``40P01`` on one of them.
    """
    clear_outcome, batch_outcome, queued = await _interleave(
        sessions,
        first=lambda s: _clear(s, plane),
        second=lambda s: _upsert(s, plane, state="applied"),
        park_first_on=CLEAR_HOLDS_ITS_FIRST_LOCK,
    )

    assert queued, "the holder's batch never queued behind the clear, so nothing raced"
    assert _completed(clear_outcome, "the clear") == 1
    seq = _completed(batch_outcome, "the holder's batch")
    assert isinstance(seq, int)


async def test_two_live_batches_from_one_holder_queue_rather_than_deadlock(
    plane: Plane, sessions: Callable[..., Awaitable[list[Any]]]
) -> None:
    """A holder whose previous batch is still in flight sends the next one.

    Both batches fence on the lease row and both then update it. Fenced at
    share strength, the second batch's fence passed beside the first's and its
    stamp then waited for the first's share to go — which it never could, the
    first now waiting on the second's — so every overlapping pair of batches
    was a deadlock. Fenced at update strength the second queues at its fence,
    and the sequence the two hand back tells them apart.
    """
    first_outcome, second_outcome, queued = await _interleave(
        sessions,
        first=lambda s: _upsert(s, plane, state="uploading"),
        second=lambda s: _upsert(s, plane, state="on_box"),
    )

    assert queued, "the second batch never queued behind the first, so nothing raced"
    first_seq = _completed(first_outcome, "the first batch")
    second_seq = _completed(second_outcome, "the second batch")
    assert second_seq == first_seq + 1


async def test_a_heartbeat_queues_behind_a_clear_and_then_lands(
    plane: Plane,
    clock: FakeClock,
    sessions: Callable[..., Awaitable[list[Any]]],
) -> None:
    """The beat is one update of the lease row, so the clear parent-first is
    what it queues behind — and what it could not queue behind while the clear
    held only the entry, which is the observable difference between the two
    orders. It then lands: the lease is extended, not fenced, and the clear
    counted its row.
    """
    clear_outcome, beat_outcome, queued = await _interleave(
        sessions,
        first=lambda s: _clear(s, plane),
        second=lambda s: _heartbeat(s, plane, clock),
        park_first_on=CLEAR_HOLDS_ITS_FIRST_LOCK,
    )

    assert queued, "the beat never queued behind the clear's lease lock"
    assert _completed(clear_outcome, "the clear") == 1
    beat = _completed(beat_outcome, "the heartbeat")
    assert isinstance(beat, Lease)
    assert beat.epoch == plane.epoch


async def test_the_clear_bumped_the_sequence_the_queued_batch_then_read(
    plane: Plane, repo: FilesRepo, sessions: Callable[..., Awaitable[list[Any]]]
) -> None:
    """Parent-first changes the order, not the answer: the clear still bumps
    once per lease and the batch that queued behind it bumps once more, so the
    row ends two ahead of where the fixture left it."""
    async with repo.transaction():
        before = await _live_seq(repo.session, plane)

    clear_outcome, batch_outcome, _ = await _interleave(
        sessions,
        first=lambda s: _clear(s, plane),
        second=lambda s: _upsert(s, plane, state="writing"),
        park_first_on=CLEAR_HOLDS_ITS_FIRST_LOCK,
    )

    _completed(clear_outcome, "the clear")
    assert _completed(batch_outcome, "the batch") == before + 2
    async with repo.transaction():
        assert await _live_seq(repo.session, plane) == before + 2


class Mount:
    """A leased folder with a file to move and a folder to move it into."""

    def __init__(self, org: FilesOrg, nodes: dict[str, Any], epoch: int, root: Path) -> None:
        self.org = org
        self.drive = DriveId(nodes["team"].drive_id)
        self.lease = NodeId(nodes["team"].id)
        self.file = NodeId(nodes["team/a.txt"].id)
        self.file_etag = int(nodes["team/a.txt"].etag)
        self.dst = NodeId(nodes["team/dst"].id)
        self.domain_id = nodes["team"].drive_id
        self.epoch = epoch
        self.root = root

    @property
    def scope(self) -> OrgScope:
        return OrgScope(org_team_id=self.org.org_team_id)


@pytest.fixture
async def mount(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    tmp_path: Path,
) -> Mount:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("team/ team/a.txt team/dst/", drive=drive)
    async with repo.transaction():
        grant = await LeaseService(repo, _ctx(files_org), clock).acquire(
            NodeId(nodes["team"].id), instance_id=INSTANCE, machine_id=MACHINE
        )
    mount = Mount(files_org, nodes, grant.epoch, tmp_path)
    mount.domain_id = drive.dedup_domain_id
    return mount


def _store(mount: Mount, clock: FakeClock) -> _RootedDomainStore:
    return _RootedDomainStore(
        FilesystemStore(mount.root / "domains" / str(mount.domain_id), clock=clock.now),
        mount.domain_id,
    )


async def _move_under_lease(session: Any, mount: Mount, clock: FakeClock) -> Any:
    repo = FilesRepo(session, mount.scope)
    ctx = _ctx(mount.org)
    async with repo.transaction():
        return await Namespace(repo, ctx, clock, _store(mount, clock)).move(
            mount.file,
            mount.dst,
            if_match=mount.file_etag,
            lease=held_by(ctx, mount.epoch, INSTANCE),
        )


async def _open_under_lease(session: Any, mount: Mount, clock: FakeClock) -> Any:
    repo = FilesRepo(session, mount.scope)
    ctx = _ctx(mount.org)
    return await UploadService(repo, ctx, clock, _store(mount, clock)).open(
        mount.drive,
        mount.lease,
        b"b.txt",
        declared_size=3,
        lease=held_by(ctx, mount.epoch, INSTANCE),
    )


async def test_a_move_under_a_lease_and_an_upload_opening_under_it_never_deadlock(
    mount: Mount, clock: FakeClock, sessions: Callable[..., Awaitable[list[Any]]]
) -> None:
    """The lease row sits after the drive in the fixed order, for every writer.

    An upload opening under the lease holds the drive and then asks for the
    lease; a move of a node under the lease used to fence first and take the
    drive second. At share strength the two lease requests were compatible and
    the open never waited on the move; at update strength they exclude each
    other, so the move holding the lease and waiting for the drive met the
    open holding the drive and waiting for the lease. The move is parked on
    its first lock — the drive, now — and the open queues behind it.
    """
    move_outcome, open_outcome, queued = await _interleave(
        sessions,
        first=lambda s: _move_under_lease(s, mount, clock),
        second=lambda s: _open_under_lease(s, mount, clock),
    )

    assert queued, "the upload open never queued behind the move, so nothing raced"
    _completed(move_outcome, "the move")
    _completed(open_outcome, "the upload open")


# ---------------------------------------------------------------------------
# The holder's tree report against the other writers of a leased folder
# ---------------------------------------------------------------------------

#: The tree report's fence: the lease row at update strength. Parking there
#: holds everything the report takes before it writes a row -- the drive, then
#: the lease -- which is the state a writer that wants either one meets.
TREE_HOLDS_ITS_FENCE = re.compile(r"FROM file_leases\b.*FOR NO KEY UPDATE", re.S)


class Held:
    """Two sibling folders on one drive, each leased live by its own box."""

    def __init__(self, org: FilesOrg, drive: Any, nodes: dict[str, Any], epochs: dict[str, int]):
        self.org = org
        self.drive = DriveId(drive.id)
        self.domain_id = drive.dedup_domain_id
        self.leases = {name: NodeId(nodes[name].id) for name in ("a", "b")}
        self.epochs = epochs

    @property
    def scope(self) -> OrgScope:
        return OrgScope(org_team_id=self.org.org_team_id)


@pytest.fixture
async def held(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> Held:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("a/ b/", drive=drive)
    epochs: dict[str, int] = {}
    for name in ("a", "b"):
        async with repo.transaction():
            grant = await LeaseService(repo, _ctx(files_org), clock).acquire(
                NodeId(nodes[name].id), instance_id=f"box-{name}", machine_id=MACHINE
            )
        epochs[name] = grant.epoch
        async with repo.transaction():
            await repo.session.execute(
                text("UPDATE file_leases SET live_cadence = CAST(:c AS jsonb) WHERE node_id = :n"),
                {"c": '{"metadataEveryMs": 300}', "n": nodes[name].id},
            )
    return Held(files_org, drive, nodes, epochs)


async def _tree(session: Any, held: Held, name: str, clock: FakeClock, files: int) -> Any:
    repo = FilesRepo(session, held.scope)
    async with repo.transaction():
        return await LeaseTreeService(repo, _ctx(held.org), clock).apply(
            held.leases[name],
            drive_id=held.drive,
            epoch=held.epochs[name],
            instance_id=f"box-{name}",
            batch_id=uuid.uuid4(),
            changes=[
                TreeChange(
                    op="upsert", path=f"src/f{index}.py".encode(), kind="file", size=1, mtime_ns=1
                )
                for index in range(files)
            ],
        )


async def _open_under(session: Any, held: Held, name: str, clock: FakeClock, root: Path) -> Any:
    repo = FilesRepo(session, held.scope)
    ctx = _ctx(held.org)
    store = _RootedDomainStore(
        FilesystemStore(root / "domains" / str(held.domain_id), clock=clock.now), held.domain_id
    )
    return await UploadService(repo, ctx, clock, store).open(
        held.drive,
        held.leases[name],
        b"upload.txt",
        declared_size=3,
        lease=held_by(ctx, held.epochs[name], f"box-{name}"),
    )


async def test_a_tree_report_holding_its_fence_and_an_upload_opening_under_it_never_deadlock(
    held: Held, clock: FakeClock, tmp_path: Path, sessions: Callable[..., Awaitable[list[Any]]]
) -> None:
    """The report takes the drive and then the lease, as the upload open does.

    Parked holding its fence, the report holds both; the open queues at the
    drive and lands after it. Fenced before the drive -- the order the brief
    first named -- the report would hold the lease and wait for the drive the
    open holds while the open waits for the lease: the detector's cycle.
    """
    tree_outcome, open_outcome, queued = await _interleave(
        sessions,
        first=lambda s: _tree(s, held, "a", clock, files=3),
        second=lambda s: _open_under(s, held, "a", clock, tmp_path),
        park_first_on=TREE_HOLDS_ITS_FENCE,
    )

    assert queued, "the upload open never queued behind the report, so nothing raced"
    assert _completed(tree_outcome, "the tree report").applied == 3
    _completed(open_outcome, "the upload open")


async def test_two_boxes_reporting_sibling_folders_at_once_queue_and_both_land(
    held: Held, clock: FakeClock, repo: FilesRepo, sessions: Callable[..., Awaitable[list[Any]]]
) -> None:
    """Two leases, one drive, both minting at once: each report reserves its
    inodes on another connection before its first lock, so the second report's
    reservation waits for the first report's drive row rather than for its own
    batch, and both land every row with no inode handed out twice."""
    first, second, queued = await _interleave(
        sessions,
        first=lambda s: _tree(s, held, "a", clock, files=50),
        second=lambda s: _tree(s, held, "b", clock, files=50),
        park_first_on=TREE_HOLDS_ITS_FENCE,
    )

    # The second report's own connection never queued: its reservation did,
    # on a connection of its own, and _interleave only watches the first.
    assert not queued, "the second report's batch queued on its own connection"
    assert _completed(first, "the first report").applied == 50
    assert _completed(second, "the second report").applied == 50
    async with repo.transaction():
        inos = (
            (
                await repo.session.execute(
                    text("SELECT ino FROM file_nodes WHERE drive_id = :d"), {"d": held.drive}
                )
            )
            .scalars()
            .all()
        )
    assert len(inos) == len(set(inos))
