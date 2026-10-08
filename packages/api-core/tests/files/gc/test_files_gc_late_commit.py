"""A commit that lands after the scan and before the move is not swept.

The reachability pass protects a commit still in flight only while its bytes
sit under ``incoming/<session>/``. One store call later the same bytes live
under ``objects/<hash>``, which no root from that scan names — so between the
scan and the move there is a window in which a perfectly legal commit produces
a version row pointing at an object the sweep is already holding a candidate
for. Both halves of the fix are exercised here against a real
``FilesystemStore`` and real rows: the age horizon must read the *publish*
time, and the move must re-check the references born since the horizon.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.files import gc as gc_module
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import Janitor, SweepResult
from alkera_core.files.store.keys import object_key
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

#: The object a late commit claims, and one that nothing ever references.
LATE = object_key(b"\x5a\x5a\x5a\x5a")
GARBAGE = object_key(b"\x0f\x0f\x0f\x0f")

LATE_BYTES = b"the bytes a version is about to name"

#: A purge deadline every real clock is already past.
PAST = datetime(2000, 1, 1, tzinfo=UTC)


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


async def test_a_version_committed_between_the_scan_and_the_move_survives(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    clock: FakeClock,
    shard: int,
) -> None:
    """Invalidate before move — the sweep re-checks and lets go.

    The second object is the control: it is unreferenced for the whole sweep,
    so a run that moved nothing at all cannot pass this test.
    """
    seeder, tree = seeded
    domain.write(LATE, LATE_BYTES)
    domain.ages.set(domain.absolute(LATE), clock.now() - timedelta(days=3650))
    domain.write(GARBAGE, b"nobody's bytes")
    domain.ages.set(domain.absolute(GARBAGE), clock.now() - timedelta(days=3650))

    checkpoints = PausingCheckpoints()
    checkpoints.pause("gc.after_reachability")
    janitor = make_janitor(repo_for_org, domain, clock, checkpoints=checkpoints)
    sweeping = asyncio.create_task(
        janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)
    )
    try:
        await checkpoints.wait_paused("gc.after_reachability")
        # The commit the scan could not have seen: it lands while the sweep
        # holds a reach set that predates it.
        await seeder.version(tree["drop.bin"], LATE)
    finally:
        checkpoints.release("gc.after_reachability")
    result = await sweeping

    assert isinstance(result, SweepResult)
    assert not domain.exists(f"deleted/{LATE}"), (
        "the just-committed object was swept: its version row now dangles"
    )
    assert domain.exists(LATE)
    assert (domain.root / "domains" / str(domain.id) / LATE).read_bytes() == LATE_BYTES
    assert result.moved == (GARBAGE,), "the sweep stopped sweeping altogether"
    assert result.skipped == (LATE,), "the late commit's bytes were not taken back"
    assert result.skipped_count == 1


async def test_an_object_published_after_the_horizon_reads_as_freshly_written(
    domain: Any,
    clock: FakeClock,
) -> None:
    """The other half: a rename must not carry the staging file's age.

    The store publishes ``incoming/`` bytes onto their content key with
    ``move``. If the destination inherited the staging mtime, an object
    published *after* a sweep stamped its horizon would read as older than that
    horizon and be collected by age.
    """
    staged = "incoming/00000000000000000000000000000000/object"
    domain.write(staged, LATE_BYTES)
    staged_at = await domain.ages.written_at(domain.absolute(staged))
    assert staged_at is not None

    horizon = await domain.ages.written_at(domain.absolute(staged))
    assert horizon is not None
    await asyncio.sleep(0.01)
    await domain.store.move(domain.absolute(staged), domain.absolute(LATE))

    published_at = await domain.ages.written_at(domain.absolute(LATE))
    assert published_at is not None
    assert published_at > horizon, "the published object still reads at its staging time"


async def test_a_reference_older_than_the_horizon_does_not_save_the_bytes(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    files_session: AsyncSession,
    clock: FakeClock,
    shard: int,
) -> None:
    """The asymmetric case: the re-check only counts references born since.

    A version row the reachability pass deliberately excluded — a trashed node
    past its purge deadline — is older than the horizon, so the re-check must
    not resurrect it. Without this the trash window would never release a byte.
    """
    seeder, tree = seeded
    node = tree["drop.bin"]
    domain.write(LATE, LATE_BYTES)
    domain.ages.set(domain.absolute(LATE), clock.now() - timedelta(days=3650))
    await seeder.version(node, LATE)
    await seeder.trash(node, purge_after=PAST)

    janitor = make_janitor(repo_for_org, domain, clock)
    result = await janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)

    assert isinstance(result, SweepResult)
    assert result.skipped == ()
    assert result.moved == (LATE,)
    assert domain.exists(f"deleted/{LATE}")


class ParkOnNth:
    """Pause the code the nth time it reaches one checkpoint name.

    `PausingCheckpoints` arms a name once; a sweep reaches `gc.before_move`
    once per candidate, and the interesting park is a specific one of those.
    """

    def __init__(self, name: str, nth: int, timeout: float = 5.0) -> None:
        self._name = name
        self._nth = nth
        self._timeout = timeout
        self._seen = 0
        self.arrived = asyncio.Event()
        self._go = asyncio.Event()

    def release(self) -> None:
        self._go.set()

    async def reach(self, name: str) -> None:
        if name != self._name:
            return
        self._seen += 1
        if self._seen != self._nth:
            return
        self.arrived.set()
        await asyncio.wait_for(self._go.wait(), self._timeout)

    def reach_sync(self, name: str) -> None:
        return None

    def as_hook(self) -> Any:
        return self.reach_sync


async def test_a_commit_landing_while_a_later_page_waits_is_re_checked(
    seeded: Any,
    domain: Any,
    repo_for_org: Any,
    files_org: Any,
    clock: FakeClock,
    shard: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-check is per page, so a batch spanning pages closes its window.

    Four unreferenced objects, a listing page of two: the sweep collects two
    pages. It is parked at ``gc.before_move`` on the last candidate of the
    first page, and a commit lands there naming the last candidate of the whole
    batch — a key on the *second* page. A re-check taken only once, before the
    first move, predates that commit and sweeps the bytes out from under the
    version row; a re-check taken per page sees it and lets the bytes go.
    """
    monkeypatch.setattr(gc_module, "LIST_PAGE", 2)
    seeder, tree = seeded

    keys = sorted(object_key(bytes([n]) * 4) for n in (0x11, 0x22, 0x33, 0x44))
    for key in keys:
        domain.write(key, b"unreferenced " + key.encode())
        domain.ages.set(domain.absolute(key), clock.now() - timedelta(days=3650))
    last = keys[-1]

    checkpoints = ParkOnNth("gc.before_move", 2)
    janitor = make_janitor(repo_for_org, domain, clock, checkpoints=checkpoints)
    sweeping = asyncio.create_task(
        janitor.sweep(domain.id, org=files_org.scope, shard=shard, dry_run=False)
    )
    try:
        await asyncio.wait_for(checkpoints.arrived.wait(), 5.0)
        await seeder.version(tree["drop.bin"], last)
    finally:
        checkpoints.release()
    result = await sweeping

    assert isinstance(result, SweepResult)
    assert result.skipped == (last,), "the commit on the second page was not re-checked"
    assert result.moved == tuple(keys[:-1])
    assert domain.exists(last)
    assert not domain.exists(f"deleted/{last}")
