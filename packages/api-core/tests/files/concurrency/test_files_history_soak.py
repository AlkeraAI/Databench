"""Eight writers hammer one drive while a checker reads the invariants under them.

The pairwise module next door proves that any *two* operations interleave
without deadlocking. This one asks the harder question: after eight backends
have each done a couple of dozen whatever-they-like operations on one drive —
create, rename, move, trash, restore, put_version, grant, all on the same tree,
all committing — is the drive still a drive?

So there are two roles. Eight **writers**, each on its own session and its own
``FilesRepo``, run a random walk of real service calls against one shared drive.
One **checker** re-derives, from the database alone, the five properties Files
claims are never observable as broken:

1. **The chain a node records is monotone and gap-free.** ``file_history.seq``
   is dense per node — ``1..N`` with no gap and no repeat, which is the property
   :func:`alkera_core.files.history.record` retries the unique index to keep,
   and which a mutation that recorded no history, or two that both won the same
   seq, would break. It is derived from the rows, never from a count the writers
   reported.

   Deliberately not asserted here: anything over the outbox ``version`` column.
   The emitters do not agree on what it means — a namespace change announces the
   etag its row now holds, while others announce ``etag + 1`` — so a chain built
   from it would pin today's disagreement rather than an invariant. It is
   written up as a finding instead.
2. **No two live siblings share a byte-exact name.** ``file_nodes.name`` is
   ``bytea``, so this is over the bytes, not the display form or the fold key.
3. **``path_ids`` equals the parent walk.** The materialized ltree is compared
   against the path rebuilt by walking ``parent_id`` to the root and spelling
   each hop as :func:`alkera_core.files.path_labels.ino_label` does, and ``depth``
   against the walk's length. A move that updated the row but not the subtree
   fails here.
4. **Quota equals the recount.** Where the drive root has a cached
   ``file_dir_stats`` row, it is compared with an independent aggregate over the
   live nodes and their head versions — the definition the cache is supposed to
   hold, recomputed, not the deltas that produced it. Only the content commit
   creates that row, so this one is dormant while ``CONTENT_COMMIT_DEADLOCKS``
   keeps ``put_version`` out of the walk.
5. **The delta feed is a prefix-closed, gap-free rendering of the history.** The
   checker consumes the real feed page by page across the whole soak, carrying
   its token the way a client would. Its cursor never moves backwards, and at
   the end every outbox row for the drive at or below the final cursor has been
   delivered: the feed may repeat an id, it may never skip one.

The module runs that in two shapes. The one in the default suite gives every
writer a **fixed number of operations** and no clock at all: the writers stop
when their quota is spent, and the checker takes its ticks from the recorded
operation count rather than from elapsed time, so a loaded box makes the run
slower and never makes it red. The second shape keeps the original twenty
seconds of wall clock and its "enough work happened in the budget" claim; that
is a load-sensitive measurement rather than an invariant, so it is marked
``perf_wallclock`` and the default suite deselects it (the same split the Files
performance budget rows use).

Snapshot discipline: each invariant is **one** statement, so it reads one
Postgres snapshot and cannot see half of a writer's transaction. Nothing here
sleeps to let a writer win; the writers are simply left to race.

Every writer outcome is either a success or a :class:`FilesError` — a documented
409/412/404 is a normal thing to lose a race with. Anything else (a raw driver
error, a ``40P01`` deadlock, a ``RepoUsageError``) is collected and fails the
test with the operation that raised it.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import acl
from alkera_core.files.clock import SystemClock
from alkera_core.files.content import ContentService
from alkera_core.files.delta import DeltaService, DeltaToken
from alkera_core.files.errors import FilesError
from alkera_core.files.ids import DomainId, DriveId, NodeId, OrgScope, TrashOpId
from alkera_core.files.namespace import Namespace
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.files.trash import Trash
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: How many operations each writer runs in the default-suite shape. Eight
#: writers of twenty operations reaches every verb many times over across the
#: drive, and — unlike a deadline — it is the same amount of work on an idle box
#: and on one running twenty other lanes.
#:
#: It was thirty, which on a CI runner carrying thirty-two of these lanes at once
#: took this test past the suite-wide ninety-second cap and ended its worker. A
#: hundred and sixty real transactions is still far more contention than any
#: invariant here needs, and it leaves room for a box several times slower than
#: the one this was measured on.
OPS_PER_WRITER = 20

#: What this test is allowed to take. The suite-wide cap in ``pyproject.toml`` is
#: a hang detector sized for ordinary tests, and eight backends committing a
#: hundred and sixty real transactions against one drive is not one: it is about
#: five seconds on a developer's machine and tens of seconds on a loaded runner.
#: Reaching this number means the box is the problem, not the soak.
SOAK_TIMEOUT_SECONDS = 120

#: How many completed operations the checker lets pass between two reads. It is
#: a count, not an interval, so the number of reads a run takes is fixed too.
CHECK_EVERY_OPS = 10

#: How long the writers run in the wall-clock shape. Twenty seconds is long
#: enough for the random walk to reach every verb many times over and for the
#: checker to take ~80 reads, and short enough to keep it inside its budget.
SOAK_SECONDS = 20.0

#: How often the checker re-derives the invariants in the wall-clock shape.
CHECK_INTERVAL = 0.25

#: One drive, eight backends on it. The point is contention, so they share the
#: drive rather than each getting their own.
WRITERS = 8

#: How many delta pages the checker will pull in one tick before yielding back.
#: The feed is consumed across the whole soak, not drained in one burst.
PAGES_PER_TICK = 4

DEADLOCK = "40P01"


def _ctx(org: FilesOrg, user_id: uuid.UUID) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(user_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _stream(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


@dataclass
class Pool:
    """What the writers know about the drive, shared and deliberately stale.

    A writer picking an id another writer has just trashed is the interesting
    case, not a bug in the test: the service is expected to answer with a
    documented conflict.
    """

    folders: list[NodeId]
    files: list[NodeId]
    trash_ops: list[TrashOpId] = field(default_factory=list)

    def pick_folder(self, rng: random.Random) -> NodeId:
        return rng.choice(self.folders)

    def pick_file(self, rng: random.Random) -> NodeId | None:
        return rng.choice(self.files) if self.files else None

    def take_trash_op(self) -> TrashOpId | None:
        return self.trash_ops.pop() if self.trash_ops else None


@dataclass
class Writer:
    """One backend's whole world: a session, a repo and the services on it."""

    name: str
    repo: FilesRepo
    namespace: Namespace
    trash: Trash
    content: ContentService
    ctx: ActingContext
    org: FilesOrg
    drive_id: DriveId
    rng: random.Random
    ops: int = 0
    conflicts: int = 0
    fatals: list[str] = field(default_factory=list)


@dataclass
class Violation:
    """One invariant, one snapshot, one reason it did not hold."""

    invariant: str
    detail: str

    def __str__(self) -> str:
        return f"{self.invariant}: {self.detail}"


async def _etag(repo: FilesRepo, node_id: NodeId) -> int:
    node = await repo.node(node_id)
    if node is None:
        return -1
    return int(node.etag)


# -- the verbs ------------------------------------------------------------


async def _do_create(writer: Writer, pool: Pool) -> None:
    parent = pool.pick_folder(writer.rng)
    folder = writer.rng.random() < 0.2
    name = f"{writer.name}-{writer.ops}-{uuid.uuid4().hex[:6]}".encode()
    async with writer.repo.transaction():
        made = await writer.namespace.create(
            writer.drive_id, parent, "folder" if folder else "file", name
        )
        # Read the id inside the transaction: these sessions expire on commit,
        # so an attribute touched afterwards would lazy-load off the event loop.
        made_id = NodeId(made.id)
    (pool.folders if folder else pool.files).append(made_id)


async def _do_rename(writer: Writer, pool: Pool) -> None:
    node_id = pool.pick_file(writer.rng)
    if node_id is None:
        return
    name = f"{writer.name}-r{writer.ops}-{uuid.uuid4().hex[:6]}".encode()
    async with writer.repo.transaction():
        await writer.namespace.rename(node_id, name, if_match=await _etag(writer.repo, node_id))


async def _do_move(writer: Writer, pool: Pool) -> None:
    node_id = pool.pick_file(writer.rng)
    if node_id is None:
        return
    async with writer.repo.transaction():
        await writer.namespace.move(
            node_id, pool.pick_folder(writer.rng), if_match=await _etag(writer.repo, node_id)
        )


async def _do_trash(writer: Writer, pool: Pool) -> None:
    node_id = pool.pick_file(writer.rng)
    if node_id is None:
        return
    async with writer.repo.transaction():
        op = await writer.trash.trash(node_id, if_match=await _etag(writer.repo, node_id))
        op_id = TrashOpId(op.id)
    if node_id in pool.files:
        pool.files.remove(node_id)
    pool.trash_ops.append(op_id)


async def _do_restore(writer: Writer, pool: Pool) -> None:
    """Restore is keyed by the trash operation, never by the path it came from."""
    op_id = pool.take_trash_op()
    if op_id is None:
        return
    async with writer.repo.transaction():
        restored = await writer.trash.restore(op_id)
        back = NodeId(restored.id) if restored.kind == "file" else None
    if back is not None:
        pool.files.append(back)


async def _do_put_version(writer: Writer, pool: Pool) -> None:
    """The one verb that owns its transactions, so none is opened around it."""
    node_id = pool.pick_file(writer.rng)
    if node_id is None:
        return
    async with writer.repo.transaction():
        etag = await _etag(writer.repo, node_id)
    payload = f"{writer.name}:{writer.ops}:{uuid.uuid4().hex}".encode()
    await writer.content.put_version(
        node_id, _stream(payload), size_declared=len(payload), if_match=etag
    )


async def _do_grant(writer: Writer, pool: Pool) -> None:
    node_id = pool.pick_file(writer.rng)
    if node_id is None:
        return
    principal = Principal(
        kind=PrincipalKind.USER,
        id=str(writer.org.member_id),
        org_id=writer.org.org_team_id,
        credential=CredentialKind.JWT,
    )
    async with writer.repo.transaction():
        node = await writer.repo.node(node_id)
        if node is None:
            return
        await acl.grant(writer.repo, writer.ctx, node, principal, "reader")


#: ``put_version`` IS in the walk. It used to be out: the reserve phase opened
#: its upload session — an INSERT whose foreign keys take ``FOR KEY SHARE`` on
#: the node and the drive — before ``QuotaService.reserve`` locked the drive,
#: so a writer waiting for the drive already held a node row another writer's
#: drive-locked commit was queued on, and Postgres cut the cycle with ``40P01``.
#: The reserve phase now takes the drive ``FOR UPDATE`` first, which is the
#: whole of the fixed drive → parent → node → version order; the pair matrix
#: next door pins the two-transaction shape of it.
CONTENT_COMMIT_DEADLOCKS = False

#: The random walk. Weighted so the tree grows a little faster than it shrinks
#: and every verb is reached hundreds of times over the soak.
VERBS: tuple[tuple[str, Callable[[Writer, Pool], Awaitable[None]], int], ...] = (
    ("create", _do_create, 3),
    ("rename", _do_rename, 2),
    ("move", _do_move, 2),
    ("trash", _do_trash, 2),
    ("restore", _do_restore, 2),
    ("grant", _do_grant, 2),
    *(() if CONTENT_COMMIT_DEADLOCKS else (("put_version", _do_put_version, 2),)),
)

_CHOICES = tuple(name for name, _, weight in VERBS for _ in range(weight))
_BY_NAME = {name: run for name, run, _ in VERBS}


@dataclass
class Progress:
    """The operation count the counted checker takes its ticks from.

    The event is only a wake-up hint — every decision is re-read from the
    counters — so a wake-up that arrives late cannot skip a read, and the
    retirement of the last writer is what ends the checker's loop.
    """

    every: int
    live: int
    done: int = 0
    checked_at: int = 0
    tick: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def due(self) -> bool:
        return self.done - self.checked_at >= self.every

    def advance(self) -> None:
        self.done += 1
        if self.due:
            self.tick.set()

    def mark(self) -> None:
        """Charge one read. Advancing by ``every`` rather than jumping to
        ``done`` means a backlog is worked off read by read, so a run of N
        operations takes exactly ``N // every`` reads however the writers and
        the checker happened to interleave."""
        self.checked_at += self.every

    def retire(self) -> None:
        self.live -= 1
        self.tick.set()


async def _step(writer: Writer, pool: Pool) -> bool:
    """Run one verb. ``False`` when the writer has to give up."""
    verb = writer.rng.choice(_CHOICES)
    writer.ops += 1
    survives = True
    try:
        await _BY_NAME[verb](writer, pool)
    except FilesError:
        writer.conflicts += 1
    except Exception as exc:
        writer.fatals.append(f"{writer.name} {verb} raised {type(exc).__name__}: {exc}")
        if DEADLOCK in str(exc) or len(writer.fatals) > 4:
            survives = False
    await asyncio.sleep(0)
    return survives


async def _run_writer_ops(writer: Writer, pool: Pool, ops: int, progress: Progress) -> None:
    """Spend a fixed quota of operations. No clock is read."""
    try:
        for _ in range(ops):
            if not await _step(writer, pool):
                return
            progress.advance()
    finally:
        progress.retire()


async def _run_writer(writer: Writer, pool: Pool, deadline: float) -> None:
    loop = asyncio.get_running_loop()
    while loop.time() < deadline:
        if not await _step(writer, pool):
            return


# -- the invariants -------------------------------------------------------


async def _history_is_dense(session: AsyncSession, drive_id: DriveId) -> list[Violation]:
    """``file_history.seq`` is ``1..N`` per node: no gap, no repeat."""
    rows = (
        await session.execute(
            text(
                "SELECT h.node_id, count(*), count(DISTINCT h.seq), min(h.seq), max(h.seq) "
                "FROM file_history h JOIN file_nodes n ON n.id = h.node_id "
                "WHERE n.drive_id = :drive GROUP BY h.node_id"
            ),
            {"drive": str(drive_id)},
        )
    ).all()
    bad: list[Violation] = []
    for node_id, total, distinct, lowest, highest in rows:
        if (int(total), int(distinct), int(lowest), int(highest)) != (
            int(total),
            int(total),
            1,
            int(total),
        ):
            bad.append(
                Violation(
                    "history seq is dense per node",
                    f"node {node_id} has {total} rows, {distinct} distinct, "
                    f"seq {lowest}..{highest}",
                )
            )
    return bad


async def _siblings_are_unique(session: AsyncSession, drive_id: DriveId) -> list[Violation]:
    """Two live children of one folder never carry the same name bytes."""
    rows = (
        await session.execute(
            text(
                "SELECT parent_id, encode(name, 'escape'), count(*) FROM file_nodes "
                "WHERE drive_id = :drive AND trashed_at IS NULL AND parent_id IS NOT NULL "
                "GROUP BY parent_id, name HAVING count(*) > 1"
            ),
            {"drive": str(drive_id)},
        )
    ).all()
    return [
        Violation(
            "live siblings have distinct name bytes",
            f"folder {parent_id} holds {count} live children named {name!r}",
        )
        for parent_id, name, count in rows
    ]


async def _paths_match_the_walk(session: AsyncSession, drive_id: DriveId) -> list[Violation]:
    """``path_ids`` and ``depth`` equal what walking ``parent_id`` produces.

    One statement, so the whole tree is one snapshot; the walk itself is done
    here rather than in SQL because the label spelling is
    :func:`alkera_core.files.path_labels.ino_label`, and rebuilding it independently is
    the point — comparing the row against itself would prove nothing.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id, ino, parent_id, path_ids::text, depth FROM file_nodes "
                "WHERE drive_id = :drive"
            ),
            {"drive": str(drive_id)},
        )
    ).all()
    parents = {row[0]: row[2] for row in rows}
    inos = {row[0]: int(row[1]) for row in rows}
    bad: list[Violation] = []
    for node_id, _, _, stored, depth in rows:
        labels: list[str] = []
        walk: Any = node_id
        seen: set[Any] = set()
        while walk is not None and walk in inos:
            if walk in seen:
                bad.append(Violation("path_ids equals the parent walk", f"cycle at {node_id}"))
                labels = []
                break
            seen.add(walk)
            labels.append(ino_label(inos[walk]))
            walk = parents[walk]
        if not labels:
            continue
        expected = ".".join(reversed(labels))
        if stored != expected:
            bad.append(
                Violation(
                    "path_ids equals the parent walk",
                    f"node {node_id} stores {stored!r}, the walk says {expected!r}",
                )
            )
        if int(depth) != len(labels) - 1:
            bad.append(
                Violation(
                    "depth equals the walk length",
                    f"node {node_id} stores depth {depth}, the walk is {len(labels) - 1} deep",
                )
            )
    return bad


#: Left from when the root ``file_dir_stats`` cache was fed by the content commit
#: alone, so the walk's creates and trashes never reached it. One owner now
#: appends every change against the changed node's parent and the check below
#: reads what the quota reads (the folded root plus the deltas the
#: aggregator has not folded yet), so the comparison is an invariant again.
DIR_STATS_COUNTS_ONLY_CONTENT = False


async def _quota_equals_the_recount(session: AsyncSession, drive_id: DriveId) -> list[Violation]:
    """The drive's accounted usage equals an independent recount.

    *Accounted* is what the quota reads: the folded root row plus every
    delta row beneath it the aggregator has not folded yet. Reading the folded
    row alone would pin the aggregator's cadence rather than the aggregate.
    """
    row = (
        await session.execute(
            text(
                "SELECT COALESCE(s.bytes, 0) + COALESCE(pend.bytes, 0), "
                "COALESCE(s.files, 0) + COALESCE(pend.files, 0), "
                "COALESCE(agg.bytes, 0), COALESCE(agg.files, 0) "
                "FROM file_drives d "
                "JOIN file_nodes f ON f.id = d.root_node_id "
                "LEFT JOIN file_dir_stats s ON s.node_id = f.id "
                "LEFT JOIN LATERAL ("
                "  SELECT SUM(x.bytes_delta) AS bytes, SUM(x.files_delta) AS files "
                "  FROM file_dir_stats_deltas x "
                "  JOIN file_nodes p ON p.id = x.node_id "
                "  WHERE p.drive_id = d.id"
                ") pend ON TRUE "
                "LEFT JOIN LATERAL ("
                "  SELECT SUM(v.size_bytes) AS bytes, COUNT(*) AS files "
                "  FROM file_nodes c JOIN file_versions v ON v.id = c.head_version_id "
                # `path_ids` labels are per-drive inos, so the same chain exists
                # in every drive: without the drive equality this recount adds up
                # other drives' files (`fsck` spells the same predicate).
                "  WHERE c.drive_id = f.drive_id "
                "  AND c.path_ids <@ f.path_ids AND c.id <> f.id "
                # A trashed node still holds its bytes in the store until it is
                # purged, so the trash charges only the folder's child count and
                # the recount counts a trashed file exactly like a live one.
                "  AND c.kind = 'file'"
                ") agg ON TRUE "
                "WHERE d.id = :drive"
            ),
            {"drive": str(drive_id)},
        )
    ).first()
    if row is None:
        # The drive itself is gone (the walk can tear one down mid-pass); there
        # is nothing to compare, and an absent drive is not a drifted cache.
        return []
    cached_bytes, cached_files, real_bytes, real_files = (int(value) for value in row)
    if (cached_bytes, cached_files) == (real_bytes, real_files):
        return []
    return [
        Violation(
            "quota equals the recount",
            f"cached ({cached_bytes} bytes, {cached_files} files) but the recount says "
            f"({real_bytes} bytes, {real_files} files)",
        )
    ]


@dataclass
class FeedConsumer:
    """A client of the real delta feed, carrying its token across the soak."""

    service: DeltaService
    repo: FilesRepo
    drive_id: DriveId
    token: DeltaToken | None = None
    cursor: int = 0
    seen: set[str] = field(default_factory=set)
    pages: int = 0
    violations: list[Violation] = field(default_factory=list)

    async def pump(self, pages: int) -> None:
        for _ in range(pages):
            async with self.repo.transaction():
                page = await self.service.read(self.drive_id, token=self.token)
            self.pages += 1
            for item in page.items:
                self.seen.add(str(item.id))
            token = page.next_link or page.delta_link
            assert token is not None
            if token.outbox_id < self.cursor:
                self.violations.append(
                    Violation(
                        "the delta cursor never moves backwards",
                        f"{self.cursor} then {token.outbox_id}",
                    )
                )
            self.cursor = max(self.cursor, token.outbox_id)
            self.token = token
            if page.next_link is None:
                return


async def _check_once(session: AsyncSession, drive_id: DriveId) -> list[Violation]:
    found: list[Violation] = []
    found += await _history_is_dense(session, drive_id)
    found += await _siblings_are_unique(session, drive_id)
    found += await _paths_match_the_walk(session, drive_id)
    if not DIR_STATS_COUNTS_ONLY_CONTENT:
        found += await _quota_equals_the_recount(session, drive_id)
    return found


async def _run_checker(
    session: AsyncSession,
    feed: FeedConsumer,
    drive_id: DriveId,
    deadline: float,
) -> tuple[int, list[Violation]]:
    loop = asyncio.get_running_loop()
    found: list[Violation] = []
    ticks = 0
    while loop.time() < deadline:
        ticks += 1
        found += await _check_once(session, drive_id)
        await feed.pump(PAGES_PER_TICK)
        await asyncio.sleep(CHECK_INTERVAL)
    return ticks, found


async def _run_checker_ops(
    session: AsyncSession,
    feed: FeedConsumer,
    drive_id: DriveId,
    progress: Progress,
) -> tuple[int, list[Violation]]:
    """Read the invariants once every ``progress.every`` completed operations.

    The loop ends when the last writer retires and the backlog of reads that
    the operations already earned has been worked off — no clock is consulted,
    so the reads a run takes are a function of the work, not of the box.
    """
    found: list[Violation] = []
    ticks = 0
    while progress.live > 0 or progress.due:
        if not progress.due:
            await progress.tick.wait()
            progress.tick.clear()
            continue
        progress.mark()
        ticks += 1
        found += await _check_once(session, drive_id)
        await feed.pump(PAGES_PER_TICK)
    return ticks, found


async def _undelivered(session: AsyncSession, drive_id: DriveId, cursor: int) -> list[str]:
    """Outbox rows for this drive at or below the cursor the feed did not deliver."""
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT o.entity_id FROM event_outbox o "
                "WHERE o.entity = 'file_node' AND o.payload->>'drive_id' = :drive "
                "AND o.id <= :cursor"
            ),
            {"drive": str(drive_id), "cursor": cursor},
        )
    ).all()
    return [str(row[0]) for row in rows]


# -- the soak -------------------------------------------------------------


@dataclass
class Harness:
    """One drive, eight writers on it, and the checker's own two sessions."""

    writers: list[Writer]
    pool: Pool
    checker_session: AsyncSession
    feed: FeedConsumer
    drive_id: DriveId


async def _build(
    files_org: FilesOrg,
    files_factory: FilesFactory,
    sessions: Callable[..., Awaitable[list[Any]]],
    tmp_path: Path,
) -> Harness:
    drive = await files_factory.drive(org=files_org)
    tree = await files_factory.tree("a/ b/ c/ a/one.bin a/two.bin b/three.bin", drive=drive)
    drive_id = DriveId(drive.id)
    domain_id = DomainId(drive.dedup_domain_id)
    scope = OrgScope(org_team_id=files_org.org_team_id)
    pool = Pool(
        folders=[NodeId(drive.root_node_id), *(NodeId(tree[k].id) for k in ("a", "b", "c"))],
        files=[NodeId(tree[k].id) for k in ("a/one.bin", "a/two.bin", "b/three.bin")],
    )

    opened = await sessions(WRITERS + 2)
    clock = SystemClock()
    ctx = _ctx(files_org, files_org.admin_id)

    def store() -> _RootedDomainStore:
        return _RootedDomainStore(
            FilesystemStore(tmp_path / "domains" / str(domain_id), clock=clock.now), domain_id
        )

    writers: list[Writer] = []
    for index in range(WRITERS):
        repo = FilesRepo(opened[index], scope)
        writers.append(
            Writer(
                name=f"w{index}",
                repo=repo,
                namespace=Namespace(repo, ctx, clock, store()),
                trash=Trash(repo, ctx, clock, store()),
                content=ContentService(repo, ctx, clock, store()),
                ctx=ctx,
                org=files_org,
                drive_id=drive_id,
                rng=random.Random(9000 + index),
            )
        )

    feed_repo = FilesRepo(opened[WRITERS + 1], scope)
    return Harness(
        writers=writers,
        pool=pool,
        checker_session=opened[WRITERS],
        feed=FeedConsumer(
            service=DeltaService(feed_repo, ctx, clock, signing_key="soak-key"),
            repo=feed_repo,
            drive_id=drive_id,
        ),
        drive_id=drive_id,
    )


async def _race(
    items: Sequence[Any],
    run_one: Callable[[Any], Coroutine[Any, Any, None]],
    run_checker: Callable[[], Coroutine[Any, Any, Any]],
) -> Any:
    """Run the writers and the checker, and never leave one mid-statement.

    ``asyncio.gather`` returns on the first exception with the other tasks
    still running: the test then tears its sessions down while eight
    connections still have a statement in flight, and asyncpg answers the close
    with ``InterfaceError: cannot perform operation: another operation is in
    progress`` — a teardown ERROR that replaces the failure that actually
    happened. A ``TaskGroup`` cancels the siblings and *awaits* them, so every
    connection has unwound before the fixture closes it. The first exception is
    re-raised on its own rather than as a group, so the failure reads as what
    it is.
    """
    try:
        async with asyncio.TaskGroup() as group:
            for item in items:
                group.create_task(run_one(item))
            checking = group.create_task(run_checker())
    except BaseExceptionGroup as failures:
        # `from None` deliberately: the group is only the wrapper this helper
        # put around the failure, so showing it as the cause would bury the one
        # line a reader needs.
        raise failures.exceptions[0] from None
    return checking.result()


async def test_the_race_cancels_its_siblings_when_one_of_them_fails() -> None:
    """The first failure ends the run: the siblings are cancelled AND awaited.

    Without that, the tasks a failure abandons are still on their connections
    when the session fixture closes them, and the run reports asyncpg's
    "another operation is in progress" instead of the error it hit.
    """
    running = asyncio.Event()
    cancelled: list[str] = []

    async def sibling(name: str) -> None:
        running.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    async def checker() -> tuple[int, list[Violation]]:
        await running.wait()
        raise RuntimeError("the checker lost its connection")

    with pytest.raises(RuntimeError, match="lost its connection"):
        async with asyncio.timeout(10):
            await _race(["w0", "w1"], sibling, checker)

    assert sorted(cancelled) == ["w0", "w1"], (
        f"a sibling was left running after the first failure: cancelled={cancelled}"
    )


async def _settle(harness: Harness, violations: list[Violation]) -> list[Violation]:
    """Drain the feed and take one last read against a quiet database."""
    for _ in range(200):
        before = harness.feed.cursor
        await harness.feed.pump(PAGES_PER_TICK)
        if harness.feed.cursor == before:
            break
    return [
        *violations,
        *harness.feed.violations,
        *await _check_once(harness.checker_session, harness.drive_id),
    ]


async def _assert_the_drive_survived(harness: Harness, violations: list[Violation]) -> None:
    """The five invariants, and the feed's no-skip claim, over the whole run."""
    fatals = [line for writer in harness.writers for line in writer.fatals]
    assert not fatals, "writers hit undocumented failures:\n" + "\n".join(fatals)
    assert not violations, "invariants broken under load:\n" + "\n".join(
        str(item) for item in violations
    )

    delivered = await _undelivered(harness.checker_session, harness.drive_id, harness.feed.cursor)
    missing = sorted(set(delivered) - harness.feed.seen)
    assert not missing, (
        f"the delta feed skipped {len(missing)} node(s) at or below cursor "
        f"{harness.feed.cursor}: {missing[:5]}"
    )

    conflicts = sum(writer.conflicts for writer in harness.writers)
    assert conflicts > 0, "no writer ever lost a race; the drive was not contended"
    assert harness.feed.pages > 1, "the delta feed was never read"


@pytest.mark.timeout(SOAK_TIMEOUT_SECONDS)
async def test_eight_writers_never_break_the_invariants(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    sessions: Callable[..., Awaitable[list[Any]]],
    tmp_path: Path,
) -> None:
    """Eight backends, twenty operations each, read every ten operations.

    No clock anywhere: the run ends when the quota is spent, so a box under
    load makes this slower and never makes it red.
    """
    harness = await _build(files_org, files_factory, sessions, tmp_path)
    progress = Progress(every=CHECK_EVERY_OPS, live=WRITERS)
    ticks, violations = await _race(
        harness.writers,
        lambda writer: _run_writer_ops(writer, harness.pool, OPS_PER_WRITER, progress),
        lambda: _run_checker_ops(harness.checker_session, harness.feed, harness.drive_id, progress),
    )

    await _assert_the_drive_survived(harness, await _settle(harness, violations))

    total_ops = sum(writer.ops for writer in harness.writers)
    assert total_ops == WRITERS * OPS_PER_WRITER, f"only {total_ops} operations ran"
    assert ticks == total_ops // CHECK_EVERY_OPS, f"the checker read {ticks} times"


@pytest.mark.perf_wallclock
async def test_eight_writers_hold_the_invariants_for_twenty_seconds(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    sessions: Callable[..., Awaitable[list[Any]]],
    tmp_path: Path,
) -> None:
    """The same invariants, but against a wall clock: twenty seconds of eight
    backends has to reach a real amount of work inside a one-minute budget.

    How much work a box gets through in twenty seconds is a measurement, not an
    invariant, so this half is deselected by default and the nightly perf job on
    a quiet runner is what selects it.
    """
    harness = await _build(files_org, files_factory, sessions, tmp_path)
    started = time.monotonic()
    deadline = asyncio.get_running_loop().time() + SOAK_SECONDS
    ticks, violations = await _race(
        harness.writers,
        lambda writer: _run_writer(writer, harness.pool, deadline),
        lambda: _run_checker(harness.checker_session, harness.feed, harness.drive_id, deadline),
    )

    await _assert_the_drive_survived(harness, await _settle(harness, violations))

    total_ops = sum(writer.ops for writer in harness.writers)
    assert total_ops > 200, f"only {total_ops} operations ran"
    assert ticks > 20, f"the checker only read {ticks} times"
    assert time.monotonic() - started < 60.0, "the soak overran its budget"
