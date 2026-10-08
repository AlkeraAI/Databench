"""A transaction that holds the lease row and nothing else updates it once — so
a writer under the lease never deadlocks with it.

Every writer under a lease takes the leased folder at update strength (the
lease gate) and then the lease row. The heartbeat and the holder's live batch
take the lease row alone. Those orders cannot cross — unless the row-only side
takes the folder *after* the row, which is exactly what a second UPDATE of the
lease row inside one transaction does: Postgres re-verifies the row's foreign
key to ``file_nodes`` on the second update and takes ``FOR KEY SHARE`` on the
folder, which queued behind the ``FOR UPDATE`` the gate used to take (the gate
is a no-key lock now, and these tests hold the folder ``FOR UPDATE`` outright so
the rule is pinned whatever the gate's strength). The batched beat stamped
``last_sync_at`` in a second statement, and on the box it deadlocked (``40P01``)
against the holder's own pushes; the live batch stamped the row and then bumped
its sequence in a second statement, and in CI it deadlocked the same way
against the holder's own tree report. Both held the row and waited for the
folder while the writer held the folder and waited for the row.

The first test pins the Postgres behaviour the rule rests on; the rest pin the
rule for each row-only transaction. Two real sessions, a short ``lock_timeout``
on the side that must not wait, no sleeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import NodeId, OrgScope
from alkera_core.files.lease_live import LiveEntriesService, LiveReport
from alkera_core.files.leases import LeasePurpose, LeaseService, covering_lease
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

LOCK_TIMEOUT = "55P03"
#: Long enough for a beat that waits on nothing; a beat that queued behind the
#: writer's folder lock is refused by ``lock_timeout`` well before this.
BEAT_DEADLINE = 10.0


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _sqlstate(exc: BaseException) -> str | None:
    seen: BaseException | None = exc
    while seen is not None:
        original = getattr(seen, "orig", None)
        state = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if state is not None:
            return str(state)
        seen = seen.__cause__ or seen.__context__
    return None


class Leased:
    def __init__(self, org: FilesOrg, tree: dict[str, Any], epoch: int) -> None:
        self.org = org
        self.tree = tree
        self.epoch = epoch

    @property
    def scope(self) -> OrgScope:
        return OrgScope(org_team_id=self.org.org_team_id)

    @property
    def box(self) -> NodeId:
        return NodeId(self.tree["box"].id)


async def _lease(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    *,
    purpose: LeasePurpose,
) -> Leased:
    drive = await files_factory.drive()
    tree = await files_factory.tree("box/ box/a.txt", drive=drive)
    async with repo.transaction():
        grant = await LeaseService(repo, _ctx(files_org), clock).acquire(
            NodeId(tree["box"].id), instance_id="box-1", machine_id="box-mbp", purpose=purpose
        )
    return Leased(files_org, tree, grant.epoch)


@pytest.fixture
async def leased(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> Leased:
    """A chat's lease: the kind whose holder runs the live plane and beats ``synced``."""
    return await _lease(repo, files_factory, files_org, clock, purpose="chat")


async def _hold_folder(session: Any, node_id: Any) -> None:
    """What the writer's lease gate looks like from another backend."""
    await session.execute(
        text("SELECT id FROM file_nodes WHERE id = :id FOR UPDATE"), {"id": node_id}
    )


async def test_a_second_update_of_a_lease_row_takes_the_leased_folders_key_lock(
    leased: Leased, sessions: Callable[..., Awaitable[list[Any]]]
) -> None:
    """The Postgres fact the single-statement rule rests on. With the folder
    held ``FOR UPDATE`` elsewhere, the FIRST update of the lease row goes
    through — it locks the row and nothing else — and the SECOND update of the
    same row in the same transaction waits for the folder: the row it now
    finds is one this transaction wrote, so its key to the folder is checked
    again, ``FOR KEY SHARE``. The day this passes without the wait, the rule
    is no longer load-bearing."""
    holder, beater = await sessions(2)
    await _hold_folder(holder, leased.box)
    try:
        await beater.execute(text("SET LOCAL lock_timeout = '300ms'"))
        first = await beater.execute(
            text("UPDATE file_leases SET heartbeat_at = now() WHERE node_id = :n RETURNING 1"),
            {"n": leased.box},
        )
        assert first.scalar() == 1, "the first update takes the lease row alone"
        with pytest.raises(DBAPIError) as waited:
            await beater.execute(
                text("UPDATE file_leases SET last_sync_at = now() WHERE node_id = :n"),
                {"n": leased.box},
            )
        assert _sqlstate(waited.value) == LOCK_TIMEOUT
    finally:
        await beater.rollback()
        await holder.rollback()


async def test_a_synced_beat_completes_while_a_writer_holds_the_leased_folder(
    leased: Leased, sessions: Callable[..., Awaitable[list[Any]]], clock: FakeClock
) -> None:
    """The incident, with the fix in place: the push holds the leased folder
    (its gate) and has not yet taken the lease row; the box's beat for that
    folder, ``synced``, lands and commits without waiting on the folder — a beat
    that queued there is refused by its ``lock_timeout`` and fails this test —
    and the push's fence then reads the row the beat renewed."""
    writer_session, beat_session = await sessions(2)
    writer = FilesRepo(writer_session, leased.scope)
    beater = FilesRepo(beat_session, leased.scope)
    a_txt = leased.tree["box/a.txt"]
    ctx = _ctx(leased.org)

    async def beat() -> Any:
        async with beater.transaction():
            await beat_session.execute(text("SET LOCAL lock_timeout = '300ms'"))
            return await LeaseService(beater, ctx, clock).heartbeat(
                leased.box, epoch=leased.epoch, instance_id="box-1", synced=True
            )

    async with writer.transaction():
        assert await writer.lock_lease_gate(NodeId(a_txt.id)) == leased.box
        try:
            renewed = await asyncio.wait_for(beat(), BEAT_DEADLINE)
        except DBAPIError as exc:
            pytest.fail(f"the beat waited on the leased folder: {_sqlstate(exc)} {exc!r}")
        assert renewed.last_sync_at is not None, "a synced beat stamps the lease"
        assert renewed.epoch == leased.epoch
        # The writer's fence, taken after the beat committed: the row it
        # locks is the one the beat renewed, and nothing deadlocks.
        try:
            covering = await covering_lease(writer, a_txt, lock="no key update")
        except DBAPIError as exc:
            pytest.fail(f"the writer's fence failed after the beat: {_sqlstate(exc)} {exc!r}")
        assert covering is not None and covering.node_id == leased.box
        assert covering.last_sync_at == renewed.last_sync_at


@pytest.mark.parametrize(
    ("seeded", "reported", "seq_after"),
    [
        pytest.param(None, None, 0, id="an-empty-batch"),
        pytest.param(None, "writing", 1, id="a-first-report"),
        pytest.param("writing", "applied", 2, id="a-settle"),
    ],
)
async def test_a_live_batch_completes_while_a_writer_holds_the_leased_folder(
    leased: Leased,
    sessions: Callable[..., Awaitable[list[Any]]],
    seeded: str | None,
    reported: str | None,
    seq_after: int,
) -> None:
    """The CI deadlock, with the fix in place: the holder's tree report holds
    the leased folder (its gate) and has not yet taken the lease row; the box's
    live batch for that folder lands and commits without waiting on the folder
    — a batch that queued there is refused by its ``lock_timeout`` and fails
    this test. The batch stamped the row and then bumped its sequence in a
    second UPDATE, and the second update's key re-check queued on the folder;
    the two now ride one statement, so a batch with rows stamps and bumps at
    once and an empty batch stamps alone. ``a-settle`` is the batch CI sent —
    ``applied`` for a row the drive held — and ``a-first-report`` the one that
    inserts a row; both were red, the empty batch never was.

    The folder is held ``FOR UPDATE`` outright — the strongest lock another
    backend can hold on it — so what is pinned is that the batch asks for the
    folder at no strength at all, whatever strength the writers' gate takes.
    """
    holder, batch_session = await sessions(2)
    batcher = FilesRepo(batch_session, leased.scope)
    a_txt = NodeId(leased.tree["box/a.txt"].id)
    ctx = _ctx(leased.org)

    async def batch(state: str | None, *, refuse_waits: bool) -> int:
        async with batcher.transaction():
            if refuse_waits:
                await batch_session.execute(text("SET LOCAL lock_timeout = '300ms'"))
            return await LiveEntriesService(batcher, ctx).upsert(
                leased.box,
                epoch=leased.epoch,
                instance_id="box-1",
                entries=[] if state is None else [LiveReport(node_id=a_txt, state=state)],
            )

    if seeded is not None:
        await batch(seeded, refuse_waits=False)

    await _hold_folder(holder, leased.box)
    try:
        try:
            seq = await asyncio.wait_for(batch(reported, refuse_waits=True), BEAT_DEADLINE)
        except DBAPIError as exc:
            pytest.fail(f"the live batch waited on the leased folder: {_sqlstate(exc)} {exc!r}")
        assert seq == seq_after
    finally:
        await holder.rollback()

    row = (
        await batch_session.execute(
            text(
                "SELECT l.live_seq, l.last_sync_at, "
                "(SELECT count(*) FROM file_lease_live_entries e "
                " WHERE e.lease_node_id = l.node_id AND e.node_id = :file) AS in_flight "
                "FROM file_leases l WHERE l.node_id = :lease"
            ),
            {"lease": leased.box, "file": a_txt},
        )
    ).one()
    assert row.live_seq == seq_after
    assert row.last_sync_at is not None, "a batch that passed the fence stamps the lease"
    assert row.in_flight == (1 if reported == "writing" else 0)


@pytest.mark.parametrize(
    ("purpose", "stamped"),
    [
        pytest.param("chat", True, id="a-lease-that-runs-the-plane-is-stamped"),
        pytest.param("mount", False, id="a-mount-runs-no-plane-and-is-not"),
    ],
)
async def test_the_synced_word_stamps_only_a_lease_that_takes_inbound_writes(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    purpose: LeasePurpose,
    stamped: bool,
) -> None:
    """The stamp moved into the beat's statement keeps the route's condition:
    ``synced`` means something only on a lease that accepts inbound writes."""
    leased = await _lease(repo, files_factory, files_org, clock, purpose=purpose)
    async with repo.transaction():
        plain = await LeaseService(repo, _ctx(files_org), clock).heartbeat(
            leased.box, epoch=leased.epoch, instance_id="box-1"
        )
        assert plain.last_sync_at is None, "a beat alone says nothing about the plane"
        synced = await LeaseService(repo, _ctx(files_org), clock).heartbeat(
            leased.box, epoch=leased.epoch, instance_id="box-1", synced=True
        )
    assert (synced.last_sync_at is not None) is stamped
