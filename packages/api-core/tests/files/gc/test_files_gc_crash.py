"""A killed sweep: nothing referenced goes missing, and the next run resumes.

The kill is the checkpoint harness raising `CheckpointKilled` — a
`BaseException` — exactly where a SIGKILL would land, so no `except Exception`
inside the janitor can soften it into a tidy shutdown.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.files.checkpoints import CheckpointKilled, PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import Janitor, SweepResult
from alkera_core.files.store.keys import object_key
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

LIVE = object_key(b"\xaa\xaa\xaa\xaa")
FIRST = object_key(b"\x11\x11\x11\x11")
SECOND = object_key(b"\xee\xee\xee\xee")
ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)


def store_key(listed: str) -> str:
    """A listing entry as a store key: whatever the platform separates with, as ``/``.

    ``Domain.listing`` reports what the filesystem holds, so on Windows a parked
    object comes back as ``deleted\\objects\\...``. A key that keeps the native
    separator strips no ``deleted/`` prefix and matches no store key, which turns
    "parked, and therefore gone from its old place" into "parked and still in
    place" — a false red for the invariant, on the one platform the invariant is
    not actually about.
    """
    return listed.replace(os.sep, "/")


def janitor_for(repo_for_org: Any, domain: Any, clock: FakeClock, cp: Any) -> Janitor:
    return Janitor(repo_for_org, AdminOnlyFactory(domain.store), clock, cp, age_source=domain.ages)


async def shard_row(session: AsyncSession, shard: int) -> tuple[Any, Any]:
    row = (
        await session.execute(
            text(
                "SELECT cursor ->> 'after', sweep_started_at "
                "FROM file_sweep_shards WHERE shard = :s"
            ),
            {"s": shard},
        )
    ).one()
    return row[0], row[1]


@pytest.fixture
def three_objects(domain: Any) -> None:
    """One live object and two pieces of garbage, all older than any horizon."""
    for key in (LIVE, FIRST, SECOND):
        domain.write(key, b"payload")
        domain.ages.set(domain.absolute(key), ANCIENT)


@pytest.mark.parametrize(
    "where",
    [
        pytest.param("gc.before_move", id="killed-before-the-first-move"),
        pytest.param("gc.after_move", id="killed-after-the-first-move"),
    ],
)
async def test_a_kill_around_a_move_never_loses_a_referenced_object(
    where: str,
    three_objects: None,
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    clock: FakeClock,
    shard: int,
) -> None:
    """The live object's bytes are readable at every kill point.

    Both halves of "invalidate before move" are asserted from outside: the
    referenced object is where its version row says it is, and whatever the
    sweep did move is parked under ``deleted/`` rather than gone.
    """
    seeder, tree = seeded
    await seeder.version(tree["keep.bin"], LIVE)

    checkpoints = PausingCheckpoints()
    checkpoints.kill(where)
    janitor = janitor_for(repo_for_org, domain, clock, checkpoints)

    with pytest.raises(CheckpointKilled):
        await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)

    assert domain.exists(LIVE), "a referenced object went missing across a crash"
    moved = domain.listing("deleted/")
    parked = {store_key(key).removeprefix("deleted/") for key in moved}
    assert LIVE not in parked
    for key in parked:
        assert not domain.exists(key), "an object is both parked and in place"


async def test_the_resumed_sweep_starts_from_the_cursor_the_killed_run_wrote(
    three_objects: None,
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    files_session: AsyncSession,
    clock: FakeClock,
    shard: int,
) -> None:
    """A sweep is resumed, never restarted: the horizon and the cursor persist.

    The decoy is the teeth: an unreachable object *behind* the cursor must be
    left where it is by the resumed run. A run that ignored the cursor and
    re-listed from the top would move it, and this test would fail.
    """
    seeder, tree = seeded
    await seeder.version(tree["keep.bin"], LIVE)

    # Kill at the second move, so the first move's cursor is on the row.
    killer = PausingCheckpoints()
    killer.kill("gc.before_move")
    first_pass = janitor_for(repo_for_org, domain, clock, killer)
    with pytest.raises(CheckpointKilled):
        await first_pass.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)

    # Nothing moved yet, so the run is resumable from a hand-set cursor that
    # stands for "the first candidate was already dealt with".
    await files_session.execute(
        text(
            "UPDATE file_sweep_shards "
            "SET cursor = jsonb_build_object('after', CAST(:after AS text)) WHERE shard = :s"
        ),
        {"after": SECOND, "s": shard},
    )
    await files_session.commit()
    cursor, started = await shard_row(files_session, shard)
    assert cursor == SECOND
    assert started is not None, "a killed sweep must keep its horizon, not restart"

    resumed = janitor_for(repo_for_org, domain, clock, PausingCheckpoints())
    result = await resumed.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)

    assert isinstance(result, SweepResult)
    assert FIRST not in result.moved, "the resumed sweep re-walked ground behind its cursor"
    assert domain.exists(FIRST)
    assert result.plan.started_at == started, "the horizon was restamped on resume"

    finished_cursor, finished_started = await shard_row(files_session, shard)
    assert finished_started is None, "a finished sweep must clear its horizon"
    assert finished_cursor is None


async def test_a_second_sweep_after_a_clean_run_starts_from_a_fresh_horizon(
    three_objects: None,
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    files_session: AsyncSession,
    clock: FakeClock,
    shard: int,
) -> None:
    """The negative twin of the resume rule: a completed sweep does not resume."""
    seeder, tree = seeded
    await seeder.version(tree["keep.bin"], LIVE)
    janitor = janitor_for(repo_for_org, domain, clock, PausingCheckpoints())

    first = await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)
    assert isinstance(first, SweepResult)
    assert set(first.moved) == {FIRST, SECOND}

    domain.write(SECOND, b"written again")
    domain.ages.set(domain.absolute(SECOND), ANCIENT)
    second = await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)

    assert isinstance(second, SweepResult)
    assert second.moved == (SECOND,)
    assert second.plan.started_at > first.plan.started_at + timedelta(microseconds=-1)


@pytest.mark.parametrize(
    ("separator", "listed"),
    [
        pytest.param("\\", "deleted\\objects\\aa\\bb\\beef", id="windows"),
        pytest.param("/", "deleted/objects/aa/bb/beef", id="posix"),
    ],
)
async def test_a_listed_path_reads_as_a_store_key_on_either_separator(
    separator: str, listed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows branch, driven from a POSIX host by pinning ``os.sep``."""
    monkeypatch.setattr(os, "sep", separator)

    assert store_key(listed) == "deleted/objects/aa/bb/beef"
    assert store_key(listed).removeprefix("deleted/") == "objects/aa/bb/beef"
