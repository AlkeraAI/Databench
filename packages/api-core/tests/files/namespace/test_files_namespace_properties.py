"""The tree's invariants, recomputed from scratch after every generated step.

The namespace maintains four derived things — ``path_ids``, ``depth``,
``name_key`` and the ``macos_safe`` flag — plus two counters (``ino`` and
``next_ino``). Every one of them is *derivable*, so the only honest check is to
derive it again from the adjacency list and compare, after every single
operation rather than at the end: a property that only looks at the final state
cannot tell a tree that was never broken from one that was broken and repaired.

The limits are the other half — or rather their absence. The spec publishes a
depth of 1,024 but no path-length cap and no children-per-folder cap, so the
explicit cases below pin the two absences at a size a naive implementation would
already have refused: a limit that creeps in later is a silent product change.
The depth boundary itself is pinned next door, in the core suite.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.config import settings
from alkera_core.files import errors, names
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import DriveId, NodeId, OrgScope, TrashOpId
from alkera_core.files.namespace import Namespace
from alkera_core.files.path_labels import ino_label
from alkera_core.files.repo import FilesRepo
from alkera_core.files.trash import Trash
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.engine import hypothesis_runner
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg, _seed_org
from tests.files.strategies import op_sequences


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


# ---------------------------------------------------------------------------
# The invariants, recomputed
# ---------------------------------------------------------------------------


class InoLedger:
    """What every ino in one drive was last seen attached to.

    ``ino`` is the identity a mounted client caches, so re-using one for a
    second node would make a box serve the wrong file out of its own cache. The
    ledger keeps every ino ever seen, not only the live ones, which is what
    turns "unique right now" into "never reused".
    """

    def __init__(self) -> None:
        self._owner: dict[int, uuid.UUID] = {}
        self._high_water = 0

    def observe(self, nodes: Sequence[FileNode], next_ino: int) -> None:
        seen: dict[int, uuid.UUID] = {}
        for node in nodes:
            assert node.ino not in seen, f"ino {node.ino} is on two live nodes in one drive"
            seen[node.ino] = node.id
            owner = self._owner.get(node.ino)
            assert owner in (None, node.id), f"ino {node.ino} was reused: {owner} then {node.id}"
            self._owner[node.ino] = node.id
            assert node.ino < next_ino, f"ino {node.ino} was handed out at or past next_ino"
        assert next_ino >= self._high_water, (
            f"next_ino went backwards: {self._high_water} then {next_ino}"
        )
        self._high_water = next_ino


async def _live_nodes(repo: FilesRepo, drive: FileDrive) -> list[FileNode]:
    """Every live node of one drive, read fresh around the identity map."""
    stmt = (
        repo.select_nodes()
        .where(FileNode.drive_id == drive.id, FileNode.trashed_at.is_(None))
        .execution_options(populate_existing=True)
    )
    return list((await repo.session.execute(stmt)).scalars().all())


async def _assert_invariants(
    repo: FilesRepo, drive: FileDrive, ledger: InoLedger, *, where: str
) -> None:
    """Derive every derived column again from the adjacency list and compare."""
    nodes = await _live_nodes(repo, drive)
    by_id = {node.id: node for node in nodes}

    for node in nodes:
        # A walk that revisits a node is a cycle; one that runs past the tree's
        # size is the same cycle seen from further away.
        walk: list[FileNode] = []
        seen: set[uuid.UUID] = set()
        cursor: FileNode | None = node
        while cursor is not None:
            assert cursor.id not in seen, f"{where}: parent chain of {node.id} is a cycle"
            seen.add(cursor.id)
            walk.append(cursor)
            cursor = by_id.get(cursor.parent_id) if cursor.parent_id is not None else None
        walk.reverse()
        assert walk[0].parent_id is None, f"{where}: {node.id} does not reach a root"

        expected_path = ".".join(ino_label(step.ino) for step in walk)
        assert node.path_ids == expected_path, (
            f"{where}: path_ids of {node.id} is {node.path_ids}, the walk says {expected_path}"
        )
        assert node.depth == len(walk) - 1, (
            f"{where}: depth of {node.id} is {node.depth}, the walk is {len(walk) - 1} deep"
        )
        assert node.name_key == names.name_key(node.name), (
            f"{where}: name_key of {node.id} does not match names.name_key"
        )

    siblings: dict[uuid.UUID | None, list[FileNode]] = {}
    for node in nodes:
        siblings.setdefault(node.parent_id, []).append(node)
    for parent_id, group in siblings.items():
        exact = [node.name for node in group]
        assert len(set(exact)) == len(exact), f"{where}: folder {parent_id} has two equal names"
        folded: dict[str, list[FileNode]] = {}
        for node in group:
            folded.setdefault(names.name_key(node.name), []).append(node)
        # Re-derived from the adjacency list in BOTH directions: the flag is
        # false EXACTLY for the live siblings that fold onto another live
        # sibling. Asserting only the first half passes on a namespace that
        # never restores the flag once a rename, a move or a trash dissolves
        # the collision that set it.
        for key, colliding in folded.items():
            expected = len(colliding) == 1
            for node in colliding:
                # A drive root is seeded with no name flags at all, and "no
                # flag" is the same claim as "safe" — so the default is True
                # rather than a skip: a colliding sibling that lost the key
                # still has to fail here.
                claimed = node.flags_names.get("macos_safe", True)
                assert claimed is expected, (
                    f"{where}: {node.id} claims macos_safe {claimed!r}; folded key {key!r} is "
                    f"held by {len(colliding)} live siblings of folder {parent_id}"
                )

    refreshed = await repo.drive(DriveId(drive.id))
    assert refreshed is not None
    # The allocator bumps ``next_ino`` with a ``text()`` UPDATE, which the
    # identity map never sees; reading the mapped instance alone would compare
    # today's inos against the counter as it stood when the drive was made.
    await repo.session.refresh(refreshed)
    ledger.observe(nodes, refreshed.next_ino)


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


async def _apply(
    repo: FilesRepo, namespace: Namespace, drive: FileDrive, op: tuple[Any, ...], seq: int
) -> None:
    """Apply one generated verb to a live node chosen deterministically.

    The generator names paths in its own model of the tree; the tree that exists
    is whatever the namespace accepted, so the verb and the ordering are what the
    property consumes. A refusal is a legal outcome — the invariants have to hold
    after it too, which is exactly what the ``except`` lets the loop assert.
    """
    async with repo.transaction():
        live = await _live_nodes(repo, drive)
    if not live:
        return
    target = live[seq % len(live)]
    folders = [node for node in live if node.kind == "folder"]
    tag = str(op[0])
    raw = op[2] if len(op) > 2 and isinstance(op[2], bytes) else b"generated"

    async with repo.transaction():
        try:
            if tag == "create" and folders:
                parent = folders[seq % len(folders)]
                kind = "folder" if seq % 2 == 0 else "file"
                await namespace.create(
                    DriveId(drive.id), NodeId(parent.id), kind, raw, conflict="rename"
                )
            elif tag == "rename" and target.parent_id is not None:
                await namespace.rename(
                    NodeId(target.id), raw, if_match=target.etag, conflict="rename"
                )
            elif tag == "move" and folders and target.parent_id is not None:
                parent = folders[(seq + 1) % len(folders)]
                if parent.id == target.id:
                    return
                await namespace.move(
                    NodeId(target.id), NodeId(parent.id), if_match=target.etag, conflict="rename"
                )
        except errors.FilesError:
            return


async def _play(ops: Sequence[tuple[Any, ...]]) -> None:
    """One example: a fresh tenant, a fresh drive, the generated history.

    The tenant is what isolates one example from the next; the connection under
    it comes from the shared pool and goes back at the end of the example.
    """
    session = AsyncSession(bind=hypothesis_runner().engine, expire_on_commit=False)
    try:
        org = await _seed_org(session)
        repo = FilesRepo(session, OrgScope(org_team_id=org.org_team_id))
        drive = await FilesFactory(session, org).drive(org=org)
        namespace = Namespace(repo, _ctx(org), FakeClock(now=EPOCH), None)
        ledger = InoLedger()

        async with repo.transaction():
            await _assert_invariants(repo, drive, ledger, where="before any op")
        for seq, op in enumerate(ops):
            await _apply(repo, namespace, drive, op, seq)
            async with repo.transaction():
                await _assert_invariants(repo, drive, ledger, where=f"after op {seq} ({op[0]!r})")
    finally:
        await session.close()


@hypothesis_settings(
    max_examples=60,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(history=op_sequences(max_ops=8, max_nodes=5))
def test_every_generated_history_keeps_the_tree_derivable(history: Any) -> None:
    """After every create, rename and move, the derived columns still derive."""
    _, ops = history
    try:
        # Not ``asyncio.run``: that would close the loop, and an asyncpg
        # connection cannot outlive the loop that opened it, so every example
        # would have to build an engine of its own to go with it.
        hypothesis_runner().run(_play(ops))
    except AssertionError:
        print(
            "replay: HYPOTHESIS_SEED=<seed printed above> "
            f"DATABASE_URL={settings.database_url} uv run --frozen pytest "
            "packages/api-core/tests/files/namespace"
            "/test_files_namespace_properties.py -p no:randomly"
        )
        raise


# ---------------------------------------------------------------------------
# The limits that exist, and the ones that must not
# ---------------------------------------------------------------------------


async def _root(repo: FilesRepo, drive: FileDrive) -> FileNode:
    assert drive.root_node_id is not None
    node = await repo.node(NodeId(drive.root_node_id))
    assert node is not None
    return node


@pytest.mark.asyncio
async def test_a_two_thousand_byte_path_stores_and_resolves(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """No server path-length limit: ten 200-byte segments resolve as one path."""
    drive = await files_factory.drive()
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    segments = [f"{index:03d}".encode() + b"z" * 197 for index in range(10)]
    assert sum(len(part) + 1 for part in segments) > 2_000

    async with repo.transaction():
        parent = await _root(repo, drive)
        for depth, segment in enumerate(segments):
            kind = "file" if depth == len(segments) - 1 else "folder"
            parent = await namespace.create(
                DriveId(drive.id), NodeId(parent.id), kind, segment, conflict="fail"
            )
        deepest = parent

    async with repo.transaction():
        found = await namespace.resolve_path(
            DriveId(drive.id), "/".join(part.decode() for part in segments)
        )
    assert found is not None
    assert found.id == deepest.id


@pytest.mark.asyncio
async def test_a_folder_of_three_thousand_children_lists_and_takes_one_more(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """No children-per-folder cap: 20,000 is a UI soft threshold, not a refusal."""
    drive = await files_factory.drive()
    made = await files_factory.tree(
        " ".join(["big/", *[f"big/child-{index:05d}.txt" for index in range(3_000)]]), drive=drive
    )
    folder = made["big"]

    async with repo.transaction():
        listed: list[FileNode] = []
        after: tuple[str, uuid.UUID] | None = None
        while True:
            page = await repo.children_page(NodeId(folder.id), after=after, limit=1_000)
            listed.extend(page)
            if len(page) < 1_000:
                break
            after = (page[-1].name_key, page[-1].id)
        assert len(listed) == 3_000

        namespace = Namespace(repo, _ctx(files_org), clock, None)
        extra = await namespace.create(
            DriveId(drive.id), NodeId(folder.id), "file", b"one-more.txt", conflict="fail"
        )
    assert extra.parent_id == folder.id


@pytest.mark.asyncio
async def test_readme_recased_is_an_ordinary_rename(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """``Readme.md`` → ``README.md`` collides with nothing: same folder, same node."""
    drive = await files_factory.drive()
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    async with repo.transaction():
        root = await _root(repo, drive)
        made = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"Readme.md", conflict="fail"
        )
    async with repo.transaction():
        renamed = await namespace.rename(NodeId(made.id), b"README.md", if_match=made.etag)

    assert renamed.id == made.id
    assert renamed.ino == made.ino
    assert renamed.name == b"README.md"
    assert renamed.name_key == names.name_key(b"README.md")
    assert renamed.flags_names["macos_safe"] is True


# ---------------------------------------------------------------------------
# macos_safe is re-derived, not only ever cleared
# ---------------------------------------------------------------------------


async def _safe(repo: FilesRepo, node_id: uuid.UUID) -> bool:
    """The flag as it stands on the row, read past the identity map."""
    async with repo.transaction():
        node = await repo.node(NodeId(node_id))
        assert node is not None
        await repo.session.refresh(node)
        return bool(node.flags_names["macos_safe"])


@pytest.mark.asyncio
async def test_macos_safe_comes_back_when_a_rename_dissolves_the_collision(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """``Readme.md`` and ``README.md`` fold together; rename one away and both are safe."""
    drive = await files_factory.drive()
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    async with repo.transaction():
        root = await _root(repo, drive)
        lower = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"Readme.md", conflict="fail"
        )
        upper = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"README.md", conflict="fail"
        )
    lower_id, upper_id = lower.id, upper.id
    assert await _safe(repo, lower_id) is False
    assert await _safe(repo, upper_id) is False

    async with repo.transaction():
        moved_on = await repo.node(NodeId(upper_id))
        assert moved_on is not None
        await namespace.rename(NodeId(upper_id), b"NOTES.md", if_match=moved_on.etag)

    assert await _safe(repo, lower_id) is True
    assert await _safe(repo, upper_id) is True


@pytest.mark.asyncio
async def test_macos_safe_comes_back_when_the_twin_is_moved_out_of_the_folder(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """A move is the same dissolution from the other side: both folders re-derive."""
    drive = await files_factory.drive()
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    async with repo.transaction():
        root = await _root(repo, drive)
        elsewhere = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "folder", b"elsewhere", conflict="fail"
        )
        lower = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"Readme.md", conflict="fail"
        )
        upper = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"README.md", conflict="fail"
        )
    lower_id, upper_id, folder_id = lower.id, upper.id, elsewhere.id
    assert await _safe(repo, lower_id) is False

    async with repo.transaction():
        moved_on = await repo.node(NodeId(upper_id))
        assert moved_on is not None
        await namespace.move(NodeId(upper_id), NodeId(folder_id), if_match=moved_on.etag)

    # The survivor in the source folder is safe again, and the arrival is safe
    # in a folder where nothing folds onto it.
    assert await _safe(repo, lower_id) is True
    assert await _safe(repo, upper_id) is True


@pytest.mark.asyncio
async def test_macos_safe_follows_a_trash_and_comes_back_on_restore(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """Trashing the twin frees the survivor; restoring it makes the pair unsafe again."""
    drive = await files_factory.drive()
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    trash = Trash(repo, _ctx(files_org), clock, None)
    async with repo.transaction():
        root = await _root(repo, drive)
        lower = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"Readme.md", conflict="fail"
        )
        upper = await namespace.create(
            DriveId(drive.id), NodeId(root.id), "file", b"README.md", conflict="fail"
        )
    lower_id, upper_id = lower.id, upper.id
    assert await _safe(repo, lower_id) is False

    async with repo.transaction():
        moved_on = await repo.node(NodeId(upper_id))
        assert moved_on is not None
        op = await trash.trash(NodeId(upper_id), if_match=moved_on.etag)
    op_id = op.id
    assert await _safe(repo, lower_id) is True

    async with repo.transaction():
        await trash.restore(TrashOpId(op_id))

    assert await _safe(repo, lower_id) is False
    assert await _safe(repo, upper_id) is False
