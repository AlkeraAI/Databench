"""What reaping a lapsed lease has to undo, against real Postgres.

A lease hands one machine the writes on a subtree, and while it holds it the
machine says what it is doing: this file is being written, this one's bytes are
on their way. When the machine dies those statements do not become false
slowly — they become false at the instant the lease lapses, and the plane has
to go with it in the same statement that lapses it. Anything less leaves a
window in which the drive tells a reader a file is on its way from a host
nobody can reach.

What the lapse may NOT take is a file. A lapse proves the holder stopped
beating, not that its copy is gone: a file whose bytes never landed exists on
that machine and nowhere else, so it stays listed for the machine to fill when
it comes back, and only the grace sweep moves it, into the trash under an op
that names the machine.

The rows are planted through ``LeaseService.acquire`` and the real live-entries
service rather than by hand, because the whole claim is that the reap agrees
with the fence: a hand-written lease row would only prove that a SELECT matches
the INSERT beside it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import NodeId
from alkera_core.files.lease_live import LiveEntriesService, LiveReport
from alkera_core.files.leases import LEASE_TTL, LeaseService
from alkera_core.files.repo import FilesRepo
from alkera_core.files.sweepers import REASON_LEFT_ON_MACHINE, LeaseReaper, SweepDeps
from freezegun import freeze_time
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

INSTANCE = "laptop-1"
MACHINE = "ana-mbp"
SECOND = timedelta(seconds=1)
#: The grace the sweep gives a lapsed holder before its unlanded files go to
#: the trash. Pinned here rather than read from settings, so the test crosses a
#: boundary it chose.
GRACE = timedelta(hours=24)


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


class Mount:
    """A leased folder, a file the holder pushed as a name only, and a file
    that already has bytes on the drive."""

    def __init__(self, nodes: dict[str, Any], epoch: int, expires_at: datetime) -> None:
        self.ids = {name: node.id for name, node in nodes.items()}
        self.epoch = epoch
        #: Read off the row rather than computed: the lease deadline is stamped
        #: from Postgres ``now()``, which is the only clock the reap compares
        #: against, so a test that dated it from its own would never line up.
        self.expires_at = expires_at

    @property
    def lapsed(self) -> datetime:
        return self.expires_at + SECOND

    @property
    def still_held(self) -> datetime:
        return self.expires_at - SECOND

    def node(self, name: str) -> NodeId:
        return NodeId(self.ids[name])


async def _mount(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    *,
    ttl: timedelta = LEASE_TTL,
) -> Mount:
    drive = await files_factory.drive()
    nodes = await files_factory.tree("team/ team/skeleton.csv team/landed.md", drive=drive)
    # `landed.md` already has bytes on the drive; `skeleton.csv` is a name the
    # holder pushed ahead of an upload that never arrived.
    await repo.session.execute(
        text(
            "INSERT INTO file_versions (id, org_team_id, node_id, seq, size_bytes, "
            "content_hash, block_hash, source, scan_state, keep_forever, held, "
            "lease_epoch, metadata, created_at) VALUES "
            "(:id, :org, :node, 1, 8, 'h', '', 'upload', 'clean', false, false, 0, "
            "'{}'::jsonb, :at)"
        ),
        {
            "id": (version_id := uuid.uuid4()),
            "org": files_org.org_team_id,
            "node": nodes["team/landed.md"].id,
            "at": EPOCH,
        },
    )
    await repo.session.execute(
        text("UPDATE file_nodes SET head_version_id = :v WHERE id = :n"),
        {"v": version_id, "n": nodes["team/landed.md"].id},
    )
    await repo.session.commit()
    async with repo.transaction():
        grant = await LeaseService(repo, _ctx(files_org), clock).acquire(
            NodeId(nodes["team"].id), instance_id=INSTANCE, machine_id=MACHINE, ttl=ttl
        )
    expires_at = (
        await repo.session.execute(
            text("SELECT expires_at FROM file_leases WHERE node_id = :n"),
            {"n": nodes["team"].id},
        )
    ).scalar_one()
    mount = Mount(nodes, grant.epoch, expires_at)
    async with repo.transaction():
        await LiveEntriesService(repo, _ctx(files_org)).upsert(
            mount.node("team"),
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[
                LiveReport(node_id=mount.node("team/skeleton.csv"), state="uploading"),
                LiveReport(node_id=mount.node("team/landed.md"), state="writing"),
            ],
        )
    return mount


@pytest.fixture
async def mount(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
) -> Mount:
    return await _mount(FilesRepo(files_session, files_org.scope), files_factory, files_org, clock)


def _deps(files_session: AsyncSession, files_org: FilesOrg) -> SweepDeps:
    return SweepDeps(
        repo=FilesRepo(files_session, files_org.scope), ctx=_ctx(files_org), unsynced_grace=GRACE
    )


async def _live_rows(session: AsyncSession, org: FilesOrg) -> int:
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM file_lease_live_entries WHERE org_team_id = :org"),
                {"org": org.org_team_id},
            )
        ).scalar_one()
    )


async def _trashed(session: AsyncSession, node_id: uuid.UUID) -> datetime | None:
    session.expire_all()
    return (
        await session.execute(
            text("SELECT trashed_at FROM file_nodes WHERE id = :n"), {"n": node_id}
        )
    ).scalar_one()


async def _trash_ops(session: AsyncSession, node_id: uuid.UUID) -> list[tuple[str, str]]:
    """``(reason, machine)`` of every trash op whose root is ``node_id``."""
    rows = await session.execute(
        text("SELECT reason, reason_machine FROM file_trash_ops WHERE root_node_id = :n"),
        {"n": node_id},
    )
    return [(str(row.reason), str(row.reason_machine)) for row in rows]


async def _reap(session: AsyncSession, org: FilesOrg, at: datetime) -> int:
    outcome = await LeaseReaper(_deps(session, org)).run(at)
    return outcome.swept


async def test_a_reaped_lease_takes_its_in_flight_plane_with_it(
    files_session: AsyncSession, files_org: FilesOrg, clock: FakeClock, mount: Mount
) -> None:
    """The plane is the holder's word about what it is doing. The reap is the
    moment that word stops being worth anything, so the rows go in the same
    statement — never a pass later, when a reader has already been told a dead
    machine is mid-upload."""
    assert await _live_rows(files_session, files_org) == 2

    assert await _reap(files_session, files_org, mount.lapsed) == 1

    assert await _live_rows(files_session, files_org) == 0


async def test_a_lease_still_inside_its_ttl_keeps_its_plane(
    files_session: AsyncSession, files_org: FilesOrg, clock: FakeClock, mount: Mount
) -> None:
    """The other half of the deadline: a sweeper that retracts a second early
    blanks a working agent's folder in front of the person watching it."""
    assert await _reap(files_session, files_org, mount.still_held) == 0

    assert await _live_rows(files_session, files_org) == 2
    assert await _trashed(files_session, mount.ids["team/skeleton.csv"]) is None


async def test_a_heartbeat_that_lands_first_saves_the_plane_too(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
) -> None:
    """The reap is one compare-and-swap and every new clause rides inside it.

    A sweeper reads a row, decides it has lapsed, and by the time its statement
    runs the holder has beaten. The `WHERE` no longer matches, so the lease
    survives — and because the retraction and the trashing hang off `reaped`,
    so does everything they would have taken. A statement that retracted the
    plane outside the CAS would blank a working agent's folder here.
    """
    repo = FilesRepo(files_session, files_org.scope)
    mount = await _mount(repo, files_factory, files_org, clock, ttl=SECOND)
    at = mount.lapsed
    async with repo.transaction():
        beaten = await LeaseService(repo, _ctx(files_org), clock).heartbeat(
            mount.node("team"), epoch=mount.epoch, instance_id=INSTANCE
        )
    assert beaten.expires_at > at

    assert await _reap(files_session, files_org, at) == 0

    assert await _live_rows(files_session, files_org) == 2
    assert await _trashed(files_session, mount.ids["team/skeleton.csv"]) is None


async def test_a_node_the_holder_named_but_never_filled_stays_listed_for_its_holder(
    files_session: AsyncSession, files_org: FilesOrg, clock: FakeClock, mount: Mount
) -> None:
    """A skeleton's bytes exist only on the machine that lost the lease, and
    the drive cannot tell a dead machine from one an outage cut off. Hiding
    the row at the lapse hid the only record that the file exists, with no
    trash entry to bring it back from."""
    with freeze_time(mount.still_held, real_asyncio=True) as frozen:
        assert await _reap(files_session, files_org, datetime.now(UTC)) == 0
        frozen.move_to(mount.lapsed)
        assert await _reap(files_session, files_org, datetime.now(UTC)) == 1

    assert await _trashed(files_session, mount.ids["team/skeleton.csv"]) is None
    assert await _trash_ops(files_session, mount.ids["team/skeleton.csv"]) == []


async def test_a_node_that_already_has_bytes_survives_its_lease(
    files_session: AsyncSession, files_org: FilesOrg, clock: FakeClock, mount: Mount
) -> None:
    """This node was mid-overwrite when the machine died; the overwrite is
    lost, but the last version that landed is still the right answer and
    trashing it would destroy a file the drive has."""
    await _reap(files_session, files_org, mount.lapsed)

    assert await _trashed(files_session, mount.ids["team/landed.md"]) is None


async def test_past_the_grace_the_unfilled_node_goes_to_the_trash_under_an_op(
    files_session: AsyncSession, files_org: FilesOrg, clock: FakeClock, mount: Mount
) -> None:
    """The one way a holder's unlanded file leaves the listing: a day after
    the lapse, into the trash, under an op that says why and names the
    machine, so the trash lists it and a restore brings it back. One second
    inside the grace it is still where the holder left it."""
    with freeze_time(mount.lapsed, real_asyncio=True) as frozen:
        await _reap(files_session, files_org, datetime.now(UTC))
        frozen.move_to(mount.lapsed + GRACE - SECOND)
        await _reap(files_session, files_org, datetime.now(UTC))
        assert await _trashed(files_session, mount.ids["team/skeleton.csv"]) is None

        frozen.move_to(mount.lapsed + GRACE + SECOND)
        await _reap(files_session, files_org, datetime.now(UTC))

    assert await _trashed(files_session, mount.ids["team/skeleton.csv"]) is not None
    assert await _trash_ops(files_session, mount.ids["team/skeleton.csv"]) == [
        (REASON_LEFT_ON_MACHINE, MACHINE)
    ]
    assert await _trashed(files_session, mount.ids["team/landed.md"]) is None


async def test_a_second_reap_over_the_same_state_changes_nothing(
    files_session: AsyncSession, files_org: FilesOrg, clock: FakeClock, mount: Mount
) -> None:
    """Idempotence, which is what lets the schedule re-run a pass that died
    half-way: the lease is reaped once, and no pass files a trash for a node
    whose holder may still come back."""
    at = mount.lapsed
    assert await _reap(files_session, files_org, at) == 1

    assert await _reap(files_session, files_org, at) == 0

    rows = (
        await files_session.execute(
            text("SELECT count(*) FROM file_history WHERE node_id = :n AND kind = 'trash'"),
            {"n": mount.ids["team/skeleton.csv"]},
        )
    ).scalar_one()
    assert int(rows) == 0
    assert await _trashed(files_session, mount.ids["team/skeleton.csv"]) is None


async def test_another_org_keeps_its_plane_when_this_one_is_reaped(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_org_factory: Any,
    files_factory: FilesFactory,
    clock: FakeClock,
    mount: Mount,
) -> None:
    """Every clause of the reap is org-scoped; a cross-tenant DELETE here would
    empty a stranger's mount from a sweeper pass they never asked for."""
    other = await files_org_factory()
    other_repo = FilesRepo(files_session, other.scope)
    other_factory = FilesFactory(files_session, other)
    await _mount(other_repo, other_factory, other, clock)
    before = int(
        (
            await files_session.execute(
                text("SELECT count(*) FROM file_lease_live_entries WHERE org_team_id = :org"),
                {"org": other.org_team_id},
            )
        ).scalar_one()
    )
    assert before == 2

    await _reap(files_session, files_org, mount.lapsed)

    assert (
        int(
            (
                await files_session.execute(
                    text("SELECT count(*) FROM file_lease_live_entries WHERE org_team_id = :org"),
                    {"org": other.org_team_id},
                )
            ).scalar_one()
        )
        == 2
    )
