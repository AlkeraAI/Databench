"""Two backends, one tree: the interleavings the single-statement writes must survive.

Every case here runs two real sessions on two real connections and forces the
interleaving through checkpoints — never a sleep — so the window between one
writer's read and its write is genuinely open when the other writer commits.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import errors
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, NodeId, OrgScope
from alkera_core.files.namespace import Namespace
from alkera_core.files.path_labels import label_ino
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.history import FileHistory
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


class _Backend:
    """One session, its repo and a Namespace on it — a second backend.

    The session takes a connection from the suite's pool. Every race below runs
    its two backends through ``asyncio.gather`` with both transactions open at
    once, so the pool cannot hand the same connection to both and serialize the
    race out of existence.
    """

    def __init__(
        self,
        org: FilesOrg,
        engine: AsyncEngine,
        checkpoints: PausingCheckpoints | None = None,
    ) -> None:
        self.session = AsyncSession(bind=engine, expire_on_commit=False)
        self.repo = FilesRepo(self.session, OrgScope(org_team_id=org.org_team_id))
        self.namespace = Namespace(
            self.repo,
            _ctx(org),
            FakeClock(now=EPOCH),
            None,
            **({"checkpoints": checkpoints} if checkpoints is not None else {}),
        )

    async def close(self) -> None:
        await self.session.close()


async def _parent_walk_holds(session: AsyncSession, drive_id: uuid.UUID) -> None:
    """Every node's ``path_ids`` is the walk of its ``parent_id``, and depth follows."""
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id, ino, parent_id, path_ids::text AS path_ids, depth "
                    "FROM file_nodes WHERE drive_id = :d"
                ),
                {"d": drive_id},
            )
        )
        .mappings()
        .all()
    )
    by_id = {row["id"]: row for row in rows}
    for row in rows:
        walked: list[uuid.UUID] = []
        cursor: Any = row
        while cursor is not None:
            walked.append(cursor["id"])
            cursor = by_id.get(cursor["parent_id"]) if cursor["parent_id"] else None
        walked.reverse()
        labels = [label_ino(label) for label in row["path_ids"].split(".")]
        assert labels == [by_id[id]["ino"] for id in walked], (
            f"path_ids for {row['id']} is not the parent walk"
        )
        assert row["depth"] == len(walked) - 1


async def test_two_renames_of_one_node_leave_one_winner(
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    files_engine: AsyncEngine,
) -> None:
    """Both read the same etag; the conditional UPDATE lets exactly one through."""
    drive = await files_factory.drive()
    made = await files_factory.tree("doc.txt", drive=drive)
    node_id = NodeId(made["doc.txt"].id)
    etag = made["doc.txt"].etag

    left, right = _Backend(files_org, files_engine), _Backend(files_org, files_engine)

    async def rename(backend: _Backend, name: bytes) -> object:
        try:
            async with backend.repo.transaction():
                return await backend.namespace.rename(node_id, name, if_match=etag)
        except Exception as exc:
            return exc

    try:
        outcomes = await asyncio.gather(rename(left, b"left.txt"), rename(right, b"right.txt"))
    finally:
        await left.close()
        await right.close()

    failures = [item for item in outcomes if isinstance(item, Exception)]
    winners = [item for item in outcomes if not isinstance(item, Exception)]
    assert len(winners) == 1, outcomes
    assert len(failures) == 1
    assert isinstance(failures[0], errors.PreconditionFailed)

    rows = (
        (await files_session.execute(select(FileHistory).where(FileHistory.node_id == node_id)))
        .scalars()
        .all()
    )
    assert [row.kind for row in rows] == ["rename"]
    row = (
        await files_session.execute(
            text("SELECT name, etag FROM file_nodes WHERE id = :id"), {"id": node_id}
        )
    ).one()
    assert row.name in (b"left.txt", b"right.txt")
    assert row.etag == etag + 1


async def test_two_moves_that_would_form_a_cycle_leave_exactly_one(
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    files_engine: AsyncEngine,
) -> None:
    """A moves under B while B moves under A: the loser is refused, not a loop."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ b/", drive=drive)
    a, b = made["a"], made["b"]

    checkpoints = PausingCheckpoints()
    left = _Backend(files_org, files_engine, checkpoints)
    right = _Backend(files_org, files_engine, checkpoints)

    async def move(backend: _Backend, node: Any, parent: Any) -> object:
        try:
            async with backend.repo.transaction():
                return await backend.namespace.move(
                    NodeId(node.id), NodeId(parent.id), if_match=node.etag
                )
        except Exception as exc:
            return exc

    # Both readers see the pre-move tree before either write lands.
    checkpoints.pause("namespace.before_move_update")
    try:
        first = asyncio.create_task(move(left, a, b))
        second = asyncio.create_task(move(right, b, a))
        await checkpoints.wait_paused("namespace.before_move_update")
        checkpoints.release("namespace.before_move_update")
        outcomes = await asyncio.gather(first, second)
    finally:
        await left.close()
        await right.close()

    refusals = [item for item in outcomes if isinstance(item, Exception)]
    winners = [item for item in outcomes if not isinstance(item, Exception)]
    assert len(winners) == 1, outcomes
    assert len(refusals) == 1
    assert isinstance(refusals[0], errors.Conflict)
    assert refusals[0].code == "files.cycle"

    await _parent_walk_holds(files_session, drive.id)


async def test_a_move_and_a_rename_of_the_same_node_do_not_both_commit(
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    files_engine: AsyncEngine,
) -> None:
    """The negative twin: two different mutations on one etag, still one winner."""
    drive = await files_factory.drive()
    made = await files_factory.tree("home/ home/f.txt dst/", drive=drive)
    node = made["home/f.txt"]
    destination = made["dst"]

    left, right = _Backend(files_org, files_engine), _Backend(files_org, files_engine)

    async def do_move() -> object:
        try:
            async with left.repo.transaction():
                return await left.namespace.move(
                    NodeId(node.id), NodeId(destination.id), if_match=node.etag
                )
        except Exception as exc:
            return exc

    async def do_rename() -> object:
        try:
            async with right.repo.transaction():
                return await right.namespace.rename(
                    NodeId(node.id), b"renamed.txt", if_match=node.etag
                )
        except Exception as exc:
            return exc

    try:
        outcomes = await asyncio.gather(do_move(), do_rename())
    finally:
        await left.close()
        await right.close()

    winners = [item for item in outcomes if not isinstance(item, Exception)]
    assert len(winners) == 1, outcomes
    etag = (
        await files_session.execute(
            text("SELECT etag FROM file_nodes WHERE id = :id"), {"id": node.id}
        )
    ).scalar_one()
    assert etag == node.etag + 1
    await _parent_walk_holds(files_session, drive.id)


async def test_two_creates_of_one_name_leave_one_row(
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    files_engine: AsyncEngine,
) -> None:
    """The partial unique index — not a pre-check — settles the race."""
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    root_id = NodeId(drive.root_node_id)

    left, right = _Backend(files_org, files_engine), _Backend(files_org, files_engine)

    async def create(backend: _Backend) -> object:
        try:
            async with backend.repo.transaction():
                return await backend.namespace.create(
                    DriveId(drive.id), root_id, "file", b"once.txt", conflict="fail"
                )
        except Exception as exc:
            return exc

    try:
        outcomes = await asyncio.gather(create(left), create(right))
    finally:
        await left.close()
        await right.close()

    winners = [item for item in outcomes if not isinstance(item, Exception)]
    assert len(winners) == 1, outcomes
    count = (
        await files_session.execute(
            text(
                "SELECT count(*) FROM file_nodes WHERE parent_id = :p AND name = :n "
                "AND trashed_at IS NULL"
            ),
            {"p": root_id, "n": b"once.txt"},
        )
    ).scalar_one()
    assert count == 1


async def test_a_move_after_an_ancestor_moved_rewrites_the_subtree_it_has_now(
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
    files_engine: AsyncEngine,
) -> None:
    """``x`` moves under ``p`` after ``a`` (its parent) moved under ``b``: ``x``'s children follow.

    An ancestor's move rewrites every descendant's ``path_ids`` without touching
    its etag, so the late mover's etag still matches and its move goes through.
    The subtree rewrite has to key on the path ``x`` has under the drive lock,
    not the one the mover read before it — or every child of ``x`` keeps a path
    no ancestor carries, and a later move of ``x`` into one of those children
    walks straight past the cycle guard.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/x/ a/x/c/ a/x/c/f.txt b/ p/", drive=drive)
    a, x, c, b, p = made["a"], made["a/x"], made["a/x/c"], made["b"], made["p"]

    checkpoints = PausingCheckpoints()
    late = _Backend(files_org, files_engine, checkpoints)
    early = _Backend(files_org, files_engine)

    async def move(backend: _Backend, node: Any, parent: Any, etag: int) -> Any:
        async with backend.repo.transaction():
            return await backend.namespace.move(NodeId(node.id), NodeId(parent.id), if_match=etag)

    # The late mover reads x's path, then stops short of the drive lock while
    # the early mover re-roots x's whole branch and commits.
    checkpoints.pause("namespace.before_move_lock")
    try:
        pending = asyncio.create_task(move(late, x, p, x.etag))
        await checkpoints.wait_paused("namespace.before_move_lock")
        await move(early, a, b, a.etag)
        checkpoints.release("namespace.before_move_lock")
        moved = await pending
        assert moved.parent_id == p.id

        await _parent_walk_holds(files_session, drive.id)

        # The harm a stale rewrite would have let through: x into its own child.
        with pytest.raises(errors.Conflict) as refused:
            await move(early, x, c, int(moved.etag))
        assert refused.value.code == "files.cycle"
    finally:
        await late.close()
        await early.close()


async def test_a_stale_rename_onto_a_taken_name_is_told_about_its_etag_first(
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_engine: AsyncEngine,
) -> None:
    """The contract: a caller holding a stale etag is refused for that, whatever
    name it asked for; only a current caller hears that the name is taken."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a.txt b.txt", drive=drive)
    a, b = made["a.txt"], made["b.txt"]
    backend = _Backend(files_org, files_engine)
    try:
        with pytest.raises(errors.PreconditionFailed):
            async with backend.repo.transaction():
                await backend.namespace.rename(NodeId(a.id), b.name, if_match=a.etag + 7)
        with pytest.raises(errors.Conflict) as taken:
            async with backend.repo.transaction():
                await backend.namespace.rename(NodeId(a.id), b.name, if_match=a.etag)
        assert taken.value.code == "files.exists"
    finally:
        await backend.close()
