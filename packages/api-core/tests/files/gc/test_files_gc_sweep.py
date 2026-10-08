"""The reachability sweep: its roots, its breaker and its two phases.

Every test here seeds real rows through the factories and real objects on a
real ``FilesystemStore`` rooted above the domain prefix, then asserts what is
on disk afterwards — never what a mock was told to return.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import BREAKER_FRACTION, DELETED_WINDOW, Janitor, SweepPlan, SweepResult
from alkera_core.files.store.keys import object_key
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

KEEP = object_key(b"\xaa\xbb\xcc\xdd")
DROP = object_key(b"\x11\x22\x33\x44")

#: Deadlines the janitor reads are Postgres ``now()``, never the fake clock, so
#: a window a test wants open is stated as an instant past every real clock.
FAR_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


def make_janitor(
    repo_for_org: Any,
    domain: Any,
    clock: FakeClock,
    *,
    checkpoints: Any = None,
) -> Janitor:
    return Janitor(
        repo_for_org,
        AdminOnlyFactory(domain.store),
        clock,
        checkpoints,
        age_source=domain.ages,
    )


async def alerts_for(session: AsyncSession, domain_id: object) -> list[str]:
    """The alerts this domain's sweeps recorded — the table outlives one test."""
    rows = await session.execute(
        text("SELECT payload ->> 'alert' FROM event_outbox WHERE entity_id = :id ORDER BY id"),
        {"id": str(domain_id)},
    )
    return [row[0] for row in rows if row[0] is not None]


# -- roots ---------------------------------------------------------------


@pytest.mark.parametrize(
    "root",
    [
        pytest.param("open_session", id="root-open-upload-session"),
        pytest.param("grant", id="root-in-flight-download-grant"),
        pytest.param("held", id="root-held-version"),
        pytest.param("trashed", id="root-trashed-not-yet-purged"),
    ],
)
async def test_each_root_keeps_its_bytes_and_its_window_releases_exactly_it(
    root: str,
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    files_session: AsyncSession,
    clock: FakeClock,
    shard: int,
) -> None:
    """Each root survives a sweep; the clock past its window frees it.

    The negative half is the point: after the window the *same* object is
    moved, so the test cannot pass by the sweep simply never moving anything.
    """
    seeder, tree = seeded
    node = tree["drop.bin"]
    # A second object with no root at all, so "the sweep did move things" is
    # visible in the same run as "it did not move the rooted one".
    domain.write(KEEP, b"unrooted")
    domain.ages.set(domain.absolute(KEEP), clock.now() - timedelta(days=3650))

    if root == "open_session":
        upload = await seeder.upload_session(tree["keep.bin"])
        protected = f"incoming/{upload.id}/object"
        domain.write(protected, b"staged")
        domain.ages.set(domain.absolute(protected), clock.now() - timedelta(days=3650))
    else:
        protected = DROP
        domain.write(protected, b"protected")
        domain.ages.set(domain.absolute(protected), clock.now() - timedelta(days=3650))
        if root == "grant":
            version = await seeder.version(node, protected)
            node.trashed_at = clock.now()
            await files_session.commit()
            await seeder.grant(version, FAR_FUTURE)
        elif root == "held":
            await seeder.version(node, protected, held=True)
            node.trashed_at = clock.now()
            await files_session.commit()
        else:
            await seeder.version(node, protected)
            await seeder.trash(node, purge_after=FAR_FUTURE)

    janitor = make_janitor(repo_for_org, domain, clock)
    first = await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)
    assert isinstance(first, SweepResult)

    assert domain.exists(protected), f"the {root} root was swept while it was still live"
    assert not domain.exists(KEEP), "the unrooted object should have been moved"

    # Move the clock past exactly this root's window and sweep again.
    if root == "open_session":
        await files_session.execute(
            text("UPDATE file_upload_sessions SET state = 'aborted' WHERE id = :id"),
            {"id": upload.id},
        )
    elif root == "grant":
        await files_session.execute(text("UPDATE file_content_grants SET expires_at = now()"))
    elif root == "held":
        await files_session.execute(text("UPDATE file_versions SET held = false"))
    else:
        await files_session.execute(text("UPDATE file_trash_ops SET purge_after = now()"))
    await files_session.commit()

    second = await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)
    assert isinstance(second, SweepResult)
    assert not domain.exists(protected), f"the {root} window passed but its bytes were not released"
    assert domain.exists(f"deleted/{protected}"), "phase one parks the object, it does not erase it"


async def test_an_unrooted_object_written_after_the_horizon_is_kept_by_age(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    clock: FakeClock,
    shard: int,
) -> None:
    """A writer that started after ``sweep_started_at`` owns its object.

    The age rule is the second net behind the session root: a commit landing
    mid-sweep is safe even when no row yet references its bytes.
    """
    domain.write(DROP, b"young")
    domain.ages.set(domain.absolute(DROP), clock.now() + timedelta(days=365))
    domain.write(KEEP, b"old")
    domain.ages.set(domain.absolute(KEEP), clock.now() - timedelta(days=365))

    janitor = make_janitor(repo_for_org, domain, clock)
    result = await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)

    assert isinstance(result, SweepResult)
    assert result.moved == (KEEP,)
    assert domain.exists(DROP), "an object newer than the horizon must never be moved"


async def test_a_commit_landing_while_the_sweep_is_paused_is_never_moved(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    files_session: AsyncSession,
    clock: FakeClock,
    shard: int,
) -> None:
    """Reachability is computed, then a commit lands, then the sweep resumes.

    The object the commit references was written after the stamp, so the age
    rule covers it; the sweep must move nothing at all.
    """
    seeder, tree = seeded
    checkpoints = PausingCheckpoints()
    checkpoints.pause("gc.after_reachability")
    janitor = make_janitor(repo_for_org, domain, clock, checkpoints=checkpoints)

    sweeping = asyncio.create_task(
        janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)
    )
    await checkpoints.wait_paused("gc.after_reachability")

    domain.write(DROP, b"just committed")
    domain.ages.set(domain.absolute(DROP), clock.now() + timedelta(days=3650))
    await seeder.version(tree["drop.bin"], DROP)

    checkpoints.release("gc.after_reachability")
    result = await sweeping

    assert isinstance(result, SweepResult)
    assert result.moved == ()
    assert domain.exists(DROP), "the object a mid-sweep commit referenced was collected"


# -- the breaker ---------------------------------------------------------


@pytest.mark.parametrize(
    ("garbage_bytes", "trips"),
    [
        pytest.param(2_000, False, id="exactly-two-percent-proceeds"),
        pytest.param(2_001, True, id="two-percent-plus-one-byte-aborts"),
    ],
)
async def test_the_breaker_aborts_above_two_percent_of_live_bytes(
    garbage_bytes: int,
    trips: bool,
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    files_session: AsyncSession,
    clock: FakeClock,
    shard: int,
) -> None:
    """The boundary, both sides: 2 % of 100,000 live bytes is 2,000."""
    seeder, tree = seeded
    live = 100_000
    assert live * BREAKER_FRACTION == 2_000  # the boundary this case straddles
    domain.write(KEEP, b"x" * live)
    domain.ages.set(domain.absolute(KEEP), clock.now() - timedelta(days=3650))
    await seeder.version(tree["keep.bin"], KEEP, size=live)

    domain.write(DROP, b"g" * garbage_bytes)
    domain.ages.set(domain.absolute(DROP), clock.now() - timedelta(days=3650))

    janitor = make_janitor(repo_for_org, domain, clock)
    result = await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)

    assert isinstance(result, SweepResult)
    assert result.breaker_tripped is trips
    assert result.aborted is trips
    assert domain.exists(DROP) is trips, "an aborted sweep must move nothing at all"
    assert (await alerts_for(files_session, domain.id) == ["file_gc.breaker"]) is trips


async def test_the_dry_run_plan_is_the_set_the_real_sweep_moves(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    clock: FakeClock,
    shard: int,
) -> None:
    """A dry run predicts the real run exactly, and touches nothing."""
    seeder, tree = seeded
    live = 1_000_000
    domain.write(KEEP, b"x" * live)
    domain.ages.set(domain.absolute(KEEP), clock.now() - timedelta(days=3650))
    await seeder.version(tree["keep.bin"], KEEP, size=live)
    domain.write(DROP, b"garbage")
    domain.ages.set(domain.absolute(DROP), clock.now() - timedelta(days=3650))

    janitor = make_janitor(repo_for_org, domain, clock)
    plan = await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=True)

    assert isinstance(plan, SweepPlan)
    assert domain.exists(DROP), "a dry run must not move a byte"

    result = await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)
    assert isinstance(result, SweepResult)
    assert result.moved == plan.keys == (DROP,)


# -- the second phase ----------------------------------------------------


@pytest.mark.parametrize(
    ("age", "erased"),
    [
        pytest.param(DELETED_WINDOW - timedelta(seconds=1), False, id="a-second-inside-the-window"),
        pytest.param(DELETED_WINDOW, True, id="exactly-the-window"),
    ],
)
async def test_deleted_objects_are_erased_only_past_the_seven_day_window(
    age: timedelta,
    erased: bool,
    domain: Any,
    repo_for_org: Any,
    clock: FakeClock,
) -> None:
    """Phase two is a clock decision, driven across the boundary itself."""
    parked = f"deleted/{DROP}"
    domain.write(parked, b"parked")
    domain.ages.set(domain.absolute(parked), clock.now())
    clock.advance(age)

    janitor = make_janitor(repo_for_org, domain, clock)
    gone = await janitor.expire_deleted(domain.id, clock.now())

    assert (parked in gone) is erased
    assert domain.exists(parked) is not erased
