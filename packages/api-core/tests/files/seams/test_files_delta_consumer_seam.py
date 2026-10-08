"""The delta feed as a *seam*, driven by a second consumer that crashes.

Future caller this stands for: the **search indexer**. Desktop sync is the
consumer the feed was designed around; the indexer is the second one, and it is
shaped differently on purpose — it keeps its own durable cursor, it applies one
item at a time, and it dies in the middle of a page and comes back. If the feed
only worked for a consumer that applied a whole page atomically it would need a
signature change to admit this one.

The claim the tests make is the one the feed's docstring promises and a second
consumer is what proves: **duplicates, never gaps.** A page may be delivered
twice — so the indexer, restarting from the cursor it had actually persisted,
re-reads rows it already applied — and the end state is still exactly one index
entry per node, at the node's latest state.

Everything runs through the real entry point,
:meth:`~alkera_core.files.delta.DeltaService.read`, over real ``event_outbox``
rows written by the library's own :func:`emit_node_changed`. The crash is
scheduled (the indexer is told which item to die on), never a mock raising.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.clock import FakeClock
from alkera_core.files.delta import DeltaService, DeltaToken
from alkera_core.files.history import emit_node_changed
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from sqlalchemy.ext.asyncio import AsyncSession
from tests._live_window import live_window
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

KEY = "delta-consumer-seam-key"
#: Enough nodes that the indexer's page size cuts the feed several times.
NODES = 7
#: The indexer's page size. Smaller than ``NODES`` on purpose.
PAGE = 3

#: How long a wait-for-delivery pass yields before asking the feed again. It
#: steps aside for whatever is holding a row back instead of spinning the
#: database.
POLL_SECONDS = 0.01

#: How long a row this rig committed may stay undelivered before the rig calls
#: the feed broken rather than the database busy. A wait with no end would turn
#: a boundary that stopped rising into a suite that hangs, which is the one
#: failure nobody can read.
DELIVERY_WINDOW = live_window(5.0)


class IndexerCrashedError(Exception):
    """The scheduled fault: the indexer died part-way through a page."""


@dataclass
class SearchIndexer:
    """A consumer with its own durable cursor and its own id-keyed index.

    It is deliberately *not* transactional with the feed: the cursor is written
    only after a whole page is applied, which is the ordering that makes a
    mid-page crash re-deliver work — the case the feed must survive.
    """

    service: DeltaService
    repo: FilesRepo
    drive_id: DriveId
    #: What survives a crash: the persisted cursor and the index itself.
    cursor: DeltaToken | None = None
    index: dict[NodeId, str] = field(default_factory=dict)
    #: Every id handed to :meth:`_apply`, including the re-delivered ones.
    applied: list[NodeId] = field(default_factory=list)
    #: Die after this many applies since the last crash; ``None`` to run clean.
    die_after: int | None = None
    _since_crash: int = 0

    def _apply(self, node_id: NodeId, name: str | None, deleted: bool) -> None:
        self._since_crash += 1
        if self.die_after is not None and self._since_crash > self.die_after:
            raise IndexerCrashedError(str(node_id))
        if deleted:
            self.index.pop(node_id, None)
        else:
            self.index[node_id] = name or ""
        self.applied.append(node_id)

    async def catch_up(self) -> None:
        """Read until the feed says there is nothing more."""
        while True:
            async with self.repo.transaction():
                page = await self.service.read(self.drive_id, token=self.cursor, limit=PAGE)
            for item in page.items:
                self._apply(item.id, item.name, item.deleted)
            # Only now is the page durable from the indexer's point of view.
            self.cursor = page.next_link or page.delta_link
            if page.next_link is None:
                return

    def restart(self, *, die_after: int | None = None) -> None:
        """Come back up: the index and the cursor survived, nothing else did."""
        self.die_after = die_after
        self._since_crash = 0


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


@dataclass(frozen=True, slots=True)
class FeedRig:
    indexer: SearchIndexer
    service: DeltaService
    repo: FilesRepo
    ctx: ActingContext
    drive: FileDrive
    nodes: list[FileNode]
    session: AsyncSession

    @property
    def drive_id(self) -> DriveId:
        return DriveId(self.drive.id)

    async def announce(self, node: FileNode, *, version: int) -> None:
        """Write the outbox row. Committing it is not yet delivering it — the
        case that cares pairs this with :meth:`delivered`."""
        async with self.repo.transaction():
            await emit_node_changed(
                self.repo,
                self.ctx,
                node_id=NodeId(node.id),
                drive_id=self.drive_id,
                version=version,
            )

    async def delivered(self, *nodes: FileNode, since: DeltaToken | None) -> None:
        """Read the feed from ``since`` until it has handed back every named node.

        Committing an outbox row is not quite what makes it readable. The feed
        holds a row back until every transaction that could still take an outbox
        id in THIS database has finished — the watermark ``delta.py`` documents,
        and the reason it can promise duplicates but never gaps. Another backend
        on this database holding a transaction id older than the row keeps it
        back for as long as that transaction runs: an autoanalyze, a fixture's
        own connection, a vacuum. Traffic on the server's OTHER databases is
        narrowed out of the boundary and holds nothing back — a claim the delta
        suite proves directly, and the reason this rig is not at the mercy of
        whatever else the cluster is doing.

        So the rig asks the feed rather than assuming, and it asks about the
        rows the case announced rather than about the feed's own "caught up" —
        the feed is caught up the moment it has nothing DELIVERABLE left, which
        is true with the case's rows still withheld. Waiting for the ids means
        every later assertion is about what the indexer does with those rows,
        and about the pages it cuts them into, rather than about what this
        database happened to be busy with. Reading is side-effect free, so the
        probe leaves the indexer's own cursor exactly where it found it.
        """
        want = {NodeId(node.id) for node in nodes}
        deadline = time.monotonic() + DELIVERY_WINDOW.seconds
        while True:
            seen: set[NodeId] = set()
            token = since
            while True:
                async with self.repo.transaction():
                    page = await self.service.read(self.drive_id, token=token)
                seen.update(item.id for item in page.items)
                if page.next_link is None:
                    break
                token = page.next_link
            if want <= seen:
                return
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"the feed withheld {len(want - seen)} of {len(want)} announced rows "
                    f"for {DELIVERY_WINDOW}"
                )
            await asyncio.sleep(POLL_SECONDS)


@pytest.fixture
async def rig(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    clock: FakeClock,
) -> FeedRig:
    drive = await files_factory.drive()
    spec = " ".join(["papers/", *(f"papers/f{index}.txt" for index in range(NODES))])
    made = await files_factory.tree(spec, drive=drive)
    nodes = [made[f"papers/f{index}.txt"] for index in range(NODES)]
    ctx = _ctx(files_org)
    service = DeltaService(repo, ctx, clock, signing_key=KEY)
    return FeedRig(
        indexer=SearchIndexer(service=service, repo=repo, drive_id=DriveId(drive.id)),
        service=service,
        repo=repo,
        ctx=ctx,
        drive=drive,
        nodes=nodes,
        session=files_session,
    )


async def _announce_all(rig: FeedRig) -> None:
    """Announce every node, and hand back only once the feed will deliver them all.

    The whole set, not each row as it is written: the page boundaries the crash
    cases count on are the ones the indexer cuts over a feed that has all seven
    rows in it. An indexer let loose while three of them were still behind the
    watermark would read a short page, take its delta link as the end, and cut
    the rest into pages the arithmetic below does not describe.
    """
    for index, node in enumerate(rig.nodes):
        await rig.announce(node, version=index + 1)
    await rig.delivered(*rig.nodes, since=None)


def _expected(rig: FeedRig) -> dict[NodeId, str]:
    return {NodeId(node.id): node.name_display for node in rig.nodes}


# ---- the seam's claims -----------------------------------------------------


async def test_a_clean_run_indexes_every_row_exactly_once(rig: FeedRig) -> None:
    """The control. Without it, "exactly one entry" could pass on an empty feed."""
    await _announce_all(rig)
    await rig.indexer.catch_up()

    assert rig.indexer.index == _expected(rig)
    assert sorted(rig.indexer.applied) == sorted(_expected(rig))
    assert len(rig.indexer.applied) == NODES


@pytest.mark.parametrize(
    "die_after",
    [
        pytest.param(1, id="first-item-of-a-page"),
        pytest.param(2, id="mid-page"),
        pytest.param(PAGE, id="page-boundary"),
        pytest.param(PAGE + 1, id="first-item-of-the-second-page"),
        pytest.param(NODES - 1, id="last-item"),
    ],
)
async def test_a_crash_mid_page_resumes_from_the_cursor_and_ends_with_one_entry_per_row(
    rig: FeedRig, die_after: int
) -> None:
    """The claim: whatever it crashed on, the index converges to one entry per
    node — and the re-delivered items are visible in ``applied``, so the test
    is not passing because nothing was replayed."""
    await _announce_all(rig)
    indexer = rig.indexer

    indexer.die_after = die_after
    with pytest.raises(IndexerCrashedError):
        await indexer.catch_up()
    crashed_at = len(indexer.applied)
    assert crashed_at == die_after

    indexer.restart()
    await indexer.catch_up()

    assert indexer.index == _expected(rig)
    # Exactly the items of the half-applied page were delivered a second time —
    # computed from the page size, not read back off the run.
    assert len(indexer.applied) - NODES == die_after % PAGE


async def test_the_cursor_the_crash_left_behind_is_the_last_durable_page(
    rig: FeedRig,
) -> None:
    """A cursor written per item instead of per page would skip the rest of the
    page it died in; this pins that the resume point is the page boundary."""
    await _announce_all(rig)
    indexer = rig.indexer

    indexer.die_after = PAGE + 1
    with pytest.raises(IndexerCrashedError):
        await indexer.catch_up()
    assert indexer.cursor is not None
    survived = dict(indexer.index)

    indexer.restart()
    async with rig.repo.transaction():
        page = await indexer.service.read(rig.drive_id, token=indexer.cursor, limit=PAGE)

    # The whole page it died in comes back — including the one item of it the
    # indexer had already applied. That is the point: the resume point is the
    # last page boundary, so nothing between it and the crash is skipped.
    redelivered = {uuid.UUID(str(item.id)) for item in page.items}
    assert redelivered == {node.id for node in rig.nodes[PAGE : PAGE * 2]}
    assert NodeId(rig.nodes[PAGE].id) in survived
    assert len(survived) == PAGE + 1


async def test_a_second_pass_over_an_unchanged_feed_changes_nothing(rig: FeedRig) -> None:
    """Idempotent apply: catching up twice is not a second index entry."""
    await _announce_all(rig)
    await rig.indexer.catch_up()
    before = dict(rig.indexer.index)
    applied_before = len(rig.indexer.applied)

    await rig.indexer.catch_up()

    assert rig.indexer.index == before
    assert len(rig.indexer.applied) == applied_before


async def test_a_row_announced_after_the_indexer_caught_up_arrives_on_the_next_pass(
    rig: FeedRig,
) -> None:
    """The cursor is a watermark, not a snapshot: the consumer keeps up."""
    await _announce_all(rig)
    await rig.indexer.catch_up()

    late = rig.nodes[0]
    late.name_display = "renamed.txt"
    await rig.session.commit()
    await rig.announce(late, version=99)
    await rig.delivered(late, since=rig.indexer.cursor)

    await rig.indexer.catch_up()
    assert rig.indexer.index[NodeId(late.id)] == "renamed.txt"


async def test_a_node_the_indexer_may_no_longer_read_is_left_off_its_pages(
    rig: FeedRig,
) -> None:
    """The second consumer meets the revoke path too: a node it may no longer
    read is not on its page at all, so a name it must not have never reaches
    the index. The entry it already held stays, which is the feed's documented
    cost: the feed cannot tell one caller about a lost node without telling
    every caller the node exists."""
    await _announce_all(rig)
    await rig.indexer.catch_up()
    secret = rig.nodes[1]
    held = rig.indexer.index[NodeId(secret.id)]

    secret.name_display = "renamed-secret.txt"
    await rig.session.commit()
    await rig.announce(secret, version=100)
    await rig.delivered(secret, since=rig.indexer.cursor)
    hidden = NodeId(secret.id)
    async with rig.repo.transaction():
        page = await rig.indexer.service.read(
            rig.drive_id,
            token=rig.indexer.cursor,
            limit=PAGE,
            readable=lambda node_id: node_id != hidden,
        )
    for item in page.items:
        rig.indexer._apply(item.id, item.name, item.deleted)

    assert all(item.id != hidden for item in page.items)
    assert rig.indexer.index[hidden] == held != "renamed-secret.txt"
