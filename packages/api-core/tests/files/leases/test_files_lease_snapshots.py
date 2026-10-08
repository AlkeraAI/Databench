"""The lease facet every reader sees under a mount, and when it turns stale.

Against real Postgres, through the real lease rows a `LeaseService.acquire`
wrote — the facet is a read over the same table the holder beats on, so a test
that hand-inserted its own lease row would prove only that the SELECT matches
the INSERT it was written next to.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.ids import NodeId
from alkera_core.files.lease_snapshots import (
    DEFAULT_SYNC_INTERVAL,
    STALE_INTERVALS,
    is_stale,
    lease_facet,
)
from alkera_core.files.leases import LeaseService
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


def _ctx(org: FilesOrg, principal_id: uuid.UUID | None = None) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(principal_id or org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _mounted(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: object,
) -> dict[str, object]:
    """A drive with `team/` mounted by the admin and a file two levels under it."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("team/ team/docs/ team/docs/spec.md", drive=drive)
    async with repo.transaction():
        service = LeaseService(repo, _ctx(files_org), clock)  # type: ignore[arg-type]
        await service.acquire(NodeId(tree["team"].id), instance_id="laptop-1", machine_id="ana-mbp")
    return {"drive": drive, "tree": tree}


async def test_every_node_under_the_leased_folder_carries_the_facet(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: object,
) -> None:
    """The facet names the *leased folder*, not the item it is rendered on.

    A file two levels down is governed by the mount above it; the facet it
    carries must point at `team/`, so the client can offer "unmount" against the
    right node rather than against the file the user happened to click.
    """
    seeded = await _mounted(repo, files_factory, files_org, clock)
    tree = seeded["tree"]
    assert isinstance(tree, dict)
    leaf = tree["team/docs/spec.md"]
    folder = tree["team"]

    async with repo.transaction():
        chain = await repo.chain(leaf)
        facet = await lease_facet(
            repo, leaf, chain, ctx=_ctx(files_org), now=EPOCH + timedelta(seconds=1)
        )

    assert facet is not None
    assert facet.node_id == folder.id
    assert facet.machine == "ana-mbp"
    assert facet.purpose == "mount"


async def test_a_node_outside_the_leased_subtree_has_no_facet(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: object,
) -> None:
    """The negative twin: a sibling folder is not under the mount, so it is free.

    Without this the facet could be "every node in the drive while any lease
    exists" and the positive test above would still pass.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("team/ other/ other/notes.md", drive=drive)
    async with repo.transaction():
        service = LeaseService(repo, _ctx(files_org), clock)  # type: ignore[arg-type]
        await service.acquire(NodeId(tree["team"].id), instance_id="laptop-1", machine_id="ana-mbp")

    outside = tree["other/notes.md"]
    async with repo.transaction():
        chain = await repo.chain(outside)
        facet = await lease_facet(
            repo, outside, chain, ctx=_ctx(files_org), now=EPOCH + timedelta(seconds=1)
        )

    assert facet is None


@pytest.mark.parametrize(
    ("as_holder", "expected_mine"),
    [
        pytest.param(True, True, id="holder-sees-mine"),
        pytest.param(False, False, id="non-holder-does-not"),
    ],
)
async def test_mine_is_true_only_for_the_holder(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: object,
    as_holder: bool,
    expected_mine: bool,
) -> None:
    """`mine` comes from the caller, not the row: one lease, two answers."""
    seeded = await _mounted(repo, files_factory, files_org, clock)
    tree = seeded["tree"]
    assert isinstance(tree, dict)
    leaf = tree["team/docs/spec.md"]
    viewer = files_org.admin_id if as_holder else files_org.member_id

    async with repo.transaction():
        chain = await repo.chain(leaf)
        facet = await lease_facet(
            repo,
            leaf,
            chain,
            ctx=_ctx(files_org, viewer),
            now=EPOCH + timedelta(seconds=1),
        )

    assert facet is not None
    assert facet.mine is expected_mine


async def test_stale_flips_when_sync_stops_while_the_heartbeat_continues(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: object,
) -> None:
    """The clock-driven crossing: alive, but behind.

    The holder keeps beating (its `heartbeat_at` advances) while `last_sync_at`
    stands still. The facet is fresh right up to two sync intervals and stale
    one second past — which is a property of the gap between the two columns,
    not of either one alone, so a holder that stops beating entirely does not
    reach this branch and a holder that keeps pushing never does either.
    """
    seeded = await _mounted(repo, files_factory, files_org, clock)
    tree = seeded["tree"]
    assert isinstance(tree, dict)
    folder = tree["team"]
    leaf = tree["team/docs/spec.md"]

    synced = EPOCH + timedelta(seconds=10)
    gap = STALE_INTERVALS * DEFAULT_SYNC_INTERVAL

    async def facet_with_heartbeat(heartbeat_at: object) -> object:
        async with repo.transaction():
            await repo.session.execute(
                text(
                    "UPDATE file_leases SET last_sync_at = :synced, heartbeat_at = :beat, "
                    "expires_at = CAST(:beat AS timestamptz) + interval '60 seconds' "
                    "WHERE node_id = :node AND org_team_id = :org"
                ),
                {
                    "synced": synced,
                    "beat": heartbeat_at,
                    "node": folder.id,
                    "org": files_org.org_team_id,
                },
            )
        async with repo.transaction():
            chain = await repo.chain(leaf)
            return await lease_facet(repo, leaf, chain, ctx=_ctx(files_org), now=heartbeat_at)

    at_the_boundary = await facet_with_heartbeat(synced + gap)
    assert at_the_boundary is not None
    assert at_the_boundary.stale is False  # type: ignore[attr-defined]

    past_the_boundary = await facet_with_heartbeat(synced + gap + timedelta(seconds=1))
    assert past_the_boundary is not None
    assert past_the_boundary.stale is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("behind", "expected"),
    [
        pytest.param(timedelta(seconds=0), False, id="just-synced"),
        pytest.param(
            STALE_INTERVALS * DEFAULT_SYNC_INTERVAL - timedelta(seconds=1),
            False,
            id="one-second-inside",
        ),
        pytest.param(STALE_INTERVALS * DEFAULT_SYNC_INTERVAL, False, id="exactly-two-intervals"),
        pytest.param(
            STALE_INTERVALS * DEFAULT_SYNC_INTERVAL + timedelta(seconds=1),
            True,
            id="one-second-past",
        ),
    ],
)
def test_stale_boundary_is_strictly_past_two_intervals(behind: timedelta, expected: bool) -> None:
    """The boundary itself, without a database: `>` and not `>=`."""
    synced = EPOCH + timedelta(minutes=5)
    assert is_stale(synced + behind, synced, acquired_at=EPOCH) is expected


def test_a_lease_that_never_synced_is_measured_from_when_it_was_acquired() -> None:
    """A mount that beats for ten minutes without ever pushing is stale.

    Falling back to `acquired_at` is what stops a NULL `last_sync_at` reading as
    "perfectly fresh forever", which is the shape a naive implementation has.
    """
    beat = EPOCH + STALE_INTERVALS * DEFAULT_SYNC_INTERVAL + timedelta(seconds=1)
    assert is_stale(beat, None, acquired_at=EPOCH) is True
    fresh = EPOCH + timedelta(seconds=1)
    assert is_stale(fresh, None, acquired_at=EPOCH) is False
