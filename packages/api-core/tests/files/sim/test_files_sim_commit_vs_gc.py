"""Content commit versus the GC sweep, explored by seed.

Four writers upload through the real ``ContentService`` while a sweeper runs the
real ``Janitor.sweep(dry_run=False)`` and a crasher kills a writer at a random
``ContentService`` checkpoint — all against this run's Postgres, a real
filesystem store wrapped in ``FaultyStore`` with a seeded 5xx/429 schedule, and
an age source that spares no live object, so the session and grant roots are the
only thing standing between the sweeper and an object a commit is about to
reference.

After every run: no version references a missing object, the sweeper never moved
an object a version still names, and ``fsck`` is clean. One seed is one case,
named by the seed, so a failure names the seed it ran in its own node id.
"""

from __future__ import annotations

import asyncio
import importlib.util
import random
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Final

import pytest
from actors_files import (
    AdminOnlyFactory,
    Bridge,
    SimRig,
    SweepAges,
    WriterSeat,
    admin_store,
    assert_fsck_clean,
    assert_no_dangling_reference,
    assert_no_referenced_object_moved,
    assert_tree_consistent,
    crasher,
    make_seat,
    random_schedule,
    sweeper,
    writer,
)
from alkera_core.files import stats
from alkera_core.files.clock import FakeClock
from alkera_core.files.fsck import run_fsck
from alkera_core.files.gc import Janitor, SweepResult
from alkera_core.files.ids import DomainId, DriveId, NodeId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.platform import FileSweepShard
from alkera_test_support.files.faulty_store import FaultSchedule
from scheduler import PauseLongestPolicy, RandomPolicy, ReorderCommitsPolicy, Sim
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

# Each case builds its own rig on the shared engine and seeds its own tenant, so
# nothing one seed leaves behind is visible to the next.
pytestmark = [pytest.mark.spread]


def _files_kit() -> Any:
    """The suite's factories, loaded by path.

    The test folders have no packages and three apps have a ``tests.files`` of their
    own, so ``from tests.files.conftest import …`` resolves to whichever tree
    pytest inserted first. Loading the sibling conftest by path is the same
    trick ``packages/api-core/tests/files/gc/conftest.py`` uses to borrow the two-session kit.
    """
    source = Path(__file__).resolve().parent.parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("files_sim_conftest_kit", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FilesFactory = _files_kit().FilesFactory
FilesOrg = _files_kit().FilesOrg

WRITERS = 4
"""The lane's shape: four writers, one sweeper, one crasher."""

ROUNDS = 3
"""Uploads per writer — small, because the interleaving is what varies."""

FILES = 4
"""File nodes the writers contend over; fewer nodes means more real races."""

#: Two fixed seeds for the case that pins the crasher itself. Which seeds they
#: are does not matter: the lane shape below, not the interleaving, is what
#: makes the kill land.
KILL_SEEDS: Final = (1, 2)


def _policy(seed: int) -> RandomPolicy | PauseLongestPolicy | ReorderCommitsPolicy:
    """Rotate the explorer so a seed set covers all three shapes."""
    return (
        RandomPolicy(),
        ReorderCommitsPolicy(delay=3, label="committed"),
        PauseLongestPolicy(rounds=6),
    )[seed % 3]


@asynccontextmanager
async def _rig(
    files_session: AsyncSession,
    files_org: FilesOrg,
    clock: FakeClock,
    tmp_path: Path,
    seed: int,
    engine: AsyncEngine,
    *,
    writers: int = WRITERS,
    faults: bool = True,
) -> AsyncIterator[SimRig]:
    """A drive, its store, a janitor and one private session per actor.

    Every session the rig opens is closed when the case ends — including when the
    build itself fails halfway — because the cases that run after this one on the
    same worker borrow their connections from the same pool.
    """
    rng = random.Random(seed)
    # One org owns one `org` drive, so every seed gets its own tenant.
    files_factory = FilesFactory(files_session, files_org)
    drive = await files_factory.drive()
    spec = " ".join(f"f{i}.bin" for i in range(FILES))
    tree = await files_factory.tree(spec, drive=drive)
    scope = OrgScope(org_team_id=files_org.org_team_id)
    domain_id = DomainId(drive.dedup_domain_id)
    bucket_root = tmp_path / "bucket"
    shard = uuid.uuid4().int % 1_000_000 + 1_000
    files_session.add(FileSweepShard(shard=shard, cursor={}))
    await files_session.commit()

    async with AsyncExitStack() as stack:
        janitor_session = AsyncSession(bind=engine, expire_on_commit=False)
        stack.push_async_callback(janitor_session.close)
        janitor_repo = FilesRepo(janitor_session, scope)
        store = admin_store(bucket_root, clock)
        janitor = Janitor(
            lambda _scope: janitor_repo,
            AdminOnlyFactory(store),
            clock,
            age_source=SweepAges(store),
        )
        seats: list[WriterSeat] = []
        for index in range(writers):
            schedule = random_schedule(rng) if faults else FaultSchedule(faults=())
            seat = await make_seat(
                engine, scope, domain_id, bucket_root, clock, f"w{index}", schedule
            )
            stack.push_async_callback(seat.session.close)
            seats.append(seat)
        rig = SimRig(
            bridge=Bridge(asyncio.get_running_loop()),
            clock=clock,
            scope=scope,
            domain_id=domain_id,
            drive_id=DriveId(drive.id),
            bucket_root=bucket_root,
            janitor=janitor,
            janitor_repo=janitor_repo,
            janitor_session=janitor_session,
            shard=shard,
            seats=seats,
            nodes=[NodeId(tree[f"f{i}.bin"].id) for i in range(FILES)],
        )
        # The factories seed rows directly, so a fresh drive already carries the
        # directory-stat drift the product's own writes would have avoided; repair
        # it once here so "fsck is clean" at the end is a claim about the run.
        await stats.aggregate([janitor_repo])
        baseline = await run_fsck(janitor, org=scope, domain_id=domain_id, repair_safe=True)
        assert baseline.codes in ((), ("fsck.dir_stats_drift",)), baseline.codes
        yield rig


async def test_commit_never_loses_an_object_to_the_sweeper(
    files_session: AsyncSession,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    clock: FakeClock,
    tmp_path: Path,
    sim_seed: int,
    sim_engine: AsyncEngine,
) -> None:
    """This seed: no dangling reference, nothing referenced was moved, fsck clean."""
    async with _rig(
        files_session, await files_org_factory(), clock, tmp_path, sim_seed, sim_engine
    ) as rig:
        rng = random.Random(sim_seed)
        sim = Sim(sim_seed, policy=_policy(sim_seed))
        for seat in rig.seats:
            sim.spawn(
                seat.name,
                writer(
                    sim, rig, seat, rounds=ROUNDS, rng=random.Random(sim_seed ^ hash(seat.name))
                ),
            )
        sim.spawn("sweeper", sweeper(sim, rig, rounds=ROUNDS))
        sim.spawn(
            "crasher",
            crasher(sim, rig, names=[s.name for s in rig.seats], rounds=ROUNDS, rng=rng),
        )
        try:
            await asyncio.to_thread(sim.run)
            # Drain: the run left whatever a crash left; the sweeper's job is to
            # take exactly that and nothing a version still names.
            await rig.janitor.claim_shard(rig.janitor_repo, rig.shard)
            result = await rig.janitor.sweep(
                rig.domain_id, org=rig.scope, shard=rig.shard, dry_run=False
            )
            rig.swept.update(result.moved if isinstance(result, SweepResult) else ())
            if isinstance(result, SweepResult):
                rig.considered.update(result.plan.keys)
            await rig.janitor.finish_shard(rig.janitor_repo, rig.shard)
            # The folder-stat cache is folded by the janitor's own pass, so the
            # drain runs it too — otherwise "fsck clean" would be asserting
            # that a background sweeper had already happened to run.
            await stats.aggregate([rig.janitor_repo])
            await assert_no_dangling_reference(rig, rig.janitor_session)
            await assert_no_referenced_object_moved(rig, rig.janitor_session)
            await assert_fsck_clean(rig)
            await assert_tree_consistent(rig.janitor_session, rig.drive_id)
        except BaseException as exc:
            sim.annotate(exc)
            raise
        # An invariant nothing exercised is not evidence: this seed really
        # committed uploads, and the sweeper really had unreferenced bytes in
        # front of it — which is what makes "none of them was referenced" mean
        # something. (``swept`` is not asserted: the circuit breaker legitimately
        # refuses a sweep that would move a large fraction of a small domain, so
        # a seed can consider candidates and move none of them.)
        assert rig.outcomes.count("committed") > 0, f"seed {sim_seed}: no upload committed"
        assert rig.considered, f"seed {sim_seed}: the sweeper never saw a candidate"


async def test_the_crasher_kills_a_writer_mid_upload(
    files_session: AsyncSession,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    clock: FakeClock,
    tmp_path: Path,
    sim_engine: AsyncEngine,
) -> None:
    """The partial state the invariants are interesting about is really produced.

    The window's cases cannot carry this claim. An armed kill is spent the moment
    its writer starts an upload, so on the full lane it is regularly lost before
    the checkpoint — to a 5xx from the fault schedule, or to a race another
    writer won — and roughly a quarter of the window's seeds end with no kill at
    all. One writer and no faults leave nothing between the arming and the
    checkpoint.
    """
    crashed = 0
    for seed in KILL_SEEDS:
        async with _rig(
            files_session,
            await files_org_factory(),
            clock,
            tmp_path / f"seed{seed}",
            seed,
            sim_engine,
            writers=1,
            faults=False,
        ) as rig:
            sim = Sim(seed, policy=_policy(seed))
            for seat in rig.seats:
                sim.spawn(
                    seat.name,
                    writer(
                        sim, rig, seat, rounds=ROUNDS, rng=random.Random(seed ^ hash(seat.name))
                    ),
                )
            sim.spawn(
                "crasher",
                crasher(
                    sim,
                    rig,
                    names=[s.name for s in rig.seats],
                    rounds=ROUNDS,
                    rng=random.Random(seed),
                ),
            )
            try:
                await asyncio.to_thread(sim.run)
            except BaseException as exc:
                sim.annotate(exc)
                raise
            crashed += rig.outcomes.count("CheckpointKilled")

    assert crashed > 0, f"the crasher never killed a writer across seeds {KILL_SEEDS}"
