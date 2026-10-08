"""Resolving a conflict is a change like any other: it is on record and it is announced.

The claim under test is not that the head moved — that was already true — but
that a second device learns about it. So every case asserts the ``file_history``
row and the delta feed, which is the only thing a sync client reads. Delete the
``record`` call or the ``emit_node_changed`` call in ``conflict_resolution`` and
the first two tests fail; keep the promotion and drop the announcement and the
feed test fails alone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import conflict_resolution
from alkera_core.files.clock import FakeClock
from alkera_core.files.delta import DeltaService
from alkera_core.files.errors import NotFound, PreconditionFailed
from alkera_core.files.ids import DriveId
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.history import FileConflict, FileHistory
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

KEY = "conflict-resolution-test-signing-key"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _version(
    session: AsyncSession, node: FileNode, *, seq: int, size: int, content_hash: str
) -> FileVersion:
    version = FileVersion(
        id=uuid.uuid4(),
        org_team_id=node.org_team_id,
        node_id=node.id,
        seq=seq,
        size_bytes=size,
        content_hash=content_hash,
        source="upload",
        store_key=f"objects/{content_hash}",
    )
    session.add(version)
    await session.flush()
    return version


async def _conflicted(
    session: AsyncSession, factory: FilesFactory, *, org: FilesOrg
) -> tuple[FileDrive, FileNode, FileConflict, FileVersion, FileVersion]:
    """A file with two divergent versions and the open conflict between them."""
    drive = await factory.drive(org=org)
    made = await factory.tree("doc.txt", drive=drive)
    node = made["doc.txt"]
    theirs = await _version(session, node, seq=1, size=11, content_hash="aa" * 32)
    mine = await _version(session, node, seq=2, size=222, content_hash="bb" * 32)
    node.head_version_id = theirs.id
    node.size = theirs.size_bytes
    row = FileConflict(
        id=uuid.uuid4(),
        org_team_id=org.org_team_id,
        node_id=node.id,
        base_version_id=None,
        theirs_version_id=theirs.id,
        mine_version_id=mine.id,
        actor=org.admin_id,
        state="open",
    )
    session.add(row)
    await session.commit()
    return drive, node, row, theirs, mine


async def _history(session: AsyncSession, node_id: uuid.UUID) -> list[FileHistory]:
    rows = await session.execute(
        select(FileHistory).where(FileHistory.node_id == node_id).order_by(FileHistory.seq)
    )
    return list(rows.scalars().all())


async def test_a_resolve_writes_one_history_row_naming_the_promotion(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    _drive, node, row, _theirs, mine = await _conflicted(
        files_session, files_factory, org=files_org
    )
    clock = FakeClock(now=EPOCH)

    async with repo.transaction():
        result = await conflict_resolution.resolve_conflict(
            repo, _ctx(files_org), row.id, choice="mine", if_match=node.etag, clock=clock
        )

    assert result.head_version_id == mine.id
    written = await _history(files_session, node.id)
    assert len(written) == 1, "a resolved conflict must leave exactly one history row"
    # Its own kind, not the `attrs` a plain new version takes: a reader of the
    # history pane can tell a promotion from an attribute change.
    assert written[0].kind == "conflict_resolved"
    assert written[0].after is not None
    assert written[0].after["conflict_resolution"] == "mine"
    assert written[0].after["conflict_id"] == str(row.id)
    assert written[0].after["head_version_id"] == str(mine.id)
    assert written[0].acting_principal == files_org.admin_id
    refreshed = await files_session.get(FileNode, node.id)
    assert refreshed is not None
    assert refreshed.head_version_id == mine.id
    assert refreshed.size == mine.size_bytes


async def test_a_resolve_shows_the_node_on_the_delta_feed(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    drive, node, row, _theirs, _mine = await _conflicted(
        files_session, files_factory, org=files_org
    )
    clock = FakeClock(now=EPOCH)
    delta = DeltaService(repo, _ctx(files_org), clock, signing_key=KEY)

    async with repo.transaction():
        start = await delta.latest_token(DriveId(drive.id))
    async with repo.transaction():
        await conflict_resolution.resolve_conflict(
            repo, _ctx(files_org), row.id, choice="theirs", if_match=node.etag, clock=clock
        )
    async with repo.transaction():
        page = await delta.read(DriveId(drive.id), token=start)

    assert [item.id for item in page.items] == [node.id], (
        "a consumer that only reads the delta feed must see the resolved node"
    )


async def test_keeping_both_creates_the_sibling_and_still_announces_the_node(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    drive, node, row, theirs, mine = await _conflicted(files_session, files_factory, org=files_org)
    clock = FakeClock(now=EPOCH)
    delta = DeltaService(repo, _ctx(files_org), clock, signing_key=KEY)

    async with repo.transaction():
        start = await delta.latest_token(DriveId(drive.id))
    async with repo.transaction():
        result = await conflict_resolution.resolve_conflict(
            repo, _ctx(files_org), row.id, choice="both", if_match=node.etag, clock=clock
        )
    async with repo.transaction():
        page = await delta.read(DriveId(drive.id), token=start)

    assert result.head_version_id == theirs.id
    assert result.copy_node_id is not None
    copy = await files_session.get(FileNode, result.copy_node_id)
    assert copy is not None
    assert copy.head_version_id == mine.id
    assert copy.name != node.name, "the caller's side is kept under its own name"
    assert node.id in {item.id for item in page.items}


async def test_a_stale_if_match_leaves_the_conflict_open(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
) -> None:
    _drive, node, row, theirs, _mine = await _conflicted(
        files_session, files_factory, org=files_org
    )
    clock = FakeClock(now=EPOCH)
    conflict_id, node_id, theirs_id = row.id, node.id, theirs.id

    with pytest.raises(PreconditionFailed):
        async with repo.transaction():
            await conflict_resolution.resolve_conflict(
                repo,
                _ctx(files_org),
                conflict_id,
                choice="mine",
                if_match=node.etag + 7,
                clock=clock,
            )

    files_session.expire_all()
    still = await files_session.get(FileConflict, conflict_id)
    assert still is not None
    assert still.state == "open"
    refreshed = await files_session.get(FileNode, node_id)
    assert refreshed is not None
    assert refreshed.head_version_id == theirs_id
    assert await _history(files_session, node_id) == []


async def test_a_strangers_conflict_is_not_found(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    files_org_factory: object,
    repo: FilesRepo,
) -> None:
    """Another org's conflict id answers exactly as a random one does."""
    other = await files_org_factory()  # type: ignore[operator]
    other_factory = FilesFactory(files_session, other)
    _drive, _node, row, _theirs, _mine = await _conflicted(files_session, other_factory, org=other)
    clock = FakeClock(now=EPOCH)

    async with repo.transaction():
        assert await conflict_resolution.get_conflict(repo, row.id) is None
        assert await conflict_resolution.get_conflict(repo, uuid.uuid4()) is None

    for conflict_id in (row.id, uuid.uuid4()):
        with pytest.raises(NotFound):
            async with repo.transaction():
                await conflict_resolution.resolve_conflict(
                    repo, _ctx(files_org), conflict_id, choice="mine", if_match=None, clock=clock
                )
