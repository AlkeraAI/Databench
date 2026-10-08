"""The move storm: six movers re-parenting a 500-node tree.

Every mover issues real ``Namespace.move`` calls with the node's current etag,
on its own session, against this run's Postgres — so the cycle guard, the name
clash and the subtree path rewrite are the product's, not a model's. The
scheduler pauses actors at random (and, on a third of the seeds, parks the
busiest one for a stretch) so a mover's decision is routinely stale by the time
its statement runs.

After every run the tree must still be a tree: acyclic, every ``path_ids``
equal to the parent walk, every live sibling name unique. One seed is one case,
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
    assert_tree_consistent,
    make_seat,
    mover,
    random_schedule,
)
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import Janitor
from alkera_core.files.ids import DomainId, DriveId, NodeId, OrgScope
from alkera_core.files.repo import FilesRepo
from scheduler import PauseLongestPolicy, RandomPolicy, Sim
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

# Each case builds its own rig on the shared engine and seeds its own tenant, so
# nothing one seed leaves behind is visible to the next.
pytestmark = [pytest.mark.spread]

MOVERS = 6
"""The lane's shape: six movers, no writers — structure is what is under test."""

NODES = 500
"""The tree they fight over."""

ROUNDS = 4
"""Moves per mover per seed."""

#: The tree the guard case fights over. Small on purpose: a mover picks its
#: source and its new parent uniformly, so on a 500-node tree it asks for a move
#: into its own subtree only now and then — fewer than half of the window's
#: seeds see a single refusal — while on a handful of nodes it asks constantly.
GUARD_NODES: Final = 8

#: Two fixed seeds for that case. Which seeds they are does not matter: the tree
#: above is what makes a mover reach for a cycle, not the interleaving.
GUARD_SEEDS: Final = (1, 3)


def _files_kit() -> Any:
    """The suite's factories, loaded by path (three apps own a ``tests.files``)."""
    source = Path(__file__).resolve().parent.parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("files_sim_conftest_kit", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FilesFactory = _files_kit().FilesFactory
FilesOrg = _files_kit().FilesOrg


def _tree_spec(rng: random.Random, count: int) -> list[str]:
    """A random hierarchy of ``count`` folders, parents always before children."""
    paths = ["n0"]
    for index in range(1, count):
        parent = rng.choice(paths)
        paths.append(f"{parent}/n{index}")
    return paths


@asynccontextmanager
async def _rig(
    files_session: AsyncSession,
    files_org: FilesOrg,
    clock: FakeClock,
    tmp_path: Path,
    seed: int,
    engine: AsyncEngine,
    *,
    nodes: int = NODES,
) -> AsyncIterator[SimRig]:
    """A folder drive and one private session per mover, closed on the way out.

    Every session the rig opens is closed when the case ends — including when the
    build itself fails halfway — because the cases that run after this one on the
    same worker borrow their connections from the same pool.
    """
    rng = random.Random(seed)
    files_factory = FilesFactory(files_session, files_org)
    drive = await files_factory.drive()
    paths = _tree_spec(rng, nodes)
    tree = await files_factory.tree(" ".join(f"{path}/" for path in paths), drive=drive)
    scope = OrgScope(org_team_id=files_org.org_team_id)
    domain_id = DomainId(drive.dedup_domain_id)
    bucket_root = tmp_path / "bucket"

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
        for index in range(MOVERS):
            seat = await make_seat(
                engine, scope, domain_id, bucket_root, clock, f"m{index}", random_schedule(rng)
            )
            stack.push_async_callback(seat.session.close)
            seats.append(seat)
        assert drive.root_node_id is not None
        yield SimRig(
            bridge=Bridge(asyncio.get_running_loop()),
            clock=clock,
            scope=scope,
            domain_id=domain_id,
            drive_id=DriveId(drive.id),
            bucket_root=bucket_root,
            janitor=janitor,
            janitor_repo=janitor_repo,
            janitor_session=janitor_session,
            shard=uuid.uuid4().int % 1_000_000 + 1_000,
            seats=seats,
            folders=[NodeId(drive.root_node_id), *(NodeId(tree[path].id) for path in paths)],
        )


async def _storm(rig: SimRig, seed: int) -> None:
    """Run one storm to completion, annotating whatever it raises with the trace."""
    sim = Sim(seed, policy=RandomPolicy() if seed % 3 else PauseLongestPolicy(rounds=5))
    for seat in rig.seats:
        sim.spawn(
            seat.name,
            mover(sim, rig, seat, rounds=ROUNDS, rng=random.Random(seed ^ hash(seat.name))),
        )
    try:
        await asyncio.to_thread(sim.run)
        await assert_tree_consistent(rig.janitor_session, rig.drive_id)
    except BaseException as exc:
        sim.annotate(exc)
        raise


async def test_a_move_storm_leaves_a_tree(
    files_session: AsyncSession,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    clock: FakeClock,
    tmp_path: Path,
    sim_seed: int,
    sim_engine: AsyncEngine,
) -> None:
    """This seed ends acyclic, with `path_ids` equal to the parent walk."""
    async with _rig(
        files_session, await files_org_factory(), clock, tmp_path, sim_seed, sim_engine
    ) as rig:
        await _storm(rig, sim_seed)
        # A storm nobody survives is vacuous: "still a tree" is only evidence if
        # the movers really re-parented nodes in it.
        assert rig.outcomes.count("moved") > 0, f"seed {sim_seed}: no move ever landed"


async def test_the_storm_loses_moves_to_the_guards(
    files_session: AsyncSession,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    clock: FakeClock,
    tmp_path: Path,
    sim_engine: AsyncEngine,
) -> None:
    """A storm nobody loses is vacuous too: moves the service must refuse are refused.

    The window's cases cannot carry this claim. A mover on a 500-node tree asks
    for a move into its own subtree — or for the root — rarely enough that most
    seeds land all twenty-four of their moves, so a seed that refuses nothing is
    the normal case rather than a broken one. On the handful of folders below,
    every seed asks.
    """
    refused = 0
    moved = 0
    for seed in GUARD_SEEDS:
        async with _rig(
            files_session,
            await files_org_factory(),
            clock,
            tmp_path / f"seed{seed}",
            seed,
            sim_engine,
            nodes=GUARD_NODES,
        ) as rig:
            await _storm(rig, seed)
            moved += rig.outcomes.count("moved")
            refused += len(rig.outcomes) - rig.outcomes.count("moved")

    assert moved > 0, f"no move landed across seeds {GUARD_SEEDS}"
    assert refused > 0, f"no move was refused across seeds {GUARD_SEEDS}"
