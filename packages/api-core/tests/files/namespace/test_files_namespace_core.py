"""Create, rename, move and resolve, against real Postgres."""

from __future__ import annotations

import base64
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import errors, names
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.namespace import Namespace, NodeAttrs
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.history import FileHistory
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from sqlalchemy import event, func, select, text
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


def _namespace(repo: FilesRepo, org: FilesOrg, clock: FakeClock, **kwargs: Any) -> Namespace:
    return Namespace(repo, _ctx(org), clock, None, **kwargs)


async def _row(session: AsyncSession, node_id: uuid.UUID) -> dict[str, Any]:
    """One node read as plain columns, around the ORM's identity map.

    The mutations under test run as ``text()`` statements, so an instance the
    session already holds keeps the values it was loaded with; asserting on a
    raw row is what makes a missing write visible instead of invisible.
    """
    result = await session.execute(text("SELECT * FROM file_nodes WHERE id = :id"), {"id": node_id})
    row = result.mappings().first()
    assert row is not None
    return dict(row)


async def _root(repo: FilesRepo, drive: FileDrive) -> FileNode:
    assert drive.root_node_id is not None
    node = await repo.node(NodeId(drive.root_node_id))
    assert node is not None
    return node


async def test_create_sets_every_derived_column(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """path_ids is the parent walk, depth follows it, and ctime comes from the server."""
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        root = await _root(repo, drive)
        folder = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "folder", b"docs", conflict="fail"
        )
        made = await namespace.create(
            DriveId(drive.id),
            NodeId(folder.id),
            "file",
            b"report.pdf",
            attrs=NodeAttrs(mode=0o600, size=17),
            conflict="fail",
        )

    async with repo.transaction():
        stored = await repo.node(NodeId(made.id))
        parent = await repo.node(NodeId(folder.id))
    assert stored is not None and parent is not None
    assert stored.path_ids == f"{parent.path_ids}.{ino_label(stored.ino)}"
    assert stored.depth == parent.depth + 1 == 2
    assert stored.name == b"report.pdf"
    assert stored.name_key == names.name_key(b"report.pdf")
    assert stored.flags_names == {
        "windows_safe": True,
        "macos_safe": True,
        "display_warning": False,
    }
    assert stored.etag == 1
    assert stored.mode == 0o600
    # ctime is the server's, so it is nowhere near the frozen test clock.
    assert stored.ctime_ns > int(clock.now().timestamp()) * 1_000_000_000
    assert stored.ino > parent.ino

    history_rows = (
        (await files_session.execute(select(FileHistory).where(FileHistory.node_id == stored.id)))
        .scalars()
        .all()
    )
    assert [(row.seq, row.kind) for row in history_rows] == [(1, "create")]


async def test_byte_exact_siblings_coexist_and_flip_macos_safe(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """README and readme both live; the fold collision is reported on both rows."""
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        root = await _root(repo, drive)
        upper = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"README", conflict="fail"
        )
        lower = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"readme", conflict="fail"
        )

    upper_row = await _row(files_session, upper.id)
    lower_row = await _row(files_session, lower.id)
    assert (upper_row["name"], lower_row["name"]) == (b"README", b"readme")
    assert upper_row["flags_names"]["macos_safe"] is False
    assert lower_row["flags_names"]["macos_safe"] is False


async def test_a_normalization_twin_of_a_sibling_is_refused(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """``café`` as one code point and as ``e`` plus a combining accent are one
    name on a macOS disk, so a person's create of the second is refused with
    the exact-clash answer, and nothing is written."""
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)
    nfc = unicodedata.normalize("NFC", "café.txt").encode()
    nfd = unicodedata.normalize("NFD", "café.txt").encode()
    async with repo.transaction():
        root = await _root(repo, drive)
        root_id = root.id
        await namespace.create(DriveId(drive.id), NodeId(root_id), "file", nfc, conflict="fail")

    with pytest.raises(errors.Conflict) as refused:
        async with repo.transaction():
            await namespace.create(DriveId(drive.id), NodeId(root_id), "file", nfd, conflict="fail")
    assert refused.value.code == "files.exists"
    stored = (
        await files_session.execute(
            select(FileNode.name).where(
                FileNode.parent_id == root_id, FileNode.trashed_at.is_(None)
            )
        )
    ).scalars()
    assert nfd not in set(stored)


async def test_keep_both_steps_past_a_normalization_twin(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)
    nfc = unicodedata.normalize("NFC", "café.txt").encode()
    nfd = unicodedata.normalize("NFD", "café.txt").encode()
    async with repo.transaction():
        root = await _root(repo, drive)
        await namespace.create(DriveId(drive.id), NodeId(root.id), "file", nfc, conflict="fail")
        kept = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", nfd, conflict="rename"
        )
    assert names.normalization_key(kept.name) != names.normalization_key(nfc)


async def test_a_rename_onto_a_normalization_twin_is_refused_but_onto_its_own_is_not(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The node's own name in the other form is not a sibling: respelling a
    name a person typed on another keyboard is allowed."""
    drive = await files_factory.drive()
    drive_id = DriveId(drive.id)
    namespace = _namespace(repo, files_org, clock)
    nfc = unicodedata.normalize("NFC", "café.txt").encode()
    nfd = unicodedata.normalize("NFD", "café.txt").encode()
    async with repo.transaction():
        root = await _root(repo, drive)
        root_id = root.id
        await namespace.create(drive_id, NodeId(root_id), "file", nfc, conflict="fail")
        other = await namespace.create(
            drive_id, NodeId(root_id), "file", b"other.txt", conflict="fail"
        )
        other_id, other_etag = other.id, other.etag

    with pytest.raises(errors.Conflict) as refused:
        async with repo.transaction():
            await namespace.rename(NodeId(other_id), nfd, if_match=other_etag)
    assert refused.value.code == "files.exists"

    async with repo.transaction():
        lone = await namespace.create(
            drive_id, NodeId(root_id), "file", "naïve.txt".encode(), conflict="fail"
        )
        respelled = await namespace.rename(
            NodeId(lone.id),
            unicodedata.normalize("NFD", "naïve.txt").encode(),
            if_match=lone.etag,
        )
    assert respelled.name == unicodedata.normalize("NFD", "naïve.txt").encode()


async def test_a_live_duplicate_is_a_conflict_and_a_trashed_one_is_not(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The partial unique index refuses the live twin and allows the trashed one."""
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        root = await _root(repo, drive)
        first = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"a.txt", conflict="fail"
        )

    async with repo.transaction():
        with pytest.raises(errors.Conflict) as refused:
            await namespace.create(
                DriveId(drive.id), NodeId(root.id), "file", b"a.txt", conflict="fail"
            )
        assert refused.value.code == "files.exists"

    async with repo.transaction():
        await repo.session.execute(
            select(FileNode).where(FileNode.id == first.id).with_for_update()
        )
        from sqlalchemy import text

        await repo.session.execute(
            text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id"), {"id": first.id}
        )
    async with repo.transaction():
        again = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"a.txt", conflict="fail"
        )
    assert again.id != first.id


async def test_conflict_rename_picks_the_numbered_name(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """conflict="rename" keeps the extension and takes the next free marker."""
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        root = await _root(repo, drive)
        await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"report.pdf", conflict="fail"
        )
        second = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"report.pdf", conflict="rename"
        )
    assert second.name == b"report (1).pdf"


async def test_a_stale_etag_rename_changes_nothing(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """412, and every column of the row is byte-identical to before the attempt."""
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        root = await _root(repo, drive)
        node = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"keep.txt", conflict="fail"
        )
    before = await _row(files_session, node.id)

    async with repo.transaction():
        with pytest.raises(errors.PreconditionFailed):
            await namespace.rename(
                NodeId(node.id), b"renamed.txt", if_match=int(before["etag"]) + 7
            )

    after = await _row(files_session, node.id)
    assert after == before


async def test_a_rename_that_matches_bumps_the_etag(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The negative twin of the 412: the right etag renames and moves it on."""
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        root = await _root(repo, drive)
        node = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"Readme.md", conflict="fail"
        )
    node_id, before_etag = NodeId(node.id), node.etag
    async with repo.transaction():
        renamed = await namespace.rename(node_id, b"README.md", if_match=before_etag)
    assert renamed.name == b"README.md"
    assert renamed.etag == before_etag + 1
    assert renamed.id == node_id


async def test_a_move_into_its_own_subtree_is_refused_in_statement(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """The cycle guard rides the UPDATE, so nothing is written before it fires."""
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b/ a/b/c/", drive=drive)
    namespace = _namespace(repo, files_org, clock)
    top, deep = made["a"], made["a/b/c"]

    async with repo.transaction():
        with pytest.raises(errors.Conflict) as refused:
            await namespace.move(NodeId(top.id), NodeId(deep.id), if_match=top.etag)
        assert refused.value.code == "files.cycle"

    stayed = await _row(files_session, top.id)
    assert stayed["parent_id"] == top.parent_id
    assert stayed["path_ids"] == top.path_ids


async def test_a_move_rewrites_the_whole_subtree(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """Every descendant's path follows, ids and inos survive, one outbox row."""
    drive = await files_factory.drive()
    made = await files_factory.tree("src/ src/pkg/ src/pkg/mod.py dst/", drive=drive)
    namespace = _namespace(repo, files_org, clock)
    moving, destination = made["src/pkg"], made["dst"]
    leaf_ino = made["src/pkg/mod.py"].ino

    async with repo.transaction():
        moved = await namespace.move(
            NodeId(moving.id), NodeId(destination.id), if_match=moving.etag
        )

    parent = await _row(files_session, destination.id)
    moved_row = await _row(files_session, moving.id)
    leaf = await _row(files_session, made["src/pkg/mod.py"].id)
    assert moved.id == moving.id
    assert moved_row["ino"] == moving.ino
    assert moved_row["path_ids"] == f"{parent['path_ids']}.{ino_label(moving.ino)}"
    assert moved_row["depth"] == parent["depth"] + 1
    assert leaf["path_ids"] == f"{moved_row['path_ids']}.{ino_label(leaf['ino'])}"
    assert leaf["depth"] == moved_row["depth"] + 1
    assert leaf["ino"] == leaf_ino

    rows = (
        (await files_session.execute(select(FileHistory).where(FileHistory.node_id == moved.id)))
        .scalars()
        .all()
    )
    assert [row.kind for row in rows] == ["move"]
    assert rows[0].before is not None and rows[0].after is not None
    assert rows[0].before["parent_id"] == str(moving.parent_id)
    assert rows[0].after["parent_id"] == str(destination.id)


async def test_a_create_hangs_off_the_path_its_folder_has_now(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """A folder carried along by a move holds its next child on the new path.

    The move rewrites every descendant's ``path_ids`` with a ``text()``
    statement the mapper never sees, so a descendant instance the session is
    already holding keeps the path it was loaded with. A create that derived
    the child's path from that instance would hang the node off a path no
    ancestor carries — off the tree for every read that asks by path prefix,
    the subtree sweeps included.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("src/ src/pkg/ src/pkg/inner/ dst/", drive=drive)
    namespace = _namespace(repo, files_org, clock)
    moving, inner, destination = made["src/pkg"], made["src/pkg/inner"], made["dst"]

    async with repo.transaction():
        await namespace.move(NodeId(moving.id), NodeId(destination.id), if_match=moving.etag)
    async with repo.transaction():
        child = await namespace.create(
            DriveId(drive.id), NodeId(inner.id), "file", b"mod.py", conflict="fail"
        )

    folder = await _row(files_session, inner.id)
    row = await _row(files_session, child.id)
    assert row["path_ids"] == f"{folder['path_ids']}.{ino_label(row['ino'])}"
    assert row["depth"] == folder["depth"] + 1


async def test_resolve_path_at_depth_24_uses_three_statements(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """Resolution is batched: the round trips do not grow with the depth."""
    depth = 24
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        parent = await _root(repo, drive)
        for level in range(depth):
            parent = await namespace.create(
                DriveId(drive.id),
                NodeId(parent.id),
                "folder",
                f"d{level}".encode(),
                conflict="fail",
            )
    path = "/".join(f"d{level}" for level in range(depth))

    counted: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        counted.append(statement)

    async with repo.transaction():
        engine = repo.session.get_bind()
        event.listen(engine, "before_cursor_execute", count)
        try:
            found = await namespace.resolve_path(DriveId(drive.id), path)
        finally:
            event.remove(engine, "before_cursor_execute", count)

    assert found is not None
    assert found.name == f"d{depth - 1}".encode()
    assert found.depth == depth
    assert len(counted) <= 3, counted


async def test_resolve_path_below_a_node_walks_the_step_down_from_it(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_org_factory: Callable[[], Awaitable[FilesOrg]],
    clock: FakeClock,
) -> None:
    """A holder that is told a folder's bare name addresses what is under it
    from the folder: the same segments, started at the node. An empty step
    is the node itself; a step that names nothing, a step that tries to climb
    (``..`` is no node's name) and a start in another drive all resolve to
    nothing, whatever an absolute walk would have found."""
    drive = await files_factory.drive()
    other = await files_factory.drive(org=await files_org_factory())
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        root = await _root(repo, drive)
        home = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "folder", b"home", conflict="fail"
        )
        chat = await namespace.create(
            DriveId(drive.id), NodeId(home.id), "folder", b"Kickoff.alkerachat", conflict="fail"
        )
        scratch = await namespace.create(
            DriveId(drive.id), NodeId(chat.id), "folder", b"scratch", conflict="fail"
        )
        answer = await namespace.create(
            DriveId(drive.id), NodeId(scratch.id), "file", b"answer.txt", conflict="fail"
        )
        # Another tenant's drive: a start node decides the drive walked,
        # never the names alone. Its root is named, not read: this org's
        # repo does not see it, which is the point.
        assert other.root_node_id is not None
        other_root = NodeId(other.root_node_id)

    async with repo.transaction():
        below = NodeId(chat.id)
        found = await namespace.resolve_path(DriveId(drive.id), "scratch/answer.txt", below=below)
        assert found is not None and found.id == answer.id
        deeper = await namespace.resolve_path(DriveId(drive.id), "/scratch/", below=below)
        assert deeper is not None and deeper.id == scratch.id
        itself = await namespace.resolve_path(DriveId(drive.id), "", below=below)
        assert itself is not None and itself.id == chat.id
        assert (
            await namespace.resolve_path(DriveId(drive.id), "scratch/nope.txt", below=below) is None
        )
        assert await namespace.resolve_path(DriveId(drive.id), "../home", below=below) is None
        # An absolute spelling is not a step below the node.
        assert (
            await namespace.resolve_path(
                DriveId(drive.id), "home/Kickoff.alkerachat/scratch", below=below
            )
            is None
        )
        # A node of another drive starts nothing in this one.
        assert await namespace.resolve_path(DriveId(drive.id), "", below=other_root) is None
        # The root walk is untouched: the absolute path still resolves.
        absolute = await namespace.resolve_path(
            DriveId(drive.id), "home/Kickoff.alkerachat/scratch/answer.txt"
        )
        assert absolute is not None and absolute.id == answer.id


async def test_resolve_path_walks_the_drive_the_path_names_not_the_handle_it_is_given(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The handle is a saved read, never a second opinion on which drive to walk.

    A request that has already resolved its drive may hand the row in so the
    walk does not read it again — but the id in the path is what decides which
    drive is resolved, so a handle for any other drive must be ignored rather
    than followed. Otherwise naming a drive id you do not have would resolve
    against the drive you do.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b/", drive=drive)
    namespace = _namespace(repo, files_org, clock)

    counted: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        counted.append(statement)

    async with repo.transaction():
        # The handle for the drive the path names: the same answer, one read
        # cheaper, because the drive row is the row the caller is holding.
        engine = repo.session.get_bind()
        event.listen(engine, "before_cursor_execute", count)
        try:
            with_handle = await namespace.resolve_path(DriveId(drive.id), "a/b", drive=drive)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        without_handle = await namespace.resolve_path(DriveId(drive.id), "a/b")
        # A handle for a different drive resolves that drive, which is not
        # there — it does not silently stand in for the one being held.
        elsewhere = await namespace.resolve_path(DriveId(uuid.uuid4()), "a/b", drive=drive)

    assert with_handle is not None
    assert with_handle.id == made["a/b"].id
    assert without_handle is not None
    assert without_handle.id == with_handle.id
    assert elsewhere is None
    assert len(counted) <= 2, counted


async def test_resolve_path_returns_none_for_a_missing_segment(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """The negative twin: a name that exists elsewhere is not a hit here."""
    drive = await files_factory.drive()
    await files_factory.tree("a/ a/b/ z/", drive=drive)
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        assert await namespace.resolve_path(DriveId(drive.id), "a/b") is not None
        assert await namespace.resolve_path(DriveId(drive.id), "z/b") is None


async def test_subtree_yields_the_node_and_its_descendants(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b/ a/b/c.txt other/", drive=drive)
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        seen = {row.id async for row in namespace.subtree(NodeId(made["a"].id))}
    assert seen == {made["a"].id, made["a/b"].id, made["a/b/c.txt"].id}


async def test_a_non_container_parent_is_refused(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/f.txt", drive=drive)
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        with pytest.raises(errors.InvalidRequest):
            await namespace.create(
                DriveId(drive.id),
                NodeId(made["a/f.txt"].id),
                "file",
                b"child.txt",
                conflict="fail",
            )


async def test_an_invalid_name_never_reaches_the_database(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """…and leaves as a Files refusal, not the raw ``ValueError``.

    ``names.InvalidName`` is a plain ``ValueError``, which the HTTP layer maps
    nowhere — a create carrying a ``/`` reached the caller as an opaque 500. The
    rule that refused it rides the code, so the route can name it.
    """
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)
    refused = {
        b"": "files.invalid_name.empty",
        b"a/b": "files.invalid_name.separator",
        b"..": "files.invalid_name.dot",
        b"n\x00l": "files.invalid_name.nul",
        b"n" * (names.NAME_MAX_BYTES + 1): "files.invalid_name.too_long",
        b"n\tl": "files.invalid_name.control",
        b"notes ": "files.invalid_name.surrounding_space",
    }
    async with repo.transaction():
        root = await _root(repo, drive)
        for bad, code in refused.items():
            with pytest.raises(errors.InvalidRequest) as raised:
                await namespace.create(
                    DriveId(drive.id), NodeId(root.id), "file", bad, conflict="fail"
                )
            assert raised.value.code == code
            assert isinstance(raised.value.__cause__, names.InvalidName)
        assert await repo.siblings(NodeId(root.id)) == []


async def test_a_move_of_a_missing_node_is_not_found(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    drive = await files_factory.drive()
    made = await files_factory.tree("a/", drive=drive)
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        with pytest.raises(errors.NotFound):
            await namespace.move(NodeId(uuid.uuid4()), NodeId(made["a"].id), if_match=1)


#: ``d0/d1/…``, one folder inside the next, in a single statement. The
#: recursive term builds each row's ``path_ids`` the way a create does — the
#: parent's path with this node's ino label appended — so the chain reaches
#: ``file_nodes`` and its truncated GiST index one deepening row at a time,
#: and every other column is what ``FilesFactory.tree`` writes for a folder.
_SEED_CHAIN = text(
    "WITH RECURSIVE rung AS ("
    " SELECT t.lvl AS lvl, t.id AS id, CAST(:root_id AS uuid) AS parent_id, t.ino AS ino,"
    " CAST(:root_path AS ltree) || CAST(t.label AS ltree) AS path_ids"
    " FROM unnest(CAST(:ids AS uuid[]), CAST(:inos AS bigint[]), CAST(:labels AS text[]))"
    " WITH ORDINALITY AS t(id, ino, label, lvl)"
    " WHERE t.lvl = 1"
    " UNION ALL"
    " SELECT t.lvl, t.id, r.id, t.ino, r.path_ids || CAST(t.label AS ltree)"
    " FROM rung r"
    " JOIN unnest(CAST(:ids AS uuid[]), CAST(:inos AS bigint[]), CAST(:labels AS text[]))"
    " WITH ORDINALITY AS t(id, ino, label, lvl) ON t.lvl = r.lvl + 1"
    ")"
    " INSERT INTO file_nodes ("
    " id, ino, drive_id, org_team_id, parent_id, kind, name, name_display, name_key,"
    " flags_names, xattrs, metadata, path_ids, depth, mode, uid, gid, nlink, rdev, size,"
    " atime_ns, mtime_ns, ctime_ns, birthtime_ns, etag, flags, state, trust, traversal_only)"
    " SELECT r.id, r.ino, :drive_id, :org_team_id, r.parent_id, 'folder',"
    " ('d' || (r.lvl - 1))::bytea, 'd' || (r.lvl - 1), 'd' || (r.lvl - 1),"
    " '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, r.path_ids, :root_depth + r.lvl,"
    " 420, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 'live', 'own', false"
    " FROM rung r"
)


async def _seed_chain(
    session: AsyncSession, drive: FileDrive, root: FileNode, *, levels: int
) -> NodeId:
    """Nest ``levels`` folders under ``root`` and return the innermost one's id.

    The chain is the corpus this test needs to be deep; it is not the claim.
    Building it one ``Namespace.create`` at a time cost twenty seconds a
    parametrization, and the cost was quadratic in the depth rather than linear:
    a create charges the drive's quota, and the quota's unfolded remainder is a
    sum over every stats delta the run has appended so far
    (``alkera_core.files.quota._pending``), so level N paid for the N-1 levels
    above it. Seeding buys that back without moving what the test drives through
    the product — the create at the bottom of the chain, the chain read, the
    subtree read and the move, every one of them at full depth.
    """
    start = int(drive.next_ino)
    inos = [start + offset for offset in range(levels)]
    ids = [uuid.uuid4() for _ in range(levels)]
    await session.execute(
        _SEED_CHAIN,
        {
            "root_id": root.id,
            "root_path": root.path_ids,
            "root_depth": root.depth,
            "drive_id": drive.id,
            "org_team_id": drive.org_team_id,
            "ids": ids,
            "inos": inos,
            "labels": [ino_label(ino) for ino in inos],
        },
    )
    drive.next_ino = start + levels
    await session.commit()
    return NodeId(ids[-1])


@pytest.mark.parametrize(
    "depth",
    [pytest.param(1_024, id="the-published-depth"), pytest.param(1_025, id="one-beyond-it")],
)
async def test_a_tree_as_deep_as_the_spec_allows_creates_resolves_lists_and_moves(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
    depth: int,
) -> None:
    """No path-length limit: the folder at the published depth is created by the
    product on top of a chain one shorter, and the tree that results still
    resolves, still answers a subtree read, and still moves.

    Every operation the depth is supposed to break runs through the product at
    full depth — the ``create`` that derives a ``path_ids`` this long from its
    parent's, the chain read, the subtree read, the move. The chain beneath them
    is seeded, and that costs the test nothing it was proving. The label width
    is still what the depth is spent on: the seeded rows go into the live
    ``ix_file_nodes_path_ids`` one deepening path at a time, and spelling those
    labels as the UUIDs the ino label replaced makes the seed itself fail with
    "failed to add item to index page". And the seed is not taken on trust
    either — the rows the path's labels name are checked to be the ``parent_id``
    walk, in order, so a path that does not describe the tree it sits in fails
    rather than passes.

    Nothing enforces a maximum, so the level past the published depth is
    exercised too: it must succeed, because a refusal there would be a limit
    the spec says is not there.
    """
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        root = await _root(repo, drive)
    parent_id = await _seed_chain(files_session, drive, root, levels=depth - 1)

    async with repo.transaction():
        parent = await repo.node(parent_id)
        assert parent is not None
        assert parent.depth == depth - 1

        made = await namespace.create(
            DriveId(drive.id), parent_id, "folder", f"d{depth - 1}".encode()
        )
        deepest = await repo.node(NodeId(made.id))
        assert deepest is not None
        assert deepest.depth == depth
        assert deepest.path_ids == f"{parent.path_ids}.{ino_label(deepest.ino)}"
        assert deepest.path_ids.count(".") == depth

        # Resolved: the whole chain comes back, root first — and it IS the
        # parent walk. The chain read looks its rows up BY the path's labels, so
        # "every row's ino is its label" is true however wrong the path is; what
        # makes the labels mean something is that the rows they name link
        # parent-to-child all the way down.
        chain = await repo.chain(deepest)
        chain_ids = [node.id for node in chain]
        assert chain_ids[0] == root.id
        assert chain_ids[-1] == deepest.id
        assert len(chain) == depth + 1
        assert [node.parent_id for node in chain[1:]] == chain_ids[:-1]
        assert [node.ino for node in chain] == [
            int(label, 36) for label in deepest.path_ids.split(".")
        ]

        # Listed as a subtree: every level is under the root, through the
        # truncated-prefix index plus the exact filter.
        under_root = await repo.subtree_page(root, limit=depth + 10)
        assert len(under_root) == depth + 1

    # Moved: the deepest node's parent becomes the root, and its subtree path is
    # rewritten to match.
    async with repo.transaction():
        moved = await namespace.move(NodeId(deepest.id), NodeId(root.id), if_match=deepest.etag)
        reread = await repo.node(NodeId(moved.id))
        assert reread is not None
        assert reread.parent_id == root.id
        assert reread.depth == 1
        assert reread.path_ids == f"{root.path_ids}.{ino_label(reread.ino)}"


@pytest.mark.parametrize(
    ("requested", "stored", "target"),
    [
        pytest.param("relative", "relative", b"sibling", id="relative"),
        pytest.param("canonical", "canonical", b"/sibling", id="canonical"),
        pytest.param(None, "relative", b"sibling", id="unclassified-defaults-to-relative"),
    ],
)
async def test_a_symlink_reads_back_the_kind_it_was_created_with(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
    requested: str | None,
    stored: str,
    target: bytes,
) -> None:
    """F-311: the column was never in the INSERT, so every link read back as the
    fallback and a `canonical` link materialized as a relative one.

    The kind is asserted off a raw row rather than the returned instance: the
    create runs as a ``text()`` statement, so an ORM instance would answer with
    whatever the reload happened to carry."""
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)

    async with repo.transaction():
        root = await _root(repo, drive)
        made = await namespace.create(
            DriveId(drive.id),
            NodeId(root.id),
            "symlink",
            f"link-{requested or 'bare'}".encode(),
            symlink_target=target,
            symlink_kind=requested,
        )

    row = await _row(files_session, made.id)
    assert row["symlink_kind"] == stored
    assert row["symlink_target"] == target


@pytest.mark.parametrize(
    ("kind", "target", "depth", "inside"),
    [
        pytest.param("relative", b"sibling", 0, True, id="a-sibling"),
        pytest.param("relative", b"./a/./b", 0, True, id="dots-that-stay"),
        pytest.param("relative", b"../up", 1, True, id="up-to-the-root-from-below"),
        pytest.param("relative", b"a/../b", 0, True, id="a-climb-that-stays"),
        pytest.param("relative", b"..", 0, False, id="the-roots-parent"),
        pytest.param("relative", b"../x", 0, False, id="climbing-out-from-the-root"),
        pytest.param("relative", b"../../x", 1, False, id="climbing-out-from-below"),
        pytest.param("relative", b"a/../../x", 0, False, id="a-nested-escape"),
        pytest.param("relative", b"a/b/../../../etc/passwd", 0, False, id="deep-nested-escape"),
        pytest.param("relative", b"a/b/../../../stays", 1, True, id="deep-climb-that-stays"),
        pytest.param("relative", b"/etc/passwd", 0, False, id="an-absolute-relative-link"),
        pytest.param("relative", b"", 0, False, id="no-target"),
        pytest.param("canonical", b"/data/notes.txt", 1, True, id="an-org-path"),
        pytest.param("canonical", b"", 1, True, id="the-anchor-itself"),
        pytest.param("canonical", b"/a/../../x", 1, False, id="an-org-path-climbing-out"),
        pytest.param("host", b"/etc/passwd", 0, False, id="a-host-path"),
        pytest.param("host", b"/", 0, False, id="the-host-root"),
    ],
)
async def test_a_symlink_is_made_only_when_it_points_inside_the_drive(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
    kind: str,
    target: bytes,
    depth: int,
    inside: bool,
) -> None:
    """A link the drive stores is materialized on every machine that pulls
    or mounts it: one whose target leaves the drive (a host path, a ``..``
    above the root, from however deep it sits) is refused before anything is
    written, and the folder holds nothing new."""
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)

    async with repo.transaction():
        parent = await _root(repo, drive)
        for level in range(depth):
            parent = await namespace.create(
                DriveId(drive.id), NodeId(parent.id), "folder", f"d{level}".encode()
            )
    async with repo.transaction():
        if inside:
            made = await namespace.create(
                DriveId(drive.id),
                NodeId(parent.id),
                "symlink",
                b"link",
                symlink_target=target,
                symlink_kind=kind,
            )
            assert (await _row(files_session, made.id))["symlink_target"] == target
            return
        with pytest.raises(errors.InvalidRequest) as refused:
            await namespace.create(
                DriveId(drive.id),
                NodeId(parent.id),
                "symlink",
                b"link",
                symlink_target=target,
                symlink_kind=kind,
            )
    assert refused.value.code == "files.link_outside_tree"
    assert refused.value.status == 422
    children = await files_session.scalar(
        select(func.count()).select_from(FileNode).where(FileNode.parent_id == parent.id)
    )
    assert children == 0


async def test_an_unknown_symlink_kind_is_refused_before_the_insert(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """The check constraint would answer with an IntegrityError that poisons the
    caller's transaction; the vocabulary is the caller's 400 instead. A folder
    that names a kind is the same mistake and gets the same answer."""
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)

    async with repo.transaction():
        root = await _root(repo, drive)
        with pytest.raises(errors.InvalidRequest):
            await namespace.create(
                DriveId(drive.id),
                NodeId(root.id),
                "symlink",
                b"bad.link",
                symlink_target=b"../x",
                symlink_kind="absolute",
            )
    async with repo.transaction():
        with pytest.raises(errors.InvalidRequest):
            await namespace.create(
                DriveId(drive.id),
                NodeId(root.id),
                "folder",
                b"not-a-link",
                symlink_kind="relative",
            )

    present = await files_session.execute(
        text("SELECT count(*) FROM file_nodes WHERE parent_id = :p"), {"p": root.id}
    )
    assert present.scalar_one() == 0


async def test_a_create_stores_xattrs_as_the_base64_the_wire_renders(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """One spelling in and out: the JSONB column holds the same base64 text the
    item facet returns, so a value written by a create and a value written by a
    PATCH are indistinguishable to a reader."""
    drive = await files_factory.drive()
    namespace = _namespace(repo, files_org, clock)
    raw = b"Red\nImportant\x00\xff"

    async with repo.transaction():
        root = await _root(repo, drive)
        made = await namespace.create(
            DriveId(drive.id),
            NodeId(root.id),
            "file",
            b"tagged.txt",
            attrs=NodeAttrs(xattrs={"user.tags": raw}),
        )

    row = await _row(files_session, made.id)
    assert row["xattrs"] == {"user.tags": base64.b64encode(raw).decode("ascii")}


async def test_a_move_into_another_drive_is_refused(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_org_factory: Any,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """The re-parent rewrites the path and never the drive, so it may not cross one.

    A node whose ``drive_id`` names one drive while its ``parent_id`` and
    ``path_ids`` sit in another is counted against the first drive's quota,
    absent from its tree and present in the other's — and the only foreign key
    is ``parent_id -> file_nodes.id``, so nothing in the database refuses it.
    The destination is an in-org folder row whose ``drive_id`` names a second
    drive, which is what a second drive per org will make ordinary.
    """
    home = await files_factory.drive()
    away = await files_factory.drive(org=await files_org_factory())
    tree = await files_factory.tree("a.txt", drive=home)
    target_id = tree["a.txt"].id
    target_etag = int(tree["a.txt"].etag)
    elsewhere = FileNode(
        id=uuid.uuid4(),
        ino=9_999,
        drive_id=away.id,
        org_team_id=files_org.org_team_id,
        parent_id=None,
        kind="folder",
        name=b"",
        name_display="",
        name_key="",
        path_ids=ino_label(9_999),
        depth=0,
        traversal_only=True,
    )
    files_session.add(elsewhere)
    await files_session.commit()
    destination = NodeId(elsewhere.id)

    namespace = _namespace(repo, files_org, clock)
    async with repo.transaction():
        with pytest.raises(errors.InvalidRequest):
            await namespace.move(NodeId(target_id), destination, if_match=target_etag)

    row = await _row(files_session, target_id)
    assert row["drive_id"] == home.id
    assert row["parent_id"] == home.root_node_id
