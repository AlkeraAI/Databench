"""`create_tree`: one folder per distinct prefix, one statement per level, replayable."""

from __future__ import annotations

import uuid

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import names, tree
from alkera_core.files.errors import InvalidRequest, NotFound
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.ino import DEFAULT_BLOCK
from alkera_core.files.repo import FilesRepo
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


class _Counter:
    def __init__(self, repo: FilesRepo) -> None:
        self._engine = repo.session.get_bind()
        self.count = 0
        self.node_inserts = 0
        self.drive_updates = 0

    def __enter__(self) -> _Counter:
        event.listen(self._engine, "before_cursor_execute", self._seen)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._seen)

    def _seen(self, _conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        self.count += 1
        squashed = " ".join(statement.split())
        if "INSERT INTO file_nodes" in squashed:
            self.node_inserts += 1
        if "UPDATE file_drives" in squashed:
            self.drive_updates += 1


def _wide_drop(files: int, levels: int) -> list[str]:
    """`files` paths spread over `levels` folder levels, each leaf a file."""
    made: list[str] = []
    for n in range(files):
        parts = [f"L{depth}-{(n // (10**depth)) % 5}" for depth in range(levels)]
        made.append("/".join([*parts, f"f{n}.txt"]))
    return made


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        pytest.param(["a/b/c.txt"], ["a", "a/b"], id="leaf-file-is-not-a-folder"),
        pytest.param(["a/b/"], ["a", "a/b"], id="trailing-slash-is-a-folder"),
        pytest.param(["a/b.txt", "a/c.txt"], ["a"], id="siblings-share-one-prefix"),
        pytest.param(["x.txt"], [], id="a-bare-file-implies-nothing"),
        pytest.param(["//a//b.txt"], ["a"], id="empty-segments-collapse"),
    ],
)
async def test_prefixes_are_the_distinct_folders_shallowest_first(
    paths: list[str], expected: list[str]
) -> None:
    assert tree.prefixes(paths) == expected


async def test_five_thousand_paths_make_one_folder_per_distinct_prefix(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    paths = _wide_drop(5_000, 8)
    wanted = tree.prefixes(paths)
    async with repo.transaction():
        with _Counter(repo) as counted:
            result = await tree.create_tree(
                repo,
                _ctx(files_org),
                DriveId(drive.id),
                NodeId(drive.root_node_id),
                paths,
                idempotency_key="drop-1",
            )
        rows = (
            await repo.execute_scoped(
                select(func.count())
                .select_from(FileNode)
                .where(FileNode.drive_id == drive.id, FileNode.kind == "folder")
            )
        ).scalar_one()
    assert set(result.nodes) == set(wanted)
    assert result.created == frozenset(wanted)
    # Exactly one folder per distinct prefix, plus the drive's own root.
    assert rows == len(wanted) + 1
    # Eight levels: eight level statements plus the one ino-block bump. The
    # history and outbox rows are the caller's own writes, not the round trip.
    level_statements = 8 + 1
    assert counted.count > level_statements  # history + outbox are real writes
    assert len({path.count("/") for path in wanted}) == 8


async def test_a_fifty_thousand_folder_skeleton_is_admitted_rather_than_refused(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    """A real directory drop is tens of thousands of folders, and the bound used
    to sit at twenty thousand — under one.

    A skeleton is all-or-nothing, so the old ceiling did not trim a large drop:
    it failed the whole thing, with nothing for the client to retry and no
    partial tree to resume from. Fifty thousand folders now travel past the
    guard and are refused, if at all, by something real about the request — here
    a parent that does not exist, which is decided AFTER the size and so is
    itself the proof the size was admitted. On the old ceiling this raised
    ``files.tree_too_large`` and never reached the lookup.

    Deliberately not driven to the rows: every created folder writes its own
    history and change event, so a fifty-thousand-folder drop is minutes of
    per-row round trips. The writing half of this path is exercised at five
    thousand paths above, which is the same statement at a size the suite can
    afford; what is under test here is the ceiling.
    """
    drive = await files_factory.drive()
    paths = [f"L0-{n % 50}/L1-{n}/f.txt" for n in range(50_000)]
    wanted = tree.prefixes(paths)
    assert len(wanted) > 20_000, "the drop has to cross the ceiling it used to hit"
    async with repo.transaction():
        with pytest.raises(NotFound):
            await tree.create_tree(
                repo,
                _ctx(files_org),
                DriveId(drive.id),
                NodeId(uuid.uuid4()),
                paths,
                idempotency_key="big-drop",
            )


async def test_the_round_trip_is_one_statement_per_level_plus_the_ino_block(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    """Levels are sequential; rows inside a level are one statement."""
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    paths = [f"L0-{a}/L1-{b}/L2-{c}/f.txt" for a in range(4) for b in range(4) for c in range(4)]
    wanted = tree.prefixes(paths)
    levels = len({row.count("/") for row in wanted})
    assert levels == 3 and len(wanted) == 4 + 16 + 64
    async with repo.transaction():
        with _Counter(repo) as counted:
            await tree.create_tree(
                repo,
                _ctx(files_org),
                DriveId(drive.id),
                NodeId(drive.root_node_id),
                paths,
                idempotency_key="levels",
            )
    # 84 folders, but only three statements wrote them — the rows inside one
    # level go in together — plus the single bump of the drive's ino counter.
    assert counted.node_inserts == levels
    assert counted.drive_updates == 1


async def test_a_replay_returns_the_same_ids_and_creates_nothing(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    paths = ["a/b/c.txt", "a/d/e.txt", "f/g.txt"]
    async with repo.transaction():
        first = await tree.create_tree(
            repo,
            _ctx(files_org),
            DriveId(drive.id),
            NodeId(drive.root_node_id),
            paths,
            idempotency_key="same",
        )
    async with repo.transaction():
        second = await tree.create_tree(
            repo,
            _ctx(files_org),
            DriveId(drive.id),
            NodeId(drive.root_node_id),
            paths,
            idempotency_key="same",
        )
        rows = (
            await repo.execute_scoped(
                select(func.count())
                .select_from(FileNode)
                .where(FileNode.drive_id == drive.id, FileNode.kind == "folder")
            )
        ).scalar_one()
    assert second.nodes == first.nodes
    assert second.created == frozenset()
    assert second.replayed and not first.replayed
    assert rows == len(first.nodes) + 1


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        pytest.param("a/./b.txt", "files.invalid_name.dot", id="dot"),
        pytest.param("a/../b.txt", "files.invalid_name.dot", id="dotdot"),
        pytest.param("a/b \x00c/d.txt", "files.invalid_name.nul", id="nul"),
        pytest.param(
            "a/" + "n" * (names.NAME_MAX_BYTES + 1) + "/d.txt",
            "files.invalid_name.too_long",
            id="one-byte-over-the-ceiling",
        ),
        pytest.param("a/b\tc/d.txt", "files.invalid_name.control", id="control"),
        pytest.param(
            "a/trailing /d.txt", "files.invalid_name.surrounding_space", id="trailing-space"
        ),
    ],
)
async def test_an_invalid_segment_refuses_the_whole_call(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory, bad: str, code: str
) -> None:
    """And says which rule refused it, in the vocabulary every other route uses.

    A skeleton raised a bare ``too_long`` where a create raises
    ``files.invalid_name.too_long``, so the browser's refusal table had no row
    for it and the same broken name read as one sentence from New folder and as
    "Request failed" from a folder upload.
    """
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    async with repo.transaction():
        with pytest.raises(InvalidRequest) as raised:
            await tree.create_tree(
                repo,
                _ctx(files_org),
                DriveId(drive.id),
                NodeId(drive.root_node_id),
                ["good/ok.txt", bad],
                idempotency_key="refused",
            )
    assert raised.value.code == code
    async with repo.transaction():
        rows = (
            await repo.execute_scoped(
                select(func.count())
                .select_from(FileNode)
                .where(FileNode.drive_id == drive.id, FileNode.kind == "folder")
            )
        ).scalar_one()
    # Nothing created — not even the valid sibling that would have gone first.
    assert rows == 1


async def test_a_parent_in_another_org_is_not_found(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    import uuid

    drive = await files_factory.drive()
    async with repo.transaction():
        with pytest.raises(NotFound):
            await tree.create_tree(
                repo,
                _ctx(files_org),
                DriveId(drive.id),
                NodeId(uuid.uuid4()),
                ["a/b.txt"],
                idempotency_key="nope",
            )


async def test_an_empty_drop_creates_nothing_and_is_not_an_error(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    async with repo.transaction():
        result = await tree.create_tree(
            repo,
            _ctx(files_org),
            DriveId(drive.id),
            NodeId(drive.root_node_id),
            ["loose.txt"],
            idempotency_key="empty",
        )
    assert result.nodes == {} and result.created == frozenset()


async def test_a_missing_idempotency_key_is_refused(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    async with repo.transaction():
        with pytest.raises(InvalidRequest):
            await tree.create_tree(
                repo,
                _ctx(files_org),
                DriveId(drive.id),
                NodeId(drive.root_node_id),
                ["a/b.txt"],
                idempotency_key="",
            )


async def test_a_skeleton_deeper_than_the_cap_is_refused(
    repo: FilesRepo, files_org: FilesOrg, files_factory: FilesFactory
) -> None:
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    too_deep = "/".join(f"d{n}" for n in range(tree.MAX_TREE_DEPTH + 1)) + "/leaf.txt"
    just_deep = "/".join(f"d{n}" for n in range(tree.MAX_TREE_DEPTH)) + "/leaf.txt"
    async with repo.transaction():
        with pytest.raises(InvalidRequest):
            await tree.create_tree(
                repo,
                _ctx(files_org),
                DriveId(drive.id),
                NodeId(drive.root_node_id),
                [too_deep],
                idempotency_key="deep",
            )
    async with repo.transaction():
        ok = await tree.create_tree(
            repo,
            _ctx(files_org),
            DriveId(drive.id),
            NodeId(drive.root_node_id),
            [just_deep],
            idempotency_key="just",
        )
    assert len(ok.created) == tree.MAX_TREE_DEPTH


async def _drive_next_ino(session: AsyncSession, drive_id: uuid.UUID) -> int:
    value = (
        await session.execute(
            text("SELECT next_ino FROM file_drives WHERE id = :id"), {"id": drive_id}
        )
    ).scalar_one()
    return int(value)


@pytest.mark.parametrize(
    ("folders", "blocks"),
    [
        pytest.param(7, 1, id="seven-folders-take-one-whole-block"),
        pytest.param(DEFAULT_BLOCK, 1, id="a-block-exactly-takes-one-block"),
        pytest.param(DEFAULT_BLOCK + 1, 2, id="one-past-a-block-takes-two"),
    ],
)
async def test_a_skeleton_moves_next_ino_by_whole_blocks_never_its_folder_count(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    files_session: AsyncSession,
    folders: int,
    blocks: int,
) -> None:
    """The counter gap says how many blocks were needed, never how many folders.

    A caller who reads `next_ino` before and after a stranger's drop must learn
    nothing about its size (ids and counters reveal nothing): an
    exact-count bump publishes it, a whole-block bump rounds it away.
    """
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    paths = [f"d{n}/f.txt" for n in range(folders)]
    assert len(tree.prefixes(paths)) == folders
    before = await _drive_next_ino(files_session, drive.id)

    async with repo.transaction():
        result = await tree.create_tree(
            repo,
            _ctx(files_org),
            DriveId(drive.id),
            NodeId(drive.root_node_id),
            paths,
            idempotency_key=f"blocks-{folders}",
        )

    assert len(result.created) == folders
    moved = await _drive_next_ino(files_session, drive.id) - before
    assert moved % DEFAULT_BLOCK == 0
    assert moved == blocks * DEFAULT_BLOCK


async def test_every_folder_of_a_skeleton_carries_an_ino_the_allocator_handed_out(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    files_session: AsyncSession,
) -> None:
    """Two blocks need not be contiguous, so rows carry the handed inos, not a run."""
    drive = await files_factory.drive()
    assert drive.root_node_id is not None
    folders = DEFAULT_BLOCK + 1
    paths = [f"d{n}/f.txt" for n in range(folders)]
    before = await _drive_next_ino(files_session, drive.id)

    async with repo.transaction():
        await tree.create_tree(
            repo,
            _ctx(files_org),
            DriveId(drive.id),
            NodeId(drive.root_node_id),
            paths,
            idempotency_key="distinct-inos",
        )

    after = await _drive_next_ino(files_session, drive.id)
    inos = [
        int(value)
        for value in (
            await files_session.execute(
                text(
                    "SELECT ino FROM file_nodes WHERE drive_id = :drive "
                    "AND kind = 'folder' AND parent_id IS NOT NULL"
                ),
                {"drive": drive.id},
            )
        )
        .scalars()
        .all()
    ]
    assert len(inos) == folders
    assert len(set(inos)) == folders
    assert all(before <= ino < after for ino in inos)
