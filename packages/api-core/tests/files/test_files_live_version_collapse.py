"""Bounding the versions one generation of live writes leaves behind.

An agent working in a mounted folder saves the file it is building over and
over, and every save that lands under the fence is a version row stamped with
the lease generation it landed under. Ordinary version retention keeps a month
of everything, so one turn would leave hundreds of rows nobody will ever open.

Each case here is a separate way the collapse can be wrong: too eager (it takes
a version the file still needs, or one still inside the window a person scrubs
back through), too broad (it reaches into a generation that is not the one that
went cold, or into an ordinary upload), or too shy (it leaves the burst it
exists to bound).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.repo import FilesRepo
from alkera_core.files.sweepers import LIVE_VERSION_HOT, LiveVersionCollapse, SweepDeps
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

NOW = EPOCH + timedelta(days=400)
SECOND = timedelta(seconds=1)
#: One agent turn's worth of autosaves.
BURST = 12


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


class Bed:
    def __init__(self, session: AsyncSession, org: FilesOrg, node_id: uuid.UUID) -> None:
        self.session = session
        self.org = org
        self.node_id = node_id
        self._seq = 0

    async def version(
        self,
        *,
        at: datetime,
        epoch: int | None,
        head: bool = False,
        keep_forever: bool = False,
        node_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """One landed version. ``epoch`` None is an ordinary upload — no live
        stamp — which is what the collapse must never touch."""
        self._seq += 1
        version_id = uuid.uuid4()
        metadata = "{}" if epoch is None else f'{{"live_lease_epoch": {epoch}}}'
        await self.session.execute(
            text(
                "INSERT INTO file_versions (id, org_team_id, node_id, seq, size_bytes, "
                "content_hash, block_hash, source, scan_state, keep_forever, held, lease_epoch, "
                "created_at, metadata) VALUES (:id, :org, :node, :seq, 8, :hash, '', 'upload', "
                "'clean', :keep, false, 0, :at, CAST(:meta AS jsonb))"
            ),
            {
                "id": version_id,
                "org": self.org.org_team_id,
                "node": node_id or self.node_id,
                "seq": self._seq,
                "hash": f"h{self._seq}",
                "keep": keep_forever,
                "at": at,
                "meta": metadata,
            },
        )
        if head:
            await self.session.execute(
                text("UPDATE file_nodes SET head_version_id = :v WHERE id = :n"),
                {"v": version_id, "n": node_id or self.node_id},
            )
        await self.session.commit()
        return version_id

    async def surviving(self) -> set[uuid.UUID]:
        self.session.expire_all()
        rows = await self.session.execute(
            text("SELECT id FROM file_versions WHERE org_team_id = :org"),
            {"org": self.org.org_team_id},
        )
        return {row.id for row in rows}

    async def collapse(self, at: datetime = NOW) -> int:
        deps = SweepDeps(repo=FilesRepo(self.session, self.org.scope), ctx=_ctx(self.org))
        return (await LiveVersionCollapse(deps).run(at)).swept


@pytest.fixture
async def bed(files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory) -> Bed:
    drive = await files_factory.drive()
    tree = await files_factory.tree("report.html", drive=drive)
    return Bed(files_session, files_org, tree["report.html"].id)


def _cold() -> datetime:
    return NOW - LIVE_VERSION_HOT - SECOND


def _hot() -> datetime:
    return NOW - LIVE_VERSION_HOT + SECOND


async def _burst(bed: Bed, *, at: datetime, epoch: int, head_last: bool = True) -> list[uuid.UUID]:
    """One generation's worth of saves, the last of them the node's head."""
    ids = [await bed.version(at=at, epoch=epoch) for _ in range(BURST - 1)]
    ids.append(await bed.version(at=at, epoch=epoch, head=head_last))
    return ids


async def test_a_cold_burst_collapses_to_the_head_and_one_landing(bed: Bed) -> None:
    """The whole point: one turn's autosaves are not twelve things anyone will
    ever open. What survives is the file as it stands plus the state it reached
    just before that, which is what a person asking "what did it do" wants."""
    ids = await _burst(bed, at=_cold(), epoch=7)

    assert await bed.collapse() == BURST - 2

    assert await bed.surviving() == {ids[-1], ids[-2]}


async def test_the_head_is_never_the_row_that_goes(bed: Bed) -> None:
    """A collapse that takes the head leaves a file that is in the listing and
    opens as nothing — worse than the rows it was trying to save."""
    ids = await _burst(bed, at=_cold(), epoch=7)
    head = (
        await bed.session.execute(
            text("SELECT head_version_id FROM file_nodes WHERE id = :n"), {"n": bed.node_id}
        )
    ).scalar_one()
    assert head == ids[-1]

    await bed.collapse()

    assert ids[-1] in await bed.surviving()


async def test_a_burst_still_inside_the_window_is_left_alone(bed: Bed) -> None:
    """Watching an agent work is the reason the live plane exists; scrubbing
    back through what it just wrote is part of that. Inside the window every
    save is still a thing a person can ask for."""
    ids = await _burst(bed, at=_hot(), epoch=7)

    assert await bed.collapse() == 0

    assert await bed.surviving() == set(ids)


async def test_the_generation_next_door_keeps_its_own_landing(bed: Bed) -> None:
    """Partitioned per generation, not per node: two sessions a week apart are
    two separate things to look back at, and folding them together would leave
    the older one with nothing between its start and the newer one's end."""
    older = [await bed.version(at=_cold(), epoch=6) for _ in range(3)]
    newer = await _burst(bed, at=_cold(), epoch=7)

    await bed.collapse()

    survivors = await bed.surviving()
    assert older[-1] in survivors
    assert survivors == {older[-1], newer[-1], newer[-2]}


async def test_a_version_nobody_wrote_live_is_not_the_collapse_to_make(bed: Bed) -> None:
    """The stamp is the whole licence. An ordinary upload is a deliberate act
    with its own retention policy, and a collapse that swept those would be
    deleting user history on a ten-minute deadline."""
    uploads = [await bed.version(at=_cold(), epoch=None) for _ in range(4)]

    assert await bed.collapse() == 0

    assert await bed.surviving() == set(uploads)


async def test_a_pinned_version_survives_its_generation(bed: Bed) -> None:
    """`keep_forever` outranks every deadline in the ledger; a sweeper that
    quietly ignores it makes the pin a lie."""
    ids = await _burst(bed, at=_cold(), epoch=7)
    pinned = ids[0]
    await bed.session.execute(
        text("UPDATE file_versions SET keep_forever = true WHERE id = :v"), {"v": pinned}
    )
    await bed.session.commit()

    await bed.collapse()

    assert pinned in await bed.surviving()


async def test_a_version_a_signed_url_can_still_redeem_survives(bed: Bed) -> None:
    """A grant row is what makes a URL somebody already holds resolve. Deleting
    the version under it turns a link that was handed out into a 404."""
    ids = await _burst(bed, at=_cold(), epoch=7)
    granted = ids[0]
    await bed.session.execute(
        text(
            "INSERT INTO file_content_grants (nonce, org_team_id, version_id, expires_at) "
            "VALUES (:nonce, :org, :v, :at)"
        ),
        {
            "nonce": uuid.uuid4().hex,
            "org": bed.org.org_team_id,
            "v": granted,
            "at": NOW + timedelta(minutes=5),
        },
    )
    await bed.session.commit()

    await bed.collapse()

    assert granted in await bed.surviving()


async def test_a_second_pass_over_a_collapsed_burst_takes_nothing(bed: Bed) -> None:
    """Idempotence, which is what lets the janitor re-run a pass that died
    half-way through without eating into what the first one left."""
    await _burst(bed, at=_cold(), epoch=7)
    assert await bed.collapse() == BURST - 2
    left = await bed.surviving()

    assert await bed.collapse() == 0

    assert await bed.surviving() == left


async def test_the_budget_bounds_one_pass_and_the_next_finishes(bed: Bed) -> None:
    """A drive that has been mounted all week must not make one pass run long;
    the sweeper takes a page and the schedule comes back for the rest."""
    await _burst(bed, at=_cold(), epoch=7)
    deps = SweepDeps(repo=FilesRepo(bed.session, bed.org.scope), ctx=_ctx(bed.org))

    first = await LiveVersionCollapse(deps).run(NOW, budget=3)

    assert first.swept == 3
    assert await bed.collapse() == BURST - 2 - 3


async def test_another_org_burst_is_not_this_org_to_collapse(
    bed: Bed, files_org_factory: Any, files_session: AsyncSession
) -> None:
    """Every clause is org-scoped; an unscoped DELETE here would erase a
    stranger's history from a pass they never triggered."""
    other = await files_org_factory()
    other_factory = FilesFactory(files_session, other)
    other_drive = await other_factory.drive()
    other_tree = await other_factory.tree("report.html", drive=other_drive)
    other_bed = Bed(files_session, other, other_tree["report.html"].id)
    theirs = await _burst(other_bed, at=_cold(), epoch=7)
    await _burst(bed, at=_cold(), epoch=7)

    await bed.collapse()

    assert set(theirs) <= await other_bed.surviving()


async def test_a_version_a_conflict_names_survives_and_the_pass_still_collapses(bed: Bed) -> None:
    """A two-way write under a live lease leaves a conflict row naming the
    holder's versions: the one that kept the name and, for a submission, the
    displaced bytes parked beside the head. Later autosaves of the same
    generation rank them out, but they are what a person's keep restores, and
    the row's keys would refuse their delete — so they stay, and everything
    else in the burst still goes."""
    ids = await _burst(bed, at=_cold(), epoch=7)
    await bed.session.execute(
        text(
            "INSERT INTO file_conflicts (id, org_team_id, node_id, base_version_id, "
            "theirs_version_id, mine_version_id, actor, state) VALUES "
            "(:id, :org, :node, :base, :theirs, :mine, :actor, 'auto')"
        ),
        {
            "id": uuid.uuid4(),
            "org": bed.org.org_team_id,
            "node": bed.node_id,
            "base": ids[0],
            "theirs": ids[1],
            "mine": ids[2],
            "actor": bed.org.admin_id,
        },
    )
    await bed.session.commit()

    assert await bed.collapse() == BURST - 5

    assert await bed.surviving() == {ids[0], ids[1], ids[2], ids[-2], ids[-1]}
