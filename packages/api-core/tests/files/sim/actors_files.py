"""Simulation actors that drive the *real* Files services against real Postgres.

The toy actors in ``actors.py`` proved the scheduler; these drive
``ContentService``, ``Janitor`` and ``Namespace`` themselves, so an interleaving
the scheduler invents is an interleaving the product actually ran.

The scheduler is synchronous and only understands ``sim.step`` / ``sim.sleep``,
while every Files service is a coroutine on an asyncio loop. :class:`Bridge` is
the join: the sim runs on a worker thread, each service call is submitted to the
test's loop and awaited to completion, and the actor yields to the scheduler
between calls — at the service boundaries the checkpoints already name. One
actor is inside a service call at a time, so the concurrency explored here is
transaction-level (separate sessions, separate transactions, real row locks and
real compare-and-swap predicates), not statement-level.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import random
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypeVar

from alkera_core.authz.principal import ActingContext, Principal, PrincipalKind
from alkera_core.files.checkpoints import CheckpointKilled, PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.content import ContentService
from alkera_core.files.errors import Conflict, FilesError, NotFound, PreconditionFailed
from alkera_core.files.fsck import FsckReport, run_fsck
from alkera_core.files.gc import Janitor, SweepResult
from alkera_core.files.ids import DomainId, DriveId, NodeId, OrgScope
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.errors import StoreError
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from alkera_test_support.files.faulty_store import Fault, FaultKind, FaultSchedule, FaultyStore
from scheduler import Sim
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

T = TypeVar("T")

#: Every object a writer stages or publishes is "old", so nothing is spared by
#: the age horizon and the session/grant roots are the *only* thing standing
#: between a sweeper and an object a commit is about to reference. That is the
#: race between commit and GC, so the harness runs it on every seed rather than by luck.
ANCIENT = datetime(2025, 1, 1, tzinfo=UTC)

#: The fault kinds a random schedule may draw: the 5xx and the 429 of the lane
#: brief, plus the outage they degrade into. Read faults are excluded — they
#: break the *reader*, not the commit-vs-sweep ordering under test.
FAULT_KINDS: tuple[FaultKind, ...] = ("server_error", "throttled", "unavailable")

#: Where a crasher may kill a writer. Each is a real ``ContentService``
#: checkpoint, so a kill leaves exactly the partial state a SIGKILL there would.
CONTENT_KILL_POINTS: tuple[str, ...] = (
    "content.after_store_put",
    "content.after_head",
    "content.before_commit",
    "content.after_commit_before_cleanup",
)

#: What a writer is allowed to lose to: a lost race, a fault, or its own crash.
EXPECTED: tuple[type[BaseException], ...] = (
    PreconditionFailed,
    Conflict,
    NotFound,
    StoreError,
    FilesError,
    CheckpointKilled,
)


def sim_ctx() -> ActingContext:
    """An acting context for a simulated writer."""
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER, id=str(uuid.uuid4()), org_id=uuid.uuid4()
        )
    )


async def stream(payload: bytes, *, chunk: int = 11) -> AsyncIterator[bytes]:
    """The bytes as a client sends them: many chunks, never one buffer."""
    for start in range(0, len(payload), chunk):
        yield payload[start : start + chunk]


#: The only wall clock left in a simulation, and a backstop rather than a budget.
#:
#: A bridged call is waited on by the thing's own signal — the future the loop
#: resolves when the coroutine returns — so a slow call is waited for however
#: loaded the box is. What has no signal of its own is a call that will never
#: return: one actor blocked on a row lock held by another actor's still-open
#: transaction, which only the sim thread can advance, and the sim thread is the
#: one blocked here. Nothing ever wakes that, so the wait needs an end.
#:
#: Deliberately longer than the suite's ninety-second per-test limit, which is
#: what reports a hang. A deadline shorter than that limit cannot report
#: anything the limit would not, and it can invent a failure the limit would
#: not: at sixty seconds the move storm turned a run that was still doing real
#: work on a loaded host into a red.
DEADLOCK_BACKSTOP_SECONDS: Final = 150.0


class Bridge:
    """Runs a real service coroutine on the test's loop from the sim thread."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        timeout: float = DEADLOCK_BACKSTOP_SECONDS,
    ) -> None:
        self._loop = loop
        self._timeout = timeout

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Await ``coro`` on the loop, until it completes or cannot."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        # Waiting and reading the result are separate calls so the two
        # ``TimeoutError``s cannot be confused: ``result(timeout)`` raises the
        # same class whether the WAIT ran out or the CALL itself timed out, and
        # only the first of those is the deadlock this is here to catch.
        if not concurrent.futures.wait([future], timeout=self._timeout).done:
            # Cancelling the concurrent future cancels the task on the loop, so
            # the coroutine's connection goes back to the pool instead of
            # holding a lock the rest of the run would queue behind.
            future.cancel()
            msg = (
                f"a bridged call did not return in {self._timeout:g}s — whatever it is "
                "waiting for is held by an actor that cannot run until this call returns"
            )
            raise AssertionError(msg)
        return future.result()


class AdminOnlyFactory:
    """A ``ScopedStoreFactory`` that only ever hands out the bucket-wide handle."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def for_domain(self, domain_id: Any) -> Any:
        raise AssertionError("the janitor never takes a domain-bound handle")

    def admin(self) -> Any:
        return self._store


class ParkingStore:
    """The janitor's admin handle, remembering when it parked each tombstone.

    Phase one *moves* an unreachable object to ``deleted/<key>``; the window
    phase two and ``fsck`` measure runs from that move, not from when the bytes
    it holds were first written, and a real store says so — the filesystem
    driver restamps the destination's mtime on publish, an S3 copy gets a fresh
    ``LastModified``. The stamp here is the simulation's clock, because that is
    the clock the window is measured against.
    """

    def __init__(self, inner: Any, clock: FakeClock) -> None:
        self._inner = inner
        self._clock = clock
        self.parked: dict[str, datetime] = {}

    async def move(self, src: str, dst: str) -> None:
        await self._inner.move(src, dst)
        self.parked[dst] = self._clock.now()

    def __getattr__(self, name: str) -> Any:
        """Every other call is the wrapped store's, unchanged."""
        return getattr(self._inner, name)


class SweepAges:
    """When the janitor is told each object was written.

    Two prefixes, two answers, because the janitor asks for two different
    reasons. Bytes under ``objects/`` or ``incoming/`` are :data:`ANCIENT`, so
    the age horizon spares nothing. A tombstone under ``deleted/`` is as old as
    the move that parked it: dating it to the birth of the bytes it holds would
    make every object a sweep parks instantly older than the seven-day window
    that ``Janitor.expire_deleted`` — the phase that owns ``deleted/``, and one
    this scenario never runs — is the only thing allowed to close.

    A key nobody parked falls back to :data:`ANCIENT`, so a tombstone this rig
    did not put there is still reported rather than quietly excused.
    """

    def __init__(self, store: ParkingStore) -> None:
        self._store = store

    async def written_at(self, key: str) -> datetime | None:
        return self._store.parked.get(key, ANCIENT)


def admin_store(bucket_root: Path, clock: FakeClock) -> ParkingStore:
    """The bucket-wide handle the janitor sweeps through, and its age source."""
    return ParkingStore(FilesystemStore(bucket_root, clock=clock, layout="bucket"), clock)


@dataclass
class WriterSeat:
    """One writer's own session, repo and store handle — nothing is shared."""

    name: str
    session: AsyncSession
    repo: FilesRepo
    store: FaultyStore
    ctx: ActingContext


@dataclass
class SimRig:
    """Everything a commit-vs-GC or move-storm run drives."""

    bridge: Bridge
    clock: FakeClock
    scope: OrgScope
    domain_id: DomainId
    drive_id: DriveId
    bucket_root: Path
    janitor: Janitor
    janitor_repo: FilesRepo
    janitor_session: AsyncSession
    shard: int
    seats: list[WriterSeat]
    nodes: list[NodeId] = field(default_factory=list)
    folders: list[NodeId] = field(default_factory=list)
    swept: set[str] = field(default_factory=set)
    considered: set[str] = field(default_factory=set)
    crash_requests: dict[str, str] = field(default_factory=dict)
    outcomes: list[str] = field(default_factory=list)

    def domain_path(self, relative: str) -> Path:
        return self.bucket_root / "domains" / str(self.domain_id) / relative


def random_schedule(rng: random.Random) -> FaultSchedule:
    """A seeded 5xx/429/outage schedule aimed at the domain's writes."""
    faults = [
        Fault(
            kind=rng.choice(FAULT_KINDS),
            call_index=rng.randrange(0, 6),
            count=rng.randrange(1, 3),
            retry_after=0.0,
        )
        for _ in range(rng.randrange(0, 3))
    ]
    return FaultSchedule(faults=tuple(faults))


async def make_seat(
    engine: AsyncEngine,
    scope: OrgScope,
    domain_id: DomainId,
    bucket_root: Path,
    clock: FakeClock,
    name: str,
    schedule: FaultSchedule,
) -> WriterSeat:
    """One actor's private session on the lane database plus its own store handle."""
    session = AsyncSession(bind=engine, expire_on_commit=False)
    inner = FaultyStore(
        FilesystemStore(bucket_root / "domains" / str(domain_id), clock=clock),
        schedule,
        sleep=_no_sleep,
    )
    return WriterSeat(
        name=name,
        session=session,
        repo=FilesRepo(session, scope),
        store=inner,
        ctx=sim_ctx(),
    )


async def _no_sleep(seconds: float) -> None:
    """A `slow_read` costs simulated patience, never wall-clock time."""
    return None


def content_service(
    rig: SimRig, seat: WriterSeat, checkpoints: PausingCheckpoints
) -> ContentService:
    """A fresh service for one upload, so a kill is armed per attempt."""
    return ContentService(
        seat.repo,
        seat.ctx,
        rig.clock,
        _RootedDomainStore(seat.store, rig.domain_id),
        checkpoints=checkpoints,
    )


# -- actors ---------------------------------------------------------------


async def writer(
    sim: Sim, rig: SimRig, seat: WriterSeat, *, rounds: int, rng: random.Random
) -> None:
    """Upload versions onto random file nodes through the real content service."""
    for _ in range(rounds):
        await sim.step("pick")
        node_id = rng.choice(rig.nodes)
        # Above `files_inline_max_bytes`: an inlined version never touches the
        # store, and a commit that never wrote an object cannot race a sweeper.
        payload = rng.randbytes(rng.randrange(70_000, 90_000))
        checkpoints = PausingCheckpoints()
        armed = rig.crash_requests.pop(seat.name, None)
        if armed is not None:
            checkpoints.kill(armed)
        service = content_service(rig, seat, checkpoints)
        await sim.step("upload")
        try:
            etag = rig.bridge.run(_etag(seat.repo, node_id))
            if etag is None:
                rig.outcomes.append("gone")
                continue
            rig.bridge.run(
                service.put_version(
                    node_id, stream(payload), size_declared=len(payload), if_match=etag
                )
            )
            rig.outcomes.append("committed")
        except EXPECTED as exc:
            rig.outcomes.append(type(exc).__name__)
            rig.bridge.run(_recover(seat.session))
        await sim.step("committed")
        await sim.sleep(1.0)


async def sweeper(sim: Sim, rig: SimRig, *, rounds: int) -> None:
    """Claim the shard and really sweep, the way the janitor schedule does."""
    for _ in range(rounds):
        await sim.sleep(1.0)
        await sim.step("claim")
        claimed = rig.bridge.run(rig.janitor.claim_shard(rig.janitor_repo, rig.shard))
        if not claimed:
            continue
        await sim.step("sweep")
        try:
            result = rig.bridge.run(
                rig.janitor.sweep(rig.domain_id, org=rig.scope, shard=rig.shard, dry_run=False)
            )
        except EXPECTED as exc:
            rig.outcomes.append(f"sweep:{type(exc).__name__}")
            rig.bridge.run(_recover(rig.janitor_session))
            rig.bridge.run(rig.janitor.release_shard(rig.janitor_repo, rig.shard))
            continue
        if isinstance(result, SweepResult):
            rig.swept.update(result.moved)
            rig.considered.update(result.plan.keys)
        await sim.step("finish")
        rig.bridge.run(rig.janitor.finish_shard(rig.janitor_repo, rig.shard))
        rig.bridge.run(rig.janitor.release_shard(rig.janitor_repo, rig.shard))


async def crasher(
    sim: Sim, rig: SimRig, *, names: list[str], rounds: int, rng: random.Random
) -> None:
    """Kill one writer at a random real checkpoint, over and over."""
    for _ in range(rounds):
        await sim.sleep(1.0)
        await sim.step("arm")
        rig.crash_requests[rng.choice(names)] = rng.choice(CONTENT_KILL_POINTS)


async def mover(
    sim: Sim, rig: SimRig, seat: WriterSeat, *, rounds: int, rng: random.Random
) -> None:
    """Re-parent random nodes; cycles and name clashes are the service's problem."""
    namespace = Namespace(seat.repo, seat.ctx, rig.clock)
    for _ in range(rounds):
        await sim.step("pick")
        node_id = rng.choice(rig.folders)
        new_parent = rng.choice(rig.folders)
        await sim.step("move")
        try:
            etag = rig.bridge.run(_etag(seat.repo, node_id))
            if etag is None:
                rig.outcomes.append("gone")
                continue
            rig.bridge.run(_move(seat.repo, namespace, node_id, new_parent, etag))
            rig.outcomes.append("moved")
        except EXPECTED as exc:
            rig.outcomes.append(type(exc).__name__)
            rig.bridge.run(_recover(seat.session))
        await sim.step("moved")
        await sim.sleep(1.0)


async def _move(
    repo: FilesRepo,
    namespace: Namespace,
    node_id: NodeId,
    new_parent: NodeId,
    etag: int,
) -> None:
    """One move in one transaction, the way a request handler runs it."""
    async with repo.transaction():
        await namespace.move(node_id, new_parent, if_match=etag)


async def _etag(repo: FilesRepo, node_id: NodeId) -> int | None:
    async with repo.transaction():
        node = await repo.node(node_id)
        return None if node is None else int(node.etag)


async def _recover(session: AsyncSession) -> None:
    """Put a session back in a usable state after a crash or a refusal."""
    await session.rollback()


# -- invariants -----------------------------------------------------------


async def referenced_keys(session: AsyncSession, drive_id: DriveId) -> set[str]:
    """Every store key a version row still points at."""
    rows = await session.execute(
        select(FileVersion.store_key)
        .join(FileNode, FileNode.id == FileVersion.node_id)
        .where(FileNode.drive_id == uuid.UUID(str(drive_id)))
        .where(FileVersion.store_key.is_not(None))
    )
    return {key for (key,) in rows if key is not None}


async def assert_no_dangling_reference(rig: SimRig, session: AsyncSession) -> None:
    """Invariant 5/6: no version references an object that is not on the store."""
    for key in await referenced_keys(session, rig.drive_id):
        if not rig.domain_path(key).exists():
            msg = f"version references a missing object: {key!r}"
            raise AssertionError(msg)


async def assert_no_referenced_object_moved(rig: SimRig, session: AsyncSession) -> None:
    """Invariant 12: the sweeper never moved an object a version still names."""
    overlap = rig.swept & await referenced_keys(session, rig.drive_id)
    if overlap:
        msg = f"the sweeper moved objects that are still referenced: {sorted(overlap)}"
        raise AssertionError(msg)


#: What a settled run may still show. The folder-stat *cache* is repaired by
#: `fsck` itself; an orphan object is only tolerable when the sweeper named it
#: as a candidate and its circuit breaker declined to move it — every other
#: code (a dangling reference, a bad head pointer, a stuck state machine) is a
#: failure of this scenario, so the list is spelled out rather than "repairable".
REPAIRED_CACHE_CODES: frozenset[str] = frozenset({"fsck.dir_stats_drift"})
ORPHAN_OBJECT = "fsck.orphan_object"
#: Bytes a killed upload left staged under `incoming/`. The sweeper that owns
#: them is `IncomingOrphans`, which runs in the janitor *pass*, not in
#: `Janitor.sweep`; this scenario drives the sweep, so the class is reported
#: rather than collected, and the assertion below still refuses any of them
#: that names a session the run never opened.
INCOMING_PAST_TTL = "fsck.incoming_past_ttl"


async def assert_fsck_clean(rig: SimRig) -> FsckReport:
    """`fsck` finds nothing dangerous, and a repair pass converges to clean.

    The dangerous classes must be absent on the *first* look — a repair pass
    could hide them otherwise — and every orphan object `fsck` does report must
    be one the sweeper already identified, so a lingering object is always one
    the janitor knowingly declined rather than one nobody ever saw.
    """
    first = await run_fsck(rig.janitor, org=rig.scope, domain_id=rig.domain_id, repair_safe=True)
    dangerous = [
        f.code
        for f in first.findings
        if f.code not in REPAIRED_CACHE_CODES and f.code not in (ORPHAN_OBJECT, INCOMING_PAST_TTL)
    ]
    if dangerous:
        msg = f"fsck found {dangerous}"
        raise AssertionError(msg)
    unseen = [
        f.ref_id
        for f in first.findings
        if f.code == ORPHAN_OBJECT and not _was_considered(rig, str(f.ref_id))
    ]
    if unseen:
        msg = f"fsck found orphan objects the sweeper never considered: {sorted(unseen)}"
        raise AssertionError(msg)
    second = await run_fsck(rig.janitor, org=rig.scope, domain_id=rig.domain_id)
    left = [f.code for f in second.findings if f.code not in (ORPHAN_OBJECT, INCOMING_PAST_TTL)]
    if left:
        msg = f"fsck did not converge; a repair pass left {left}"
        raise AssertionError(msg)
    return second


def _was_considered(rig: SimRig, ref: str) -> bool:
    """Whether a sweep listed this object as garbage (absolute or relative key)."""
    return any(ref == key or ref.endswith(f"/{key}") for key in rig.considered)


async def assert_tree_consistent(session: AsyncSession, drive_id: DriveId) -> None:
    """Acyclic, `path_ids` equals the parent walk, and siblings have unique names."""
    rows = (
        await session.execute(select(FileNode).where(FileNode.drive_id == uuid.UUID(str(drive_id))))
    ).scalars()
    nodes = {node.id: node for node in rows}
    for node in nodes.values():
        walk: list[str] = []
        cursor: FileNode | None = node
        seen: set[uuid.UUID] = set()
        while cursor is not None:
            if cursor.id in seen:
                msg = f"the tree has a cycle through {node.id}"
                raise AssertionError(msg)
            seen.add(cursor.id)
            walk.append(cursor.path_ids.rsplit(".", 1)[-1])
            cursor = nodes.get(cursor.parent_id) if cursor.parent_id is not None else None
        expected = ".".join(reversed(walk))
        if node.path_ids != expected:
            msg = f"path_ids {node.path_ids!r} is not the parent walk {expected!r}"
            raise AssertionError(msg)
        if node.depth != len(walk) - 1:
            msg = f"depth {node.depth} does not match the walk of {len(walk) - 1}"
            raise AssertionError(msg)
    siblings: set[tuple[uuid.UUID | None, str]] = set()
    for node in nodes.values():
        if node.trashed_at is not None:
            continue
        slot = (node.parent_id, node.name_key)
        if slot in siblings:
            msg = f"two live siblings share the name {node.name_key!r} under {node.parent_id}"
            raise AssertionError(msg)
        siblings.add(slot)


__all__ = [
    "ANCIENT",
    "CONTENT_KILL_POINTS",
    "DEADLOCK_BACKSTOP_SECONDS",
    "AdminOnlyFactory",
    "Bridge",
    "ParkingStore",
    "SimRig",
    "SweepAges",
    "WriterSeat",
    "admin_store",
    "assert_fsck_clean",
    "assert_no_dangling_reference",
    "assert_no_referenced_object_moved",
    "assert_tree_consistent",
    "crasher",
    "make_seat",
    "mover",
    "random_schedule",
    "sweeper",
    "writer",
]
