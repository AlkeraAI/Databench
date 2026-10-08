"""Trash, restore and purge against real Postgres."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import errors
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, NodeId, TrashOpId
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.scoped import DomainStore
from alkera_core.files.trash import Trash
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import FilesFactory, FilesOrg

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


def _trash(repo: FilesRepo, org: FilesOrg, clock: FakeClock, **kwargs: Any) -> Trash:
    return Trash(repo, _ctx(org), clock, None, **kwargs)


class RecordingStore:
    """A ``DomainStore`` that fails the test if anything asks it for bytes.

    Purge must leave every object to the reachability sweep, so the proof that
    it does is a store handle that records the calls it never receives.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        async def record(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return None

        return record


async def _row(session: AsyncSession, node_id: uuid.UUID) -> dict[str, Any] | None:
    result = await session.execute(text("SELECT * FROM file_nodes WHERE id = :id"), {"id": node_id})
    row = result.mappings().first()
    return None if row is None else dict(row)


async def _trash_ops(session: AsyncSession, drive_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM file_trash_ops WHERE drive_id = :d"), {"d": drive_id}
            )
        ).scalar_one()
    )


async def _seed(factory: FilesFactory) -> tuple[FileDrive, dict[str, FileNode]]:
    drive = await factory.drive()
    tree = await factory.tree(
        "papers/ papers/sub/ papers/sub/report.pdf papers/notes.txt", drive=drive
    )
    return drive, tree


async def _live_children(session: AsyncSession, parent_id: uuid.UUID) -> list[bytes]:
    rows = await session.execute(
        text(
            "SELECT name FROM file_nodes WHERE parent_id = :p AND trashed_at IS NULL ORDER BY name"
        ),
        {"p": parent_id},
    )
    return [row[0] for row in rows]


async def test_trash_marks_the_whole_subtree_in_one_statement(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """One UPDATE stamps the folder and everything under it.

    The count is the point: a walk would be right and unusably slow, so the
    statement count is part of the contract, not an implementation detail.
    """
    _, tree = await _seed(files_factory)
    docs = tree["papers"]
    statements: list[str] = []

    def spy(conn: Any, cursor: Any, sql: str, *args: Any) -> None:
        if "UPDATE file_nodes" in sql:
            statements.append(sql)

    async with repo.transaction():
        engine = repo.session.get_bind()
        event.listen(engine, "before_cursor_execute", spy)
        try:
            await _trash(repo, files_org, clock).trash(NodeId(docs.id), if_match=docs.etag)
        finally:
            event.remove(engine, "before_cursor_execute", spy)

    assert len(statements) == 1
    for node in tree.values():
        row = await _row(repo.session, node.id)
        assert row is not None
        assert row["trashed_at"] is not None
        assert row["trash_op_id"] is not None


async def test_a_trashed_subtree_leaves_the_listing_and_becomes_one_trash_root(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive, tree = await _seed(files_factory)
    docs = tree["papers"]
    assert drive.root_node_id is not None

    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(NodeId(docs.id), if_match=docs.etag)

    assert await _live_children(repo.session, drive.root_node_id) == []
    async with repo.transaction():
        page = await _trash(repo, files_org, clock).list_trash(DriveId(drive.id))
    assert [entry.node.id for entry in page.entries] == [docs.id]
    assert page.entries[0].original_parent_id == drive.root_node_id
    assert timedelta(days=29) < page.entries[0].time_left <= timedelta(days=30)


@pytest.mark.xfail(
    strict=True,
    reason="Namespace.create does not yet refuse a trashed parent; that check belongs to "
    "the namespace lane, which owns the file. Strict so the day it is fixed this test "
    "turns green loudly instead of rotting.",
)
async def test_a_trashed_folder_cannot_receive_a_child(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A trashed parent is not a live parent, so a create into it is refused."""
    drive, tree = await _seed(files_factory)
    docs = tree["papers"]
    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(NodeId(docs.id), if_match=docs.etag)

    namespace = Namespace(repo, _ctx(files_org), clock, None)
    with pytest.raises(errors.FilesError):
        async with repo.transaction():
            await namespace.create(DriveId(drive.id), NodeId(docs.id), "file", b"new.txt")


async def test_trash_reaches_a_child_created_after_its_folder_moved(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A node created under a folder the last move carried is swept with it.

    The sweep is one ``UPDATE`` over the root's path prefix, so a subtree is
    stamped whole only while every node in it really carries that path. The
    move rewrote the whole subtree's paths with a statement the mapper never
    saw, so a create that took its parent's path from the instance the session
    was already holding puts the new node on a path the sweep walks past — and
    the folder ends up trashed with a live node standing inside it.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("src/ src/pkg/ src/pkg/inner/ dst/", drive=drive)
    moving, inner, destination = tree["src/pkg"], tree["src/pkg/inner"], tree["dst"]
    namespace = Namespace(repo, _ctx(files_org), clock, None)

    async with repo.transaction():
        await namespace.move(NodeId(moving.id), NodeId(destination.id), if_match=moving.etag)
    async with repo.transaction():
        child = await namespace.create(
            DriveId(drive.id), NodeId(inner.id), "file", b"mod.py", conflict="fail"
        )
    folder = await _row(repo.session, moving.id)
    assert folder is not None
    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(NodeId(moving.id), if_match=folder["etag"])

    swept = await _row(repo.session, moving.id)
    left = await _row(repo.session, child.id)
    assert swept is not None and swept["trashed_at"] is not None
    assert left is not None
    assert left["trashed_at"] is not None, "a live node was left inside a trashed folder"


async def test_trash_refuses_a_stale_etag_and_changes_nothing(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive, tree = await _seed(files_factory)
    drive_id, docs_id, etag = drive.id, tree["papers"].id, tree["papers"].etag
    with pytest.raises(errors.PreconditionFailed):
        async with repo.transaction():
            await _trash(repo, files_org, clock).trash(NodeId(docs_id), if_match=etag + 7)

    row = await _row(repo.session, docs_id)
    assert row is not None and row["trashed_at"] is None
    assert await _trash_ops(repo.session, drive_id) == 0


@pytest.mark.parametrize(
    ("state", "held_version"),
    [
        pytest.param("locked", False, id="locked-node"),
        pytest.param("live", True, id="held-version"),
    ],
)
async def test_trash_and_purge_refuse_a_held_subtree(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    state: str,
    held_version: bool,
) -> None:
    """A hold anywhere under the node refuses the whole operation."""
    _, tree = await _seed(files_factory)
    leaf = tree["papers/sub/report.pdf"]
    if state != "live":
        await repo.session.execute(
            text("UPDATE file_nodes SET state = :s WHERE id = :id"), {"s": state, "id": leaf.id}
        )
    if held_version:
        await repo.session.execute(
            text(
                "INSERT INTO file_versions (id, org_team_id, node_id, seq, size_bytes, "
                "content_hash, source, held, keep_forever, lease_epoch, metadata) VALUES "
                "(:id, :org, :node, 1, 4, 'h', 'upload', true, false, 0, '{}'::jsonb)"
            ),
            {"id": uuid.uuid4(), "org": files_org.org_team_id, "node": leaf.id},
        )
    await repo.session.commit()

    docs_id, etag, leaf_id = tree["papers"].id, tree["papers"].etag, leaf.id
    with pytest.raises(errors.Conflict) as trashing:
        async with repo.transaction():
            await _trash(repo, files_org, clock).trash(NodeId(docs_id), if_match=etag)
    assert trashing.value.code == "files.held"

    with pytest.raises(errors.Conflict) as purging:
        async with repo.transaction():
            await _trash(repo, files_org, clock).purge(NodeId(docs_id))
    assert purging.value.code == "files.held"
    assert await _row(repo.session, leaf_id) is not None


async def test_a_legal_hold_stops_the_purge_and_not_the_trash(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
) -> None:
    """A held subtree is still deletable — reversibly — and never permanently.

    A legal hold preserves evidence, so it answers the operation that destroys
    the row and no other. Trashing loses nothing: the nodes keep their ids,
    their versions and the hold itself, and the restore below puts them back.
    Refusing the trash instead would let one matter reference make a folder
    permanently undeletable for the whole org, which is what this pins.
    """
    drive, tree = await _seed(files_factory)
    docs, leaf = tree["papers"], tree["papers/sub/report.pdf"]
    # Held as plain ids: a rolled-back purge below expires the instances, and
    # touching an attribute then is a lazy refresh outside the greenlet.
    root_id, docs_id, docs_etag, leaf_id = (
        drive.root_node_id,
        docs.id,
        docs.etag,
        leaf.id,
    )
    await repo.session.execute(
        text(DIRECT_SQL["file_holds_live"]),
        {
            "row": uuid.uuid4(),
            "org": files_org.org_team_id,
            "node": leaf_id,
            "actor": files_org.admin_id,
        },
    )
    await repo.session.commit()

    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(docs_id), if_match=docs_etag)
    op_id = TrashOpId(op.id)
    trashed = await _row(repo.session, leaf_id)
    assert trashed is not None and trashed["trashed_at"] is not None

    async with repo.transaction():
        back = await _trash(repo, files_org, clock).restore(op_id)
    assert back.id == docs_id and back.parent_id == root_id
    restored = await _row(repo.session, leaf_id)
    assert restored is not None
    assert restored["trashed_at"] is None and restored["trash_op_id"] is None

    # The hold rode the whole round trip, and the permanent deletion is the
    # one it answers.
    held = await repo.session.execute(
        text("SELECT count(*) FROM file_holds WHERE node_id = :id AND released_at IS NULL"),
        {"id": leaf_id},
    )
    assert held.scalar_one() == 1
    with pytest.raises(errors.Conflict) as purging:
        async with repo.transaction():
            await _trash(repo, files_org, clock).purge(NodeId(docs_id))
    assert purging.value.code == "files.held"
    assert await _row(repo.session, leaf_id) is not None


async def test_restore_brings_every_node_back_with_its_name(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive, tree = await _seed(files_factory)
    docs = tree["papers"]
    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(docs.id), if_match=docs.etag)
    async with repo.transaction():
        back = await _trash(repo, files_org, clock).restore(TrashOpId(op.id))

    assert back.id == docs.id and back.parent_id == drive.root_node_id
    for path, node in tree.items():
        row = await _row(repo.session, node.id)
        assert row is not None, path
        assert row["trashed_at"] is None and row["trash_op_id"] is None
        assert row["name"] == node.name
    async with repo.transaction():
        page = await _trash(repo, files_org, clock).list_trash(DriveId(drive.id))
    assert page.entries == ()


async def test_restore_onto_an_occupied_name_applies_the_conflict_rename(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The name is taken again by the time the user restores; nothing is lost."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("report.pdf", drive=drive)
    original = tree["report.pdf"]
    assert drive.root_node_id is not None

    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(original.id), if_match=original.etag)
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    async with repo.transaction():
        await namespace.create(DriveId(drive.id), NodeId(drive.root_node_id), "file", b"report.pdf")
    async with repo.transaction():
        back = await _trash(repo, files_org, clock).restore(TrashOpId(op.id))

    assert back.name == b"report (1).pdf"
    assert await _live_children(repo.session, drive.root_node_id) == [
        b"report (1).pdf",
        b"report.pdf",
    ]


async def test_restore_into_a_vanished_parent_lands_in_the_drive_root(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The original folder was purged while the file sat in the trash."""
    drive, tree = await _seed(files_factory)
    leaf = tree["papers/notes.txt"]
    assert drive.root_node_id is not None

    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(leaf.id), if_match=leaf.etag)
    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(
            NodeId(tree["papers"].id), if_match=tree["papers"].etag
        )
    async with repo.transaction():
        back = await _trash(repo, files_org, clock).restore(TrashOpId(op.id))

    assert back.parent_id == drive.root_node_id
    assert b"notes.txt" in await _live_children(repo.session, drive.root_node_id)


async def test_restore_prefers_a_live_ancestor_over_the_actors_home(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """Negative twin of the home landing: a surviving ancestor outranks home."""
    drive, tree = await _seed(files_factory)
    assert drive.root_node_id is not None
    await files_factory.tree(f"home/ home/{files_org.admin_id}/", drive=drive)
    leaf = tree["papers/notes.txt"]

    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(leaf.id), if_match=leaf.etag)
    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(
            NodeId(tree["papers"].id), if_match=tree["papers"].etag
        )
    async with repo.transaction():
        back = await _trash(repo, files_org, clock).restore(TrashOpId(op.id))

    assert back.parent_id == drive.root_node_id


async def test_restore_falls_back_to_home_when_no_ancestor_survives(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """Only with every folder on the original path gone does home take the tree."""
    drive, tree = await _seed(files_factory)
    assert drive.root_node_id is not None
    homes = await files_factory.tree(f"home/ home/{files_org.admin_id}/", drive=drive)
    leaf = tree["papers/notes.txt"]

    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(leaf.id), if_match=leaf.etag)
    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(
            NodeId(tree["papers"].id), if_match=tree["papers"].etag
        )
        # The drive root itself is stamped, which no verb does — this is the
        # last-resort arm, reachable only when the whole path is unusable.
        await repo.session.execute(
            text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id AND org_team_id = :org"),
            {"id": drive.root_node_id, "org": files_org.org_team_id},
        )
    async with repo.transaction():
        back = await _trash(repo, files_org, clock).restore(TrashOpId(op.id))

    assert back.parent_id == homes[f"home/{files_org.admin_id}"].id


async def test_restore_reattaches_under_the_nearest_live_ancestor(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A file trashed under a folder that is itself trashed comes back one level up."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("a/ a/b/ a/b/c.txt", drive=drive)
    a, b, c = tree["a"], tree["a/b"], tree["a/b/c.txt"]

    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(c.id), if_match=c.etag)
    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(NodeId(b.id), if_match=b.etag)
    async with repo.transaction():
        back = await _trash(repo, files_org, clock).restore(TrashOpId(op.id))

    assert back.parent_id == a.id
    assert back.name == b"c.txt"
    assert await _live_children(repo.session, a.id) == [b"c.txt"]


async def test_restore_conflict_renames_at_the_nearest_live_ancestor(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The ancestor it falls back to already holds the name: keep both."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("a/ a/b/ a/b/c.txt", drive=drive)
    a, b, c = tree["a"], tree["a/b"], tree["a/b/c.txt"]

    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(c.id), if_match=c.etag)
    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(NodeId(b.id), if_match=b.etag)
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    async with repo.transaction():
        await namespace.create(DriveId(drive.id), NodeId(a.id), "file", b"c.txt")
    async with repo.transaction():
        back = await _trash(repo, files_org, clock).restore(TrashOpId(op.id))

    assert back.parent_id == a.id
    assert back.name == b"c (1).txt"


async def test_restore_reaches_the_drive_root_when_the_whole_branch_is_trashed(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """b's own ancestor a is trashed too, so the nearest survivor is the root."""
    drive = await files_factory.drive()
    tree = await files_factory.tree("a/ a/b/ a/b/c.txt", drive=drive)
    a, b = tree["a"], tree["a/b"]
    assert drive.root_node_id is not None

    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(b.id), if_match=b.etag)
    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(NodeId(a.id), if_match=a.etag)
    async with repo.transaction():
        back = await _trash(repo, files_org, clock).restore(TrashOpId(op.id))

    assert back.parent_id == drive.root_node_id
    assert back.name == b"b"
    # c.txt rode along inside b's op and is live again under it.
    assert await _live_children(repo.session, NodeId(back.id)) == [b"c.txt"]


async def test_restore_never_lands_a_subtree_inside_a_trashed_folder(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The ancestor walk believes the rows, not the chain the session already read.

    Trashing the branch stamps every folder above the leaf with a statement the
    mapper never sees, so the ancestors the session is holding still look live.
    A walk that took them at their word would hand the restore a parent that is
    in the trash — and the re-parent into it is refused, so the caller is told
    its subtree could not be restored at all.
    """
    drive = await files_factory.drive()
    tree = await files_factory.tree("a/ a/a/ a/a/a/", drive=drive)
    outer, deepest = tree["a"], tree["a/a/a"]
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    assert drive.root_node_id is not None

    async with repo.transaction():
        leaf = await namespace.create(DriveId(drive.id), NodeId(deepest.id), "file", b"a")
    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(leaf.id), if_match=leaf.etag)
    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(NodeId(outer.id), if_match=outer.etag)
    async with repo.transaction():
        back = await _trash(repo, files_org, clock).restore(TrashOpId(op.id))

    assert back.parent_id == drive.root_node_id
    landed = await _row(repo.session, back.id)
    parent = await _row(repo.session, drive.root_node_id)
    assert landed is not None and landed["trashed_at"] is None
    assert parent is not None and parent["trashed_at"] is None


async def test_restore_into_an_explicit_parent(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive, tree = await _seed(files_factory)
    other = (await files_factory.tree("elsewhere/", drive=drive))["elsewhere"]
    leaf = tree["papers/notes.txt"]

    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(leaf.id), if_match=leaf.etag)
    async with repo.transaction():
        back = await _trash(repo, files_org, clock).restore(
            TrashOpId(op.id), parent_id=NodeId(other.id)
        )
    assert back.parent_id == other.id
    assert await _live_children(repo.session, other.id) == [b"notes.txt"]


async def test_restore_elsewhere_renames_against_the_folder_it_was_trashed_from(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The name is free at the destination but taken back at the source.

    Restore un-trashes the root while it still stands in the folder it was
    trashed from and only re-parents afterwards, so a candidate picked from the
    destination's siblings alone hits the live-sibling index at the source.
    """
    drive = await files_factory.drive()
    original = (await files_factory.tree("report.pdf", drive=drive))["report.pdf"]
    other = (await files_factory.tree("elsewhere/", drive=drive))["elsewhere"]
    assert drive.root_node_id is not None

    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(original.id), if_match=original.etag)
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    async with repo.transaction():
        await namespace.create(DriveId(drive.id), NodeId(drive.root_node_id), "file", b"report.pdf")

    async with repo.transaction():
        back = await _trash(repo, files_org, clock).restore(
            TrashOpId(op.id), parent_id=NodeId(other.id)
        )

    assert back.name == b"report (1).pdf"
    assert back.parent_id == other.id
    assert await _live_children(repo.session, other.id) == [b"report (1).pdf"]
    # The name that took the slot back is untouched and still live.
    assert await _live_children(repo.session, drive.root_node_id) == [
        b"elsewhere",
        b"report.pdf",
    ]


async def test_purge_removes_the_rows_and_never_calls_the_store(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive, tree = await _seed(files_factory)
    docs = tree["papers"]
    store = RecordingStore()
    purger = Trash(repo, _ctx(files_org), clock, cast_store(store))

    async with repo.transaction():
        await purger.trash(NodeId(docs.id), if_match=docs.etag)
    async with repo.transaction():
        await purger.purge(NodeId(docs.id))

    for node in tree.values():
        assert await _row(repo.session, node.id) is None
    assert await _trash_ops(repo.session, drive.id) == 0
    assert store.calls == []


async def test_trashed_bytes_still_count_and_a_round_trip_nets_to_zero(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """Quota accounting: a round trip nets to zero and nothing is added twice.

    The node here is headless — ``create`` makes the row, the head swap lands
    the bytes — so a recount over ``file_versions`` counts neither its declared
    size nor it as a file, and the aggregate must agree: only the folder's own
    entry count moves. Trash and restore then mirror each other exactly.
    """
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    async with repo.transaction():
        from alkera_core.files.namespace import NodeAttrs

        made = await namespace.create(
            DriveId(drive.id),
            NodeId(drive.root_node_id),
            "file",
            b"big.bin",
            attrs=NodeAttrs(size=4096),
        )

    async def totals() -> tuple[int, int, int]:
        row = (
            await repo.session.execute(
                text(
                    "SELECT coalesce(sum(bytes_delta), 0), coalesce(sum(files_delta), 0), "
                    "coalesce(sum(direct_children_delta), 0) "
                    "FROM file_dir_stats_deltas WHERE node_id = :p"
                ),
                {"p": drive.root_node_id},
            )
        ).one()
        return int(row[0]), int(row[1]), int(row[2])

    async def live_bytes() -> int:
        return int(
            (
                await repo.session.execute(
                    text("SELECT coalesce(sum(size), 0) FROM file_nodes WHERE drive_id = :d"),
                    {"d": drive.id},
                )
            ).scalar_one()
        )

    after_create = await totals()
    assert after_create == (0, 0, 1), "a headless node contributes 0 bytes and 0 files"
    bytes_before = await live_bytes()

    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(made.id), if_match=made.etag)
    assert await totals() == (0, 0, 0)
    assert await live_bytes() == bytes_before

    async with repo.transaction():
        await _trash(repo, files_org, clock).restore(TrashOpId(op.id))
    assert await totals() == after_create
    assert await live_bytes() == bytes_before


async def test_time_left_is_computed_from_postgres_now(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A planted ``purge_after`` decides the answer, not the process clock.

    The injected clock is moved a year forward and the answer does not move: the
    deadline is the database's, which is the only clock every reader shares.
    """
    drive, tree = await _seed(files_factory)
    docs = tree["papers"]
    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(NodeId(docs.id), if_match=docs.etag)

    await repo.session.execute(
        text("UPDATE file_trash_ops SET purge_after = now() + interval '90 minutes' WHERE id = :i"),
        {"i": op.id},
    )
    await repo.session.commit()
    clock.advance(timedelta(days=365))

    async with repo.transaction():
        page = await _trash(repo, files_org, clock).list_trash(DriveId(drive.id))
    left = page.entries[0].time_left
    assert timedelta(minutes=89) < left <= timedelta(minutes=90)


async def test_the_trash_lists_the_newest_deletion_first_and_pages_past_it(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The order is what a person just deleted, and the marker walks backwards.

    Seven roots are trashed one at a time, so each carries a later
    ``deleted_at`` than the one before. The first page must open on the last
    one deleted; feeding the marker back must reach the first one, which is the
    only way an older deletion stays reachable once a drive holds more trash
    than one page.
    """
    drive, _tree = await _seed(files_factory)
    names_in_order: list[str] = []
    for index in range(7):
        name = f"sweep-{index}.txt"
        node = (await files_factory.tree(name, drive=drive))[name]
        async with repo.transaction():
            await _trash(repo, files_org, clock).trash(NodeId(node.id), if_match=node.etag)
        names_in_order.append(name)

    seen: list[str] = []
    marker: str | None = None
    for _page in range(4):
        async with repo.transaction():
            page = await _trash(repo, files_org, clock).list_trash(
                DriveId(drive.id), marker=marker, limit=3
            )
        seen.extend(entry.node.name.decode() for entry in page.entries)
        marker = page.next_marker
        if marker is None:
            break

    assert marker is None
    # Newest first, every root exactly once, none skipped by the cursor.
    assert [name for name in seen if name.startswith("sweep-")] == list(reversed(names_in_order))


async def test_the_trash_refuses_a_marker_it_did_not_write(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A cursor from nowhere is a refusal, not a silent rewind to page one."""
    drive, _tree = await _seed(files_factory)
    with pytest.raises(errors.InvalidRequest):
        async with repo.transaction():
            await _trash(repo, files_org, clock).list_trash(
                DriveId(drive.id), marker=str(uuid.uuid4())
            )


async def test_empty_purges_every_trashed_root(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    drive, tree = await _seed(files_factory)
    extra = (await files_factory.tree("loose.txt", drive=drive))["loose.txt"]
    async with repo.transaction():
        service = _trash(repo, files_org, clock)
        await service.trash(NodeId(tree["papers"].id), if_match=tree["papers"].etag)
        await service.trash(NodeId(extra.id), if_match=extra.etag)

    async with repo.transaction():
        removed = await _trash(repo, files_org, clock).empty(DriveId(drive.id))
    assert removed == 2
    remaining = (
        await repo.session.execute(
            text("SELECT count(*) FROM file_nodes WHERE drive_id = :d"), {"d": drive.id}
        )
    ).scalar_one()
    assert remaining == 1


async def _versions(session: AsyncSession, node: FileNode, count: int) -> list[uuid.UUID]:
    """``count`` versions on ``node``, the newest of them its head.

    Written straight against the ORM because the shape purge meets in
    production is exactly this — a node whose ``head_version_id`` points into
    ``file_versions`` — and no library call is needed to produce it.
    """
    made: list[uuid.UUID] = []
    for seq in range(1, count + 1):
        version = FileVersion(
            id=uuid.uuid4(),
            org_team_id=node.org_team_id,
            node_id=node.id,
            seq=seq,
            size_bytes=8,
            content_hash=f"hash-{node.id}-{seq}",
            source="upload",
        )
        session.add(version)
        made.append(version.id)
    await session.flush()
    node.head_version_id = made[-1]
    await session.commit()
    return made


async def test_purge_removes_a_node_that_still_has_a_head_version(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """The head pointer is dropped before the versions it points at are.

    ``file_nodes.head_version_id`` references ``file_versions``, so deleting
    the version rows first leaves the node pointing at nothing and Postgres
    refuses the whole purge. Every file that was ever written has a head, which
    made purge unusable for all of them.
    """
    drive, tree = await _seed(files_factory)
    leaf = tree["papers/notes.txt"]
    version_ids = await _versions(repo.session, leaf, 3)
    store = RecordingStore()

    async with repo.transaction():
        purger = Trash(repo, _ctx(files_org), clock, cast_store(store))
        await purger.trash(NodeId(tree["papers"].id), if_match=tree["papers"].etag)
    async with repo.transaction():
        await Trash(repo, _ctx(files_org), clock, cast_store(store)).purge(
            NodeId(tree["papers"].id)
        )

    for node in tree.values():
        assert await _row(repo.session, node.id) is None
    surviving = (
        await repo.session.execute(
            text("SELECT count(*) FROM file_versions WHERE id = ANY(:ids)"), {"ids": version_ids}
        )
    ).scalar_one()
    assert surviving == 0
    assert await _trash_ops(repo.session, drive.id) == 0
    # The bytes are the reachability sweep's to move, never purge's.
    assert store.calls == []


async def test_purge_detaches_an_operation_that_named_the_node_as_its_result(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A finished operation outlives the node it produced, with a null result.

    ``file_ops.result_node_id`` points at ``file_nodes``, so a copy or an
    upload that produced the purged node made the purge fail with a
    foreign-key violation — an unhandled 500 out of
    ``DELETE …?permanent=true``. The operation row is the record of what
    happened and must survive; only its pointer goes.
    """
    drive, tree = await _seed(files_factory)
    leaf = tree["papers/notes.txt"]
    op_id = uuid.uuid4()
    repo.session.add(
        FileOp(
            id=op_id,
            org_team_id=files_org.org_team_id,
            drive_id=drive.id,
            kind="copy",
            actor=files_org.admin_id,
            state="done",
            result_node_id=leaf.id,
        )
    )
    await repo.session.commit()

    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(NodeId(leaf.id), if_match=leaf.etag)
    async with repo.transaction():
        await _trash(repo, files_org, clock).purge(NodeId(leaf.id))

    assert await _row(repo.session, leaf.id) is None
    row = (
        (
            await repo.session.execute(
                text("SELECT state, result_node_id FROM file_ops WHERE id = :i"), {"i": op_id}
            )
        )
        .mappings()
        .one()
    )
    assert row["state"] == "done"
    assert row["result_node_id"] is None


# ---- every foreign key into the purged subtree -------------------------------
#
# A key nobody decided about is an unhandled ``ForeignKeyViolationError`` — a
# 500 out of ``DELETE …?permanent=true`` — for every user who happened to be in
# that state. The table below is the executable copy of the decision table in
# ``trash.py``'s docstring: one planter per foreign key, and the outcome the
# module promises.

DIRECT_SQL: dict[str, str] = {
    "file_holds_live": (
        "INSERT INTO file_holds (id, org_team_id, scope, node_id, matter_ref, placed_by) "
        "VALUES (:row, :org, 'subtree', :node, 'matter-1', :actor)"
    ),
    "file_holds_released": (
        "INSERT INTO file_holds (id, org_team_id, scope, node_id, matter_ref, placed_by, "
        "released_at) VALUES (:row, :org, 'subtree', :node, 'matter-1', :actor, now())"
    ),
    "file_leases": (
        "INSERT INTO file_leases (node_id, org_team_id, epoch, holder_principal_kind, "
        "holder_principal_id, holder_instance_id, machine_id, purpose, expires_at) "
        "VALUES (:node, :org, 1, 'user', :actor, 'inst-1', 'machine-1', 'mount', "
        "now() + interval '1 hour')"
    ),
    "file_lease_epoch_hwm": (
        "INSERT INTO file_lease_epoch_hwm (node_id, org_team_id, hwm) VALUES (:node, :org, 7)"
    ),
    "file_links": (
        "INSERT INTO file_links (id, org_team_id, node_id, token_hash, scope, role, params, "
        "hide_download) VALUES (:row, :org, :node, :hash, 'node', 'viewer', '{}'::jsonb, false)"
    ),
    "file_locks": (
        "INSERT INTO file_locks (id, org_team_id, node_id, holder_principal, kind, "
        "enforcement, token, expires_at) VALUES (:row, :org, :node, :actor, 'flock', "
        "'advisory', :hash, now() + interval '1 hour')"
    ),
    "file_upload_sessions_parent": (
        "INSERT INTO file_upload_sessions (id, org_team_id, drive_id, parent_id, name, "
        "dedup_domain_id, state, declared_size, bytes_received, transfer_mode, lease_epoch, "
        "quota_hold_bytes, quota_hold_nodes, expires_at) "
        "SELECT :row, :org, d.id, :node, 'pending.txt'::bytea, d.dedup_domain_id, 'open', "
        "64, 0, 'proxied', 0, 64, 1, now() + interval '1 hour' "
        "FROM file_drives d WHERE d.id = :drive"
    ),
    "file_content_grants_live": (
        "INSERT INTO file_content_grants (nonce, org_team_id, version_id, expires_at) "
        "VALUES (:nonce, :org, :version, now() + interval '1 hour')"
    ),
    "file_content_grants_expired": (
        "INSERT INTO file_content_grants (nonce, org_team_id, version_id, expires_at) "
        "VALUES (:nonce, :org, :version, now() - interval '1 hour')"
    ),
    "file_content_grants_used": (
        "INSERT INTO file_content_grants (nonce, org_team_id, version_id, expires_at, used_at) "
        "VALUES (:nonce, :org, :version, now() + interval '1 hour', now())"
    ),
    "file_stage_jobs": (
        "INSERT INTO file_leases (node_id, org_team_id, epoch, holder_principal_kind, "
        "holder_principal_id, holder_instance_id, machine_id, purpose, expires_at) "
        "VALUES (:node, :org, 1, 'user', :actor, 'inst-1', 'machine-1', 'mount', "
        "now() + interval '1 hour'); "
        "INSERT INTO file_stage_jobs (id, org_team_id, lease_node_id, machine_id, direction, "
        "mode, state, cursor, bytes_total, bytes_done, failed_count, quarantined_count, "
        "lease_epoch) VALUES (:row, :org, :node, 'machine-1', 'out', '', 'queued', "
        "'{}'::jsonb, 0, 0, 0, 0, 0)"
    ),
}

#: ``True`` where the planted state must refuse the purge outright.
REFUSES: frozenset[str] = frozenset({"file_holds_live", "file_content_grants_live"})

#: Where each planted row lives, so the test can prove it is gone (or still
#: there, on a refusal) without knowing how the purge got rid of it.
COUNT_SQL: dict[str, tuple[str, str]] = {
    "file_holds_live": ("file_holds", "node_id"),
    "file_holds_released": ("file_holds", "node_id"),
    "file_leases": ("file_leases", "node_id"),
    "file_lease_epoch_hwm": ("file_lease_epoch_hwm", "node_id"),
    "file_links": ("file_links", "node_id"),
    "file_locks": ("file_locks", "node_id"),
    "file_upload_sessions_parent": ("file_upload_sessions", "parent_id"),
    "file_stage_jobs": ("file_stage_jobs", "lease_node_id"),
}


@pytest.mark.parametrize("case", sorted(DIRECT_SQL))
async def test_purge_answers_every_foreign_key_into_the_subtree(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    case: str,
) -> None:
    """Each row that points into the subtree purges or refuses, never 500s.

    The purge names the folder, not the leaf the row hangs off, because that is
    the shape the trash sweeper drives: a root whose descendants carry the
    references. Before this, every one of these states raised
    ``ForeignKeyViolationError`` out of the delete.
    """
    drive, tree = await _seed(files_factory)
    docs, leaf = tree["papers"], tree["papers/notes.txt"]
    version_id = (await _versions(repo.session, leaf, 1))[0]
    # Read off the mapped rows now: a refused purge rolls its transaction back,
    # which expires every instance, and a lazy re-read outside a session is a
    # MissingGreenlet rather than the assertion the test is making.
    node_ids = [node.id for node in tree.values()]
    docs_id, docs_etag, leaf_id = docs.id, docs.etag, leaf.id
    params = {
        "row": uuid.uuid4(),
        "nonce": uuid.uuid4().hex,
        "hash": uuid.uuid4().hex,
        "org": files_org.org_team_id,
        "actor": files_org.admin_id,
        "node": leaf_id,
        "drive": drive.id,
        "version": version_id,
    }
    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(NodeId(docs_id), if_match=docs_etag)

    # Planted after the trash, not before: a legal hold refuses the trash too,
    # and the state under test here is the one the purge sweeper meets — an
    # already-trashed subtree that something still points into.
    for statement in DIRECT_SQL[case].split("; "):
        await repo.session.execute(text(statement), params)
    await repo.session.commit()

    if case in REFUSES:
        with pytest.raises(errors.Conflict) as refusal:
            async with repo.transaction():
                await _trash(repo, files_org, clock).purge(NodeId(docs_id))
        assert refusal.value.code == "files.held"
        assert await _row(repo.session, leaf_id) is not None
        return

    async with repo.transaction():
        await _trash(repo, files_org, clock).purge(NodeId(docs_id))

    for node_id in node_ids:
        assert await _row(repo.session, node_id) is None
    table, column = COUNT_SQL.get(case, ("file_content_grants", "version_id"))
    key = version_id if table == "file_content_grants" else leaf_id
    left = (
        await repo.session.execute(
            text(f"SELECT count(*) FROM {table} WHERE {column} = :k"),
            {"k": key},
        )
    ).scalar_one()
    assert left == 0


async def test_purge_detaches_a_shortcut_that_pointed_into_the_subtree(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A shortcut outside the subtree survives with a null target.

    ``file_nodes.target_id`` is a foreign key back into ``file_nodes``, so a
    shortcut anywhere else in the drive made the purge fail. The shortcut is
    not the user's to delete — they deleted the target — so it keeps its row.
    """
    drive, tree = await _seed(files_factory)
    docs, leaf = tree["papers"], tree["papers/notes.txt"]
    shortcut = (await files_factory.tree("link-to-notes", drive=drive))["link-to-notes"]
    await repo.session.execute(
        text("UPDATE file_nodes SET target_id = :t WHERE id = :i"),
        {"t": leaf.id, "i": shortcut.id},
    )
    await repo.session.commit()

    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(NodeId(docs.id), if_match=docs.etag)
    async with repo.transaction():
        await _trash(repo, files_org, clock).purge(NodeId(docs.id))

    assert await _row(repo.session, leaf.id) is None
    surviving = await _row(repo.session, shortcut.id)
    assert surviving is not None
    assert surviving["target_id"] is None


async def test_purge_refuses_the_drive_root(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """``file_drives.root_node_id`` is the one node no purge may take."""
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    root_id = drive.root_node_id
    with pytest.raises(errors.InvalidRequest):
        async with repo.transaction():
            await _trash(repo, files_org, clock).purge(NodeId(root_id))
    assert await _row(repo.session, root_id) is not None


def cast_store(store: RecordingStore) -> DomainStore:
    """The recording double, typed as the protocol the service takes."""
    return store  # type: ignore[return-value]


async def test_trashing_holds_the_bytes_and_purging_is_what_releases_them(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, clock: FakeClock
) -> None:
    """A trashed node's bytes are still in the store, so they still count.

    Subtracting them at trash lets a drive at its ceiling keep writing simply by
    moving files to the trash, for the whole purge window, while the store holds
    every byte. Only the folder's own entry count moves at trash; the bytes and
    the file count are charged back at purge, which is also the only place the
    drive can come back under its ceiling.
    """
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    tree = await files_factory.tree("big.bin", drive=drive)
    node = tree["big.bin"]
    await _versions(repo.session, node, 1)
    # The head swap is what puts the bytes on the node in production; the
    # aggregate counts that column, so the seeded node carries it too.
    node.size = 8
    await repo.session.commit()

    async def totals() -> tuple[int, int, int]:
        row = (
            await repo.session.execute(
                text(
                    "SELECT coalesce(sum(bytes_delta), 0), coalesce(sum(files_delta), 0), "
                    "coalesce(sum(direct_children_delta), 0) "
                    "FROM file_dir_stats_deltas WHERE node_id = :p"
                ),
                {"p": drive.root_node_id},
            )
        ).one()
        return int(row[0]), int(row[1]), int(row[2])

    before = await totals()
    node_id = NodeId(node.id)
    etag = int(node.etag)
    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(node_id, if_match=etag)
    # Only the child count left; the eight bytes of the head version did not.
    assert await totals() == (before[0], before[1], before[2] - 1)

    async with repo.transaction():
        await _trash(repo, files_org, clock).purge(node_id)
    # Purge is the release: the bytes and the file go, the child count does not
    # move again because the trash already took it.
    assert await totals() == (before[0] - 8, before[1] - 1, before[2] - 1)
    assert op.id is not None


async def test_a_restore_that_has_to_queue_its_re_parent_hands_back_the_operation(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An oversized re-parent is a queued operation, and the caller has to see it.

    Returning the node would report a restore that has not happened: the runner
    is started by the caller, so a swallowed operation is a subtree left under
    its old (trashed) parent and a ``file_ops`` row nobody ever picks up.
    """
    from alkera_core.files import namespace as namespace_module
    from alkera_core.files.ops import OperationState

    drive = await files_factory.drive()
    tree = await files_factory.tree("home/ home/inner/ home/inner/a.txt away/", drive=drive)
    # The original parent goes to the trash first, so the restore has to land
    # the subtree somewhere else — which is what makes it a move at all.
    folder = tree["home/inner"]
    async with repo.transaction():
        op = await _trash(repo, files_org, clock).trash(
            NodeId(folder.id), if_match=int(folder.etag)
        )
    home = tree["home"]
    async with repo.transaction():
        await _trash(repo, files_org, clock).trash(NodeId(home.id), if_match=int(home.etag))
    # Every subtree is oversized: the threshold, not a hundred thousand rows.
    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", 0)

    async with repo.transaction():
        handed = await _trash(repo, files_org, clock).restore(TrashOpId(op.id))

    assert isinstance(handed, OperationState), "the queued move was swallowed"
    assert handed.kind == "move"
    queued = (
        await repo.session.execute(
            text("SELECT count(*) FROM file_ops WHERE id = :id AND state = 'queued'"),
            {"id": handed.id},
        )
    ).scalar_one()
    assert int(queued) == 1
