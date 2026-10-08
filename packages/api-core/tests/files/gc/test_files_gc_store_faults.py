"""What a sweep does when the object store fails underneath it.

The crash tests kill the *process*; these kill the *store*. A scheduled
``FaultyStore`` fault lands on one real ``move`` — the driver underneath is a
genuine ``FilesystemStore``, so "the object is still where it was" is read off
the disk, never off a recorder — and the sweep must leave the shard exactly as
recoverable as it found it: the cursor unadvanced, the object at its original
key, and the same key retried by the next sweep once the fault clears.

The budget cases are the same shape from the other side: a collection that
stops at the candidate which would cross ``budget_bytes`` must leave the rest
behind the cursor for the next sweep, and is pinned by the objects that ended
up under ``deleted/`` rather than by how many times anything was called.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import DELETED_WINDOW, Janitor, SweepResult
from alkera_core.files.store.errors import Unavailable
from alkera_core.files.store.keys import object_key
from alkera_test_support.files.faulty_store import Fault, FaultSchedule, FaultyStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

#: Two unrooted objects, named so their sweep order is the sorted order below.
ONE = object_key(b"\x01\x01\x01\x01")
TWO = object_key(b"\x02\x02\x02\x02")
FIRST, SECOND = sorted((ONE, TWO))

#: Each object's payload, sized so a budget can admit exactly one of them.
PAYLOAD = b"x" * 10
ANCIENT = timedelta(days=3650)


def make_janitor(
    repo_for_org: Any,
    domain: Any,
    clock: FakeClock,
    store: Any,
    *,
    checkpoints: Any = None,
) -> Janitor:
    return Janitor(
        repo_for_org,
        AdminOnlyFactory(store),
        clock,
        checkpoints,
        age_source=domain.ages,
    )


def seed_two_objects(domain: Any, clock: FakeClock) -> None:
    """Two collectable objects, aged well behind any sweep horizon."""
    for key in (FIRST, SECOND):
        domain.write(key, PAYLOAD)
        domain.ages.set(domain.absolute(key), clock.now() - ANCIENT)


async def cursor_of(session: AsyncSession, shard: int) -> str | None:
    """The shard's resume point, read the way a second sweeper would."""
    row = (
        await session.execute(
            text("SELECT cursor ->> 'after' FROM file_sweep_shards WHERE shard = :shard"),
            {"shard": shard},
        )
    ).first()
    return None if row is None else row[0]


# -- a fault mid-move ----------------------------------------------------


async def test_a_store_fault_mid_move_leaves_the_cursor_unadvanced_and_retries_the_same_key(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    files_session: AsyncSession,
    clock: FakeClock,
    shard: int,
) -> None:
    """A failed move is reported, and costs the shard no progress.

    The retry half is what makes this more than "an exception escaped": the
    next sweep must move the *same* key, which it can only do because the
    cursor never claimed it was done.
    """
    seed_two_objects(domain, clock)
    store = FaultyStore(
        domain.store,
        FaultSchedule(faults=(Fault(kind="read_only", key=domain.absolute(FIRST), count=1),)),
    )
    janitor = make_janitor(repo_for_org, domain, clock, store)

    with pytest.raises(Unavailable):
        await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)

    assert await cursor_of(files_session, shard) is None, (
        "a move that failed must not leave a cursor claiming the key was done"
    )
    assert domain.exists(FIRST), "the failed move must leave the object at its original key"
    assert not domain.exists(f"deleted/{FIRST}"), "nothing was parked, so deleted/ must be empty"
    assert store.unfired() == [], "the scheduled fault never landed on the move"

    # The fault is spent; the retry is the same janitor on the same shard.
    result = await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)

    assert isinstance(result, SweepResult)
    assert result.moved == (FIRST, SECOND), "the retry must re-attempt the key the fault ate"
    assert domain.exists(f"deleted/{FIRST}") and domain.exists(f"deleted/{SECOND}")
    assert await cursor_of(files_session, shard) is None, "a finished sweep clears its cursor"


async def test_a_fault_on_the_second_move_keeps_the_first_key_behind_the_cursor(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    files_session: AsyncSession,
    clock: FakeClock,
    shard: int,
) -> None:
    """The cursor advances exactly as far as the moves that actually landed."""
    seed_two_objects(domain, clock)
    store = FaultyStore(
        domain.store,
        FaultSchedule(faults=(Fault(kind="read_only", key=domain.absolute(SECOND), count=1),)),
    )
    janitor = make_janitor(repo_for_org, domain, clock, store)

    with pytest.raises(Unavailable):
        await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)

    assert await cursor_of(files_session, shard) == FIRST, (
        "the cursor must name the last key whose move committed, and no further"
    )
    assert domain.exists(f"deleted/{FIRST}"), "the move that landed must stay landed"
    assert domain.exists(SECOND), "the move that failed must leave its object untouched"

    result = await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)

    assert isinstance(result, SweepResult)
    assert result.moved == (SECOND,), "a resumed sweep re-walks from the cursor, not from zero"
    assert domain.exists(f"deleted/{SECOND}")


async def test_a_referenced_object_is_at_exactly_one_key_at_every_step_of_a_move(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    clock: FakeClock,
    shard: int,
) -> None:
    """Nothing invalidates a key before the bytes are at the new one.

    The sweep is parked either side of the single ``move`` and the disk is read
    from the test's own task: an implementation that erased, unlinked or
    "invalidated" the source before writing ``deleted/`` would show a moment
    with the object at neither key, and this is the assertion that sees it.
    """
    domain.write(FIRST, PAYLOAD)
    domain.ages.set(domain.absolute(FIRST), clock.now() - ANCIENT)
    checkpoints = PausingCheckpoints()
    checkpoints.pause("gc.before_move")
    checkpoints.pause("gc.after_move")
    janitor = make_janitor(repo_for_org, domain, clock, domain.store, checkpoints=checkpoints)

    sweeping = asyncio.create_task(
        janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)
    )
    await checkpoints.wait_paused("gc.before_move")
    assert domain.exists(FIRST) and not domain.exists(f"deleted/{FIRST}"), (
        "before the move the object must still be readable at its original key"
    )

    checkpoints.release("gc.before_move")
    await checkpoints.wait_paused("gc.after_move")
    assert domain.exists(f"deleted/{FIRST}") and not domain.exists(FIRST), (
        "after the move the object must be readable at exactly the parked key"
    )

    checkpoints.release("gc.after_move")
    result = await sweeping
    assert isinstance(result, SweepResult)
    assert result.moved == (FIRST,)


async def test_a_fault_erasing_a_parked_object_leaves_it_recoverable_under_deleted(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    clock: FakeClock,
    shard: int,
) -> None:
    """Phase two is the same rule: a failed erase must not lose the bytes."""
    domain.write(FIRST, PAYLOAD)
    domain.ages.set(domain.absolute(FIRST), clock.now() - ANCIENT)
    plain = make_janitor(repo_for_org, domain, clock, domain.store)
    await plain.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)
    parked = f"deleted/{FIRST}"
    assert domain.exists(parked)
    domain.ages.set(domain.absolute(parked), clock.now() - DELETED_WINDOW - timedelta(days=1))

    store = FaultyStore(
        domain.store,
        FaultSchedule(faults=(Fault(kind="read_only", key_prefix=domain.absolute("deleted/")),)),
    )
    faulty = make_janitor(repo_for_org, domain, clock, store)

    with pytest.raises(Unavailable):
        await faulty.expire_deleted(domain.id, clock.now())

    assert domain.exists(parked), "an erase that failed must leave the object recoverable"

    erased = await plain.expire_deleted(domain.id, clock.now())
    assert erased == (parked,), "once the fault clears the same object is erased"
    assert not domain.exists(parked)


# -- the byte budget -----------------------------------------------------


async def test_the_budget_ends_the_collection_at_the_candidate_that_would_cross_it(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    files_session: AsyncSession,
    clock: FakeClock,
    shard: int,
) -> None:
    """A budget of one object's bytes moves one object, and only one.

    Pinned by what ended up under ``deleted/`` — a budget that merely limited
    how many times something was called would pass a call-count assertion and
    fail this one.
    """
    seed_two_objects(domain, clock)
    janitor = make_janitor(repo_for_org, domain, clock, domain.store)

    first = await janitor.sweep(
        domain.id,
        org=files_org.scope,
        shard=shard,
        dry_run=False,
        budget_bytes=len(PAYLOAD),
    )

    assert isinstance(first, SweepResult)
    assert first.moved == (FIRST,), "the candidate that would cross the budget must be left"
    assert domain.exists(SECOND), "the object past the budget must still be at its own key"
    assert await cursor_of(files_session, shard) == FIRST

    second = await janitor.sweep(
        domain.id,
        org=files_org.scope,
        shard=shard,
        dry_run=False,
        budget_bytes=len(PAYLOAD),
    )

    assert isinstance(second, SweepResult)
    assert second.moved == (SECOND,), "the next sweep must resume from the cursor, not re-walk"
    assert domain.exists(f"deleted/{FIRST}") and domain.exists(f"deleted/{SECOND}")


async def test_a_budget_wider_than_the_domain_collects_everything_in_one_sweep(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    clock: FakeClock,
    shard: int,
) -> None:
    """The negative half: the budget is a limit, never a per-sweep quota of one."""
    seed_two_objects(domain, clock)
    janitor = make_janitor(repo_for_org, domain, clock, domain.store)

    result = await janitor.sweep(
        domain.id,
        org=files_org.scope,
        shard=shard,
        dry_run=False,
        budget_bytes=len(PAYLOAD) * 2,
    )

    assert isinstance(result, SweepResult)
    assert result.moved == (FIRST, SECOND)
