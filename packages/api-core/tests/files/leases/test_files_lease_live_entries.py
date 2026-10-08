"""The in-flight plane under a live lease, against real Postgres.

A leased folder's drive copy is the last checkpoint; between two checkpoints the
machine holding the lease is the only place the current bytes exist. These tests
drive the service that makes that gap visible — through real lease rows a real
``LeaseService.acquire`` wrote, because the whole point of the plane is that it
agrees with the fence, and a hand-inserted lease row would prove only that a
SELECT matches the INSERT written beside it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.events.types import EventType
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import NodeId
from alkera_core.files.lease_live import (
    InboundEntry,
    LiveEntriesService,
    LiveReport,
)
from alkera_core.files.lease_snapshots import lease_facet, lease_facets
from alkera_core.files.leases import LeaseConflict, LeaseService
from alkera_core.files.repo import FilesRepo
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

INSTANCE = "laptop-1"
MACHINE = "ana-mbp"


@contextmanager
def _counted(engine: AsyncEngine) -> Iterator[list[str]]:
    """Every statement the block issues, in order."""
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        seen.append(statement)

    sync_engine = engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(sync_engine, "before_cursor_execute", record)


def _ctx(org: FilesOrg, principal_id: uuid.UUID | None = None) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(principal_id or org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


class Mount:
    """A drive with ``team/`` leased live and two files under it."""

    def __init__(self, nodes: dict[str, Any], drive: Any, epoch: int) -> None:
        self.nodes = nodes
        self.drive = drive
        self.epoch = epoch
        # Plain ids, captured while the rows are live: a refused call rolls the
        # session back, which expires every ORM instance the factory handed
        # over, and re-reading one from a sync attribute access is an error.
        self.ids = {name: node.id for name, node in nodes.items()}
        self.drive_id = nodes["team"].drive_id

    @property
    def root(self) -> NodeId:
        return NodeId(self.ids["team"])

    def node(self, name: str) -> NodeId:
        return NodeId(self.ids[name])


async def _mount(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    *,
    live: bool = True,
    inbound: bool = False,
) -> Mount:
    drive = await files_factory.drive()
    nodes = await files_factory.tree(
        "team/ team/docs/ team/docs/spec.md team/notes.md outside.md", drive=drive
    )
    async with repo.transaction():
        grant = await LeaseService(repo, _ctx(files_org), clock).acquire(
            NodeId(nodes["team"].id), instance_id=INSTANCE, machine_id=MACHINE
        )
    if live or inbound:
        # The acquire route learns these from the body; the plane only cares
        # that the row says so, so the row is what the test sets.
        async with repo.transaction():
            await repo.session.execute(
                text(
                    "UPDATE file_leases SET accepts_inbound = :inbound, "
                    "live_cadence = CAST(:cadence AS jsonb) WHERE node_id = :node"
                ),
                {
                    "inbound": inbound,
                    "cadence": '{"debounceMs": 300}' if live else "{}",
                    "node": nodes["team"].id,
                },
            )
    return Mount(nodes, drive, grant.epoch)


def _service(repo: FilesRepo, files_org: FilesOrg, clock: FakeClock) -> LiveEntriesService:
    return LiveEntriesService(repo, _ctx(files_org))


async def _rows(repo: FilesRepo, lease_node_id: uuid.UUID) -> list[Any]:
    async with repo.transaction():
        return list(
            (
                await repo.session.execute(
                    text(
                        "SELECT * FROM file_lease_live_entries WHERE lease_node_id = :n "
                        "ORDER BY node_id"
                    ),
                    {"n": lease_node_id},
                )
            ).all()
        )


async def _live_seq(repo: FilesRepo, lease_node_id: uuid.UUID) -> int:
    async with repo.transaction():
        return int(
            (
                await repo.session.execute(
                    text("SELECT live_seq FROM file_leases WHERE node_id = :n"),
                    {"n": lease_node_id},
                )
            ).scalar_one()
        )


async def _lease_events(repo: FilesRepo, org: FilesOrg) -> list[Any]:
    """The lease announcements this org wrote, in order.

    Scoped to the org because the suite shares one database across its modules:
    an unscoped read would pass or fail on what some other test happened to
    leave behind. Read as the owner rather than through the Files role, which
    is not granted the outbox.
    """
    return list(
        (
            await repo.session.execute(
                text(
                    "SELECT entity, entity_id, version, payload FROM event_outbox "
                    "WHERE type = :t AND org_id = :org ORDER BY id"
                ),
                {"t": EventType.FILE_LEASE_CHANGED.value, "org": org.org_team_id},
            )
        ).all()
    )


async def test_a_batch_at_the_holders_epoch_lands_rows_and_bumps_the_sequence(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The holder says what it is writing; the drive records it and moves on.

    ``live_seq`` is what a client that missed a frame compares against, so the
    batch is only useful if it advances — and the rows it advanced past have to
    be readable at the state the holder reported them in.
    """
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)

    async with repo.transaction():
        seq = await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[
                LiveReport(node_id=mount.node("team/notes.md"), state="writing"),
                LiveReport(
                    node_id=mount.node("team/docs/spec.md"),
                    state="uploading",
                    box_size=4096,
                    box_mtime=datetime(2024, 5, 1, tzinfo=UTC),
                ),
            ],
        )

    assert seq == 1
    assert await _live_seq(repo, mount.ids["team"]) == 1
    rows = await _rows(repo, mount.ids["team"])
    assert sorted(row.state for row in rows) == ["uploading", "writing"]
    uploading = next(row for row in rows if row.state == "uploading")
    assert uploading.box_size == 4096
    assert uploading.lease_epoch == mount.epoch
    assert uploading.seq == 1


@pytest.mark.parametrize("empty", [True, False], ids=["an-empty-batch", "a-batch-with-rows"])
async def test_a_batch_that_passed_the_fence_stamps_the_lease_as_synced(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    empty: bool,
) -> None:
    """A holder whose plane is speaking has synced, whether or not it had a
    row to report.

    ``last_sync_at`` is what the drive reads ``stale`` off — a holder that is
    beating but has not synced for two intervals is shown to every reader as a
    machine that stopped writing back. A chat whose agent has written nothing
    for a minute is not that machine, and the empty batch is how its holder
    says so; a batch with rows says the same, before any of its bytes land.
    """
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)
    assert await _last_sync(repo, mount.ids["team"]) is None

    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[]
            if empty
            else [LiveReport(node_id=mount.node("team/notes.md"), state="writing")],
        )

    assert await _last_sync(repo, mount.ids["team"]) is not None


async def test_a_fenced_batch_stamps_nothing(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The stamp is the holder's word, so a stranger's batch cannot leave it:
    a superseded machine saying it is fine would hide from every reader that
    the folder's real holder has gone quiet."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)

    with pytest.raises(LeaseConflict):
        async with repo.transaction():
            await service.upsert(
                mount.root, epoch=mount.epoch - 1, instance_id=INSTANCE, entries=[]
            )

    assert await _last_sync(repo, mount.ids["team"]) is None


@pytest.mark.parametrize(
    ("entry", "cap", "code"),
    [
        pytest.param("outside.md", None, "files.lease_mismatch", id="refused-by-confinement"),
        pytest.param("team/docs/spec.md", 1, "files.live_too_many", id="refused-by-the-ceiling"),
    ],
)
async def test_a_batch_refused_after_the_fence_has_written_nothing_to_the_lease(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    cap: int | None,
    code: str,
) -> None:
    """Confinement and the ceiling precede the lease row's one UPDATE: a batch
    those refuse has stamped nothing and bumped nothing, inside the very
    transaction that refused it.

    Read before the transaction is rolled back, because the order of the
    statements is the contract. The stamp used to come first, in a statement
    of its own, so that a refused batch still read as synced in its own
    transaction — a difference nothing outside it could see, since the caller
    rolls a refused batch back, and one that cost a second update of the lease
    row: the second update re-checks the row's key to the leased folder, which
    is the deadlock the module docstring describes. The stamp now rides the
    bump, after the checks.
    """
    from alkera_core.config import settings

    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)
    if cap is not None:
        monkeypatch.setattr(settings, "files_live_max_pending_entries", cap)
        async with repo.transaction():
            await service.upsert(
                mount.root,
                epoch=mount.epoch,
                instance_id=INSTANCE,
                entries=[LiveReport(node_id=mount.node("team/notes.md"), state="writing")],
            )
        async with repo.transaction():
            await repo.session.execute(
                text("UPDATE file_leases SET last_sync_at = NULL WHERE node_id = :node"),
                {"node": mount.ids["team"]},
            )
    assert await _last_sync(repo, mount.ids["team"]) is None
    seq_before = await _live_seq(repo, mount.ids["team"])

    inside_the_refusal: Any = None
    with pytest.raises(LeaseConflict) as refused:
        async with repo.transaction():
            try:
                await service.upsert(
                    mount.root,
                    epoch=mount.epoch,
                    instance_id=INSTANCE,
                    entries=[LiveReport(node_id=mount.node(entry), state="writing")],
                )
            finally:
                inside_the_refusal = (
                    await repo.session.execute(
                        text("SELECT last_sync_at, live_seq FROM file_leases WHERE node_id = :n"),
                        {"n": mount.ids["team"]},
                    )
                ).one()

    assert refused.value.code == code
    assert inside_the_refusal.last_sync_at is None, "a refused batch stamped the lease"
    assert inside_the_refusal.live_seq == seq_before, "a refused batch bumped the sequence"


async def _last_sync(repo: FilesRepo, lease_node_id: uuid.UUID) -> Any:
    return (
        await repo.session.execute(
            text("SELECT last_sync_at FROM file_leases WHERE node_id = :node"),
            {"node": lease_node_id},
        )
    ).scalar_one()


async def test_a_second_report_for_a_node_replaces_the_first(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A node is in exactly one state, so a later word wins rather than queues."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)
    node = mount.node("team/notes.md")

    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=node, state="writing")],
        )
    async with repo.transaction():
        second = await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=node, state="uploading", box_size=99)],
        )

    rows = await _rows(repo, mount.ids["team"])
    assert len(rows) == 1
    assert rows[0].state == "uploading"
    assert rows[0].box_size == 99
    assert rows[0].seq == second == 2


async def test_a_batch_at_a_stale_epoch_lands_nothing(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A machine that came back from the dead cannot resurrect its old view.

    The fence is asserted before anything is written, so the refusal leaves the
    plane exactly as it was — no row, and no sequence bump a live client would
    read as "there is something new to fetch".
    """
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)

    with pytest.raises(LeaseConflict) as refused:
        async with repo.transaction():
            await service.upsert(
                mount.root,
                epoch=mount.epoch - 1,
                instance_id=INSTANCE,
                entries=[LiveReport(node_id=mount.node("team/notes.md"), state="writing")],
            )

    assert refused.value.code == "files.lease_fenced"
    assert await _rows(repo, mount.ids["team"]) == []
    assert await _live_seq(repo, mount.ids["team"]) == 0


async def test_another_instance_at_the_right_epoch_is_fenced_too(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The epoch alone is not the holder: the same principal on a second machine
    is a second holder, and only the one the lease row names may report."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)

    with pytest.raises(LeaseConflict) as refused:
        async with repo.transaction():
            await service.upsert(
                mount.root,
                epoch=mount.epoch,
                instance_id="desktop-2",
                entries=[LiveReport(node_id=mount.node("team/notes.md"), state="writing")],
            )

    assert refused.value.code == "files.lease_fenced"
    assert await _rows(repo, mount.ids["team"]) == []


async def test_an_entry_outside_the_subtree_refuses_the_whole_batch(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A fenced request is confined to what it holds.

    The refusal is the whole batch and not the one bad entry: a holder that can
    get its good entries applied alongside a rejected one would learn, one probe
    at a time, which ids exist outside the folder it was given.
    """
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)

    with pytest.raises(LeaseConflict) as refused:
        async with repo.transaction():
            await service.upsert(
                mount.root,
                epoch=mount.epoch,
                instance_id=INSTANCE,
                entries=[
                    LiveReport(node_id=mount.node("team/notes.md"), state="writing"),
                    LiveReport(node_id=mount.node("outside.md"), state="writing"),
                ],
            )

    assert refused.value.code == "files.lease_mismatch"
    assert await _rows(repo, mount.ids["team"]) == []
    assert await _live_seq(repo, mount.ids["team"]) == 0


async def test_the_leased_folder_itself_may_carry_an_entry(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """Ancestor-or-self: the root of the mount is inside its own subtree."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)

    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=mount.root, state="writing")],
        )

    assert [row.node_id for row in await _rows(repo, mount.ids["team"])] == [mount.ids["team"]]


async def test_a_batch_past_the_pending_ceiling_is_refused(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plane is bounded: a holder cannot make the drive hold an unbounded
    list of things it has not sent."""
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "files_live_max_pending_entries", 1)
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)

    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=mount.node("team/notes.md"), state="writing")],
        )
    with pytest.raises(LeaseConflict) as refused:
        async with repo.transaction():
            await service.upsert(
                mount.root,
                epoch=mount.epoch,
                instance_id=INSTANCE,
                entries=[LiveReport(node_id=mount.node("team/docs/spec.md"), state="writing")],
            )

    assert refused.value.code == "files.live_too_many"
    assert len(await _rows(repo, mount.ids["team"])) == 1


async def test_re_reporting_a_node_already_in_flight_is_admitted_at_the_ceiling(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling counts nodes in flight, not reports.

    A holder at the ceiling still has to be able to say "that one finished
    uploading", or the plane would wedge at exactly the moment it matters.
    """
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "files_live_max_pending_entries", 1)
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)
    node = mount.node("team/notes.md")

    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=node, state="writing")],
        )
    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=node, state="uploading")],
        )

    rows = await _rows(repo, mount.ids["team"])
    assert [row.state for row in rows] == ["uploading"]


@pytest.mark.parametrize("verb", ["applied", "superseded"])
async def test_the_holder_clears_a_node_by_reporting_it_settled(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    verb: str,
) -> None:
    """``applied`` and ``superseded`` are not states a node sits in — they are
    the holder saying the difference is gone, so the row goes away."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)
    node = mount.node("team/notes.md")

    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=node, state="writing")],
        )
    async with repo.transaction():
        seq = await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=node, state=verb)],
        )

    assert await _rows(repo, mount.ids["team"]) == []
    assert seq == 2


async def test_every_write_to_the_plane_announces_the_lease(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The announcement is the leased node and the sequence, and nothing else —
    the stream fans out to every session in the org."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)

    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=mount.node("team/notes.md"), state="writing")],
        )

    events = await _lease_events(repo, files_org)
    assert len(events) == 1
    assert events[0].entity == "file_lease"
    assert events[0].entity_id == str(mount.ids["team"])
    assert events[0].version == 1
    assert events[0].payload == {
        "lease_node_id": str(mount.ids["team"]),
        "drive_id": str(mount.drive_id),
        "live_seq": 1,
        # The holder's own report: the holder is not rung back into draining.
        "reason": "report",
    }


async def test_a_refused_batch_announces_nothing(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A fenced holder must not be able to nudge every open client in the org."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)

    with pytest.raises(LeaseConflict):
        async with repo.transaction():
            await service.upsert(
                mount.root,
                epoch=mount.epoch + 7,
                instance_id=INSTANCE,
                entries=[LiveReport(node_id=mount.node("team/notes.md"), state="writing")],
            )

    assert await _lease_events(repo, files_org) == []


async def test_clear_removes_the_rows_and_bumps_the_sequence(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The bytes landed, so the gap the row described is gone."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)
    node = mount.node("team/notes.md")

    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[
                LiveReport(node_id=node, state="uploading"),
                LiveReport(node_id=mount.node("team/docs/spec.md"), state="writing"),
            ],
        )
    async with repo.transaction():
        cleared = await service.clear([node], reason="saved")

    assert cleared == 1
    assert [row.node_id for row in await _rows(repo, mount.ids["team"])] == [
        mount.ids["team/docs/spec.md"]
    ]
    assert await _live_seq(repo, mount.ids["team"]) == 2


async def test_clearing_a_node_with_nothing_in_flight_changes_nothing(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """Every write path calls clear after it commits; the common case is that
    there was never a row, and that must not bump a sequence or wake a client."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)

    async with repo.transaction():
        cleared = await service.clear([mount.node("team/notes.md")], reason="saved")

    assert cleared == 0
    assert await _live_seq(repo, mount.ids["team"]) == 0
    assert await _lease_events(repo, files_org) == []


async def test_accept_inbound_records_the_change_the_holder_has_yet_to_apply(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A write admitted into a leased subtree is not finished when it commits —
    the machine still has the old bytes, so the plane holds the difference."""
    mount = await _mount(repo, files_factory, files_org, clock, inbound=True)
    service = _service(repo, files_org, clock)

    async with repo.transaction():
        await service.accept_inbound(mount.nodes["team/notes.md"], kind="inbound")

    rows = await _rows(repo, mount.ids["team"])
    assert [(row.node_id, row.state) for row in rows] == [(mount.ids["team/notes.md"], "inbound")]
    assert await _live_seq(repo, mount.ids["team"]) == 1


async def test_an_inbound_change_announces_the_lease_as_inbound(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The frame a holder drains on: it says the drive queued something for it,
    which is what lets a holder skip every frame its own reports ring."""
    mount = await _mount(repo, files_factory, files_org, clock, inbound=True)
    service = _service(repo, files_org, clock)

    async with repo.transaction():
        await service.accept_inbound(mount.nodes["team/notes.md"], kind="inbound")

    events = await _lease_events(repo, files_org)
    assert [event.payload.get("reason") for event in events] == ["inbound"]
    assert events[0].entity_id == str(mount.ids["team"])


async def test_accepting_the_same_inbound_change_twice_reports_it_once(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """Two writes to one node before the holder drains leave one thing to do."""
    mount = await _mount(repo, files_factory, files_org, clock, inbound=True)
    service = _service(repo, files_org, clock)

    async with repo.transaction():
        await service.accept_inbound(mount.nodes["team/notes.md"], kind="inbound")
    async with repo.transaction():
        await service.accept_inbound(mount.nodes["team/notes.md"], kind="inbound_rename")

    rows = await _rows(repo, mount.ids["team"])
    assert [(row.node_id, row.state) for row in rows] == [
        (mount.ids["team/notes.md"], "inbound_rename")
    ]


async def test_an_inbound_change_under_no_lease_is_not_recorded(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """There is nobody to hand it to, so there is nothing to remember."""
    mount = await _mount(repo, files_factory, files_org, clock, inbound=True)
    service = _service(repo, files_org, clock)

    async with repo.transaction():
        await service.accept_inbound(mount.nodes["outside.md"], kind="inbound")

    assert await _rows(repo, mount.ids["team"]) == []
    assert await _lease_events(repo, files_org) == []


async def test_the_holder_drains_only_what_is_addressed_to_it(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The drain is the inbound half of the plane: what the holder pushed out is
    its own business and must not come back to it as work."""
    mount = await _mount(repo, files_factory, files_org, clock, inbound=True)
    service = _service(repo, files_org, clock)

    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=mount.node("team/docs/spec.md"), state="uploading")],
        )
    async with repo.transaction():
        await service.accept_inbound(mount.nodes["team/notes.md"], kind="inbound_delete")

    async with repo.transaction():
        drained = await service.inbound_for_holder(
            mount.root, epoch=mount.epoch, instance_id=INSTANCE
        )

    assert drained == [
        InboundEntry(node_id=NodeId(mount.ids["team/notes.md"]), state="inbound_delete", seq=2)
    ]


async def test_a_fenced_holder_drains_nothing(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The read side of the fence: a superseded machine reads as a stranger."""
    mount = await _mount(repo, files_factory, files_org, clock, inbound=True)
    service = _service(repo, files_org, clock)
    async with repo.transaction():
        await service.accept_inbound(mount.nodes["team/notes.md"], kind="inbound")

    with pytest.raises(LeaseConflict) as refused:
        async with repo.transaction():
            await service.inbound_for_holder(
                mount.root, epoch=mount.epoch + 1, instance_id=INSTANCE
            )

    assert refused.value.code == "files.lease_fenced"


async def test_another_orgs_plane_is_invisible(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_org_factory: Any,
    clock: FakeClock,
) -> None:
    """Every statement is org-scoped, so a lease node id guessed across the
    tenant boundary reads as a lease that is not there."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)
    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=mount.node("team/notes.md"), state="writing")],
        )

    other = await files_org_factory()
    other_repo = FilesRepo(repo.session, other.scope)
    other_service = LiveEntriesService(other_repo, _ctx(other))

    with pytest.raises(LeaseConflict) as refused:
        async with other_repo.transaction():
            await other_service.upsert(
                mount.root,
                epoch=mount.epoch,
                instance_id=INSTANCE,
                entries=[LiveReport(node_id=mount.node("team/notes.md"), state="writing")],
            )
    assert refused.value.code == "files.lease_fenced"

    async with other_repo.transaction():
        page = await other_service.facets_for_page(
            [mount.ids["team/notes.md"]],
            lease_node_ids=[mount.ids["team"]],
            now=clock.now(),
        )
    assert page.facets == {}
    assert page.pending == {}


async def test_the_page_reads_the_plane_in_one_statement(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A listing renders many rows that share one lease. The facet for each and
    the "how many are in flight" beside it are one statement for the page, so a
    long page costs what a short one costs."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)
    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[
                LiveReport(node_id=mount.node("team/notes.md"), state="writing", box_size=12),
                LiveReport(node_id=mount.node("team/docs/spec.md"), state="deferred"),
            ],
        )

    async with repo.transaction():
        page = await service.facets_for_page(
            [mount.ids["team/notes.md"]],
            lease_node_ids=[mount.ids["team"]],
            now=clock.now(),
        )

    assert set(page.facets) == {mount.ids["team/notes.md"]}
    facet = page.facets[mount.ids["team/notes.md"]]
    assert facet.state == "writing"
    assert facet.box_size == 12
    # Both entries count towards what the mount is doing, even though only one
    # of them is on this page.
    assert page.pending == {mount.ids["team"]: 2}


async def test_the_plane_is_not_read_for_a_lease_that_has_lapsed(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """One second after the TTL, before any reaper has run, the rows the dead
    holder left behind are no longer anybody's live view of the tree."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)
    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=mount.node("team/notes.md"), state="writing")],
        )
    async with repo.transaction():
        await repo.session.execute(
            text("UPDATE file_leases SET expires_at = now() - interval '1 second' "),
        )

    async with repo.transaction():
        page = await service.facets_for_page(
            [mount.ids["team/notes.md"]],
            lease_node_ids=[mount.ids["team"]],
            now=datetime.now(UTC),
        )

    assert page.facets == {}
    assert page.pending == {}
    # The row is still there — nothing swept it — so the absence is the read's
    # own liveness rule and not a deletion.
    assert len(await _rows(repo, mount.ids["team"])) == 1


async def test_the_lease_facet_carries_what_the_plane_is_doing(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """Every reader under the mount learns that it is streaming, how many files
    are in flight, and — on the row that is one of them — which way it is going."""
    mount = await _mount(repo, files_factory, files_org, clock, inbound=True)
    service = _service(repo, files_org, clock)
    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=mount.node("team/notes.md"), state="uploading")],
        )

    async with repo.transaction():
        facet = await lease_facet(
            repo,
            mount.nodes["team/notes.md"],
            [mount.nodes["team"]],
            ctx=_ctx(files_org),
            now=clock.now(),
        )

    assert facet is not None
    assert facet.live is True
    assert facet.inbound is True
    assert facet.pending == 1
    assert facet.live_seq == 1
    assert facet.live_state is not None
    assert facet.live_state.state == "uploading"


async def test_a_lease_that_is_not_streaming_says_so(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A plain mount is still a fence — the subtree is claimed — but nothing
    under it is ever newer than the last checkpoint, so no row is in flight."""
    mount = await _mount(repo, files_factory, files_org, clock, live=False)

    async with repo.transaction():
        facet = await lease_facet(
            repo,
            mount.nodes["team/notes.md"],
            [mount.nodes["team"]],
            ctx=_ctx(files_org),
            now=clock.now(),
        )

    assert facet is not None
    assert facet.live is False
    assert facet.inbound is False
    assert facet.pending == 0
    assert facet.live_state is None


async def test_a_page_shares_one_planes_answer_across_its_rows(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The batched form folds the same plane onto every row of a page: the one
    that is in flight carries its own state, its neighbour carries the count."""
    mount = await _mount(repo, files_factory, files_org, clock)
    service = _service(repo, files_org, clock)
    async with repo.transaction():
        await service.upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=mount.node("team/notes.md"), state="writing")],
        )

    chain = [mount.ids["team"]]
    async with repo.transaction():
        facets = await lease_facets(
            repo,
            [
                (mount.ids["team/notes.md"], chain),
                (mount.ids["team/docs"], chain),
            ],
            ctx=_ctx(files_org),
            now=clock.now(),
        )

    in_flight = facets[mount.ids["team/notes.md"]]
    neighbour = facets[mount.ids["team/docs"]]
    assert in_flight.live_state is not None
    assert in_flight.live_state.state == "writing"
    assert neighbour.live_state is None
    assert neighbour.pending == in_flight.pending == 1


async def test_a_lapsed_lease_stops_being_reported_as_live(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The whole facet goes, not only the live half, because the fence is gone."""
    mount = await _mount(repo, files_factory, files_org, clock)
    async with repo.transaction():
        await repo.session.execute(
            text("UPDATE file_leases SET expires_at = now() - interval '1 second'")
        )

    async with repo.transaction():
        facet = await lease_facet(
            repo,
            mount.nodes["team/notes.md"],
            [mount.nodes["team"]],
            ctx=_ctx(files_org),
            now=datetime.now(UTC),
        )

    assert facet is None


async def test_the_settled_verbs_are_not_stored_states() -> None:
    """``applied`` and ``superseded`` are things a holder says, not things a
    node is; the column's CHECK must never learn them."""
    from alkera_core.files.lease_live import SETTLED_STATES
    from alkera_core.models.files.leases import LIVE_ENTRY_STATES

    assert SETTLED_STATES.isdisjoint(LIVE_ENTRY_STATES)


async def test_a_long_page_under_a_streaming_lease_costs_exactly_one_statement_more(
    repo: FilesRepo,
    files_engine: AsyncEngine,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """Five hundred rows, one extra query — and only when there is something to
    show.

    The whole reason the plane is read off the leases the page already resolved
    rather than per row is that a chat's Files tab re-renders on every frame
    while an agent works. So the cost is pinned two ways: a page under a plain
    mount pays nothing at all for the live plane, and a page under a streaming
    one pays exactly one statement however long it is.
    """
    mount = await _mount(repo, files_factory, files_org, clock, live=False)
    page = [(mount.ids["team/notes.md"], [mount.ids["team"]])] * 500

    async def cost() -> int:
        async with repo.transaction():
            with _counted(files_engine) as seen:
                await lease_facets(repo, page, ctx=_ctx(files_org), now=clock.now())
        return len(seen)

    assert await cost() == 1

    async with repo.transaction():
        await repo.session.execute(
            text(
                "UPDATE file_leases SET live_cadence = CAST('{\"debounceMs\": 300}' AS jsonb) "
                "WHERE node_id = :node"
            ),
            {"node": mount.ids["team"]},
        )
    async with repo.transaction():
        await _service(repo, files_org, clock).upsert(
            mount.root,
            epoch=mount.epoch,
            instance_id=INSTANCE,
            entries=[LiveReport(node_id=mount.node("team/notes.md"), state="writing")],
        )

    assert await cost() == 2
