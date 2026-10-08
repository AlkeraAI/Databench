"""A subtree copy: new nodes, the same content hashes, no store call.

Every assertion here is about what a later reader can see — the rows, the
hashes, the aggregated bytes — never about how the copy got there.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.config import settings
from alkera_core.files import copy as copy_module
from alkera_core.files.checkpoints import CheckpointKilled, PausingCheckpoints
from alkera_core.files.clock import FakeClock
from alkera_core.files.content import ContentService
from alkera_core.files.copy import resume_copy, run_copy, start_copy
from alkera_core.files.ids import DomainId, NodeId, OperationId, VersionId
from alkera_core.files.ops import Operations
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.store.scoped import _RootedDomainStore
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from alkera_core.models.files.versions import FileVersion
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: The three-level tree every test copies. Names carry a byte that is not
#: ASCII-safe in one of them, so "byte-identical" is a claim with teeth.
TREE = "src/ src/a/ src/a/b/ src/a/b/deep.bin src/a/one.txt src/two.txt dst/"


async def _stream(payload: bytes, *, chunk: int = 8192) -> AsyncIterator[bytes]:
    """The bytes as a client sends them: several chunks, never one buffer."""
    for start in range(0, len(payload), chunk):
        yield payload[start : start + chunk]


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _give_content(
    session: AsyncSession, node: FileNode, *, payload: bytes, digest: str
) -> FileVersion:
    """Give a file node a head version holding ``payload`` inline."""
    version = FileVersion(
        id=uuid.uuid4(),
        org_team_id=node.org_team_id,
        node_id=node.id,
        seq=1,
        size_bytes=len(payload),
        content_hash=digest,
        block_hash=digest,
        inline_bytes=payload,
        source="upload",
    )
    session.add(version)
    await session.flush()
    node.head_version_id = version.id
    node.size = len(payload)
    await session.commit()
    return version


async def _seed(session: AsyncSession, factory: FilesFactory) -> tuple[FileDrive, dict[str, Any]]:
    drive = await factory.drive()
    made = await factory.tree(TREE, drive=drive)
    await _give_content(session, made["src/a/b/deep.bin"], payload=b"deep", digest="h-deep")
    await _give_content(session, made["src/a/one.txt"], payload=b"one!!", digest="h-one")
    await _give_content(session, made["src/two.txt"], payload=b"two", digest="h-two")
    return drive, made


async def _queue(
    repo: FilesRepo, ctx: ActingContext, source: FileNode, destination: FileNode
) -> OperationId:
    ops = Operations(repo, ctx, FakeClock(now=EPOCH))
    async with repo.transaction():
        return await start_copy(repo, ops, node=source, dest_parent=destination)


def _ids(made: dict[str, Any]) -> dict[str, uuid.UUID]:
    """The seeded ids, taken before any expiry: an ORM row read after
    ``expire_all`` would reload, and these tests assert across that boundary."""
    return {name: node.id for name, node in made.items()}


async def _shape(session: AsyncSession, root_id: uuid.UUID) -> set[tuple[str, str, int]]:
    """Every live node at or under ``root_id`` as (relative path, hash, size).

    Built by walking ``parent_id`` from the row, so it is an independent read of
    the tree rather than a restatement of what the copy wrote into ``path_ids``.
    """
    rows = (
        await session.execute(
            text(
                "WITH RECURSIVE walk AS ("
                "  SELECT id, parent_id, name, size, head_version_id, ''::text AS rel "
                "  FROM file_nodes WHERE id = :root "
                "  UNION ALL "
                "  SELECT n.id, n.parent_id, n.name, n.size, n.head_version_id, "
                "    walk.rel || '/' || encode(n.name, 'escape') "
                "  FROM file_nodes AS n JOIN walk ON n.parent_id = walk.id "
                "  WHERE n.trashed_at IS NULL) "
                "SELECT walk.rel, COALESCE(v.content_hash, ''), walk.size "
                "FROM walk LEFT JOIN file_versions AS v ON v.id = walk.head_version_id"
            ),
            {"root": root_id},
        )
    ).all()
    return {(str(row[0]), str(row[1]), int(row[2])) for row in rows}


async def _root_bytes(session: AsyncSession, drive_id: uuid.UUID, org_id: uuid.UUID) -> int:
    """The logical bytes the drive's pending deltas add up to."""
    row = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(bytes_delta), 0) FROM file_dir_stats_deltas "
                "WHERE org_team_id = :org AND node_id IN "
                "(SELECT id FROM file_nodes WHERE drive_id = :drive)"
            ),
            {"org": org_id, "drive": drive_id},
        )
    ).first()
    assert row is not None
    return int(row[0])


async def _new_root(session: AsyncSession, repo: FilesRepo, op_id: OperationId) -> FileNode:
    plan = await copy_module.load_copy_plan(repo, op_id)
    assert plan.new_root_id is not None
    node = await session.get(FileNode, uuid.UUID(str(plan.new_root_id)))
    assert node is not None
    return node


async def test_a_three_level_tree_copies_with_the_same_hashes(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """The copy is the source's shape, byte-for-byte, sharing every hash."""
    _drive, made = await _seed(files_session, files_factory)
    ids = _ids(made)
    ctx = _ctx(files_org)
    op_id = await _queue(repo, ctx, made["src"], made["dst"])

    await run_copy(repo, ctx, op_id, batch=2)

    files_session.expire_all()
    new_root = await _new_root(files_session, repo, op_id)
    source_shape = await _shape(files_session, ids["src"])
    copy_shape = await _shape(files_session, new_root.id)
    assert copy_shape == source_shape
    assert new_root.id != ids["src"]
    assert (await Operations(repo, ctx, FakeClock(now=EPOCH)).get(op_id)).state == "done"


async def test_the_copy_shares_the_source_objects_and_never_calls_the_store(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """Dedup: new version rows, the same content hashes, zero store calls."""
    _drive, made = await _seed(files_session, files_factory)
    drive_id = _drive.id
    ctx = _ctx(files_org)
    op_id = await _queue(repo, ctx, made["src"], made["dst"])

    await run_copy(repo, ctx, op_id, batch=2)

    files_session.expire_all()
    new_root = await _new_root(files_session, repo, op_id)
    rows = (
        await files_session.execute(
            text(
                "SELECT v.content_hash, count(DISTINCT v.id), count(DISTINCT v.node_id) "
                "FROM file_versions AS v JOIN file_nodes AS n ON n.head_version_id = v.id "
                "WHERE n.drive_id = :drive GROUP BY v.content_hash ORDER BY v.content_hash"
            ),
            {"drive": drive_id},
        )
    ).all()
    # Each of the three hashes now has TWO version rows on TWO nodes: the copy
    # made its own rows and pointed them at the source's bytes.
    assert [(str(row[0]), int(row[1]), int(row[2])) for row in rows] == [
        ("h-deep", 2, 2),
        ("h-one", 2, 2),
        ("h-two", 2, 2),
    ]
    # The bytes are shared; the provenance is not. Every copied version says
    # `copy`, while the sources keep the `upload` they were made under — a
    # version that claimed to be the upload it was copied from would mislead
    # the history pane and the retention policy alike.
    provenance = (
        await files_session.execute(
            text(
                "SELECT v.source, count(*) FROM file_versions AS v "
                "JOIN file_nodes AS n ON n.head_version_id = v.id "
                "WHERE n.drive_id = :drive GROUP BY 1 ORDER BY 1"
            ),
            {"drive": drive_id},
        )
    ).all()
    assert [(str(row[0]), int(row[1])) for row in provenance] == [("copy", 3), ("upload", 3)]
    inline = (
        (
            await files_session.execute(
                text(
                    "SELECT DISTINCT v.inline_bytes FROM file_versions AS v "
                    "JOIN file_nodes AS n ON n.head_version_id = v.id "
                    "WHERE n.drive_id = :drive AND n.path_ids <@ CAST(:path AS ltree) "
                    "ORDER BY 1"
                ),
                {"path": str(new_root.path_ids), "drive": drive_id},
            )
        )
        .scalars()
        .all()
    )
    assert sorted(one for one in inline if one is not None) == [b"deep", b"one!!", b"two"]


async def test_the_quota_settles_the_copied_logical_bytes(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """A copy costs the org its logical bytes even though no byte moved."""
    _drive, made = await _seed(files_session, files_factory)
    drive_id, org_id = _drive.id, _drive.org_team_id
    ctx = _ctx(files_org)
    before = await _root_bytes(files_session, drive_id, org_id)
    op_id = await _queue(repo, ctx, made["src"], made["dst"])

    await run_copy(repo, ctx, op_id, batch=2)

    files_session.expire_all()
    after = await _root_bytes(files_session, drive_id, org_id)
    # deep(4) + one(5) + two(3): the tree's whole logical weight, once.
    assert after - before == 12


async def test_a_kill_at_a_batch_boundary_resumes_to_the_same_end_state(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """A crash mid-copy resumes at its cursor and lands the identical tree."""
    _drive, made = await _seed(files_session, files_factory)
    drive_id = _drive.id
    ids = _ids(made)
    ctx = _ctx(files_org)
    op_id = await _queue(repo, ctx, made["src"], made["dst"])

    checkpoints = PausingCheckpoints()
    checkpoints.kill("copy.after_batch")
    with pytest.raises(CheckpointKilled):
        await run_copy(repo, ctx, op_id, batch=1, checkpoints=checkpoints)

    files_session.expire_all()
    new_root_id = (await _new_root(files_session, repo, op_id)).id
    partial = await _shape(files_session, new_root_id)
    source_shape = await _shape(files_session, ids["src"])
    assert partial != source_shape, "the kill must have stopped the copy part-way"
    new_root_path = str((await _new_root(files_session, repo, op_id)).path_ids)

    await resume_copy(repo, ctx, op_id, batch=1)

    files_session.expire_all()
    assert await _shape(files_session, new_root_id) == source_shape
    assert (await Operations(repo, ctx, FakeClock(now=EPOCH)).get(op_id)).state == "done"
    # And the resume did not duplicate what the first attempt committed.
    count = (
        await files_session.execute(
            text(
                "SELECT count(*) FROM file_nodes WHERE drive_id = :drive "
                "AND path_ids <@ CAST(:path AS ltree) AND trashed_at IS NULL"
            ),
            {"path": new_root_path, "drive": drive_id},
        )
    ).scalar_one()
    assert int(count) == len(source_shape)


async def test_a_cancel_leaves_a_consistent_tree(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """A cancelled copy stops at a boundary; what it made is a whole tree."""
    _drive, made = await _seed(files_session, files_factory)
    drive_id = _drive.id
    ids = _ids(made)
    ctx = _ctx(files_org)
    ops = Operations(repo, ctx, FakeClock(now=EPOCH))
    op_id = await _queue(repo, ctx, made["src"], made["dst"])

    class CancelAfterFirstBatch(PausingCheckpoints):
        async def reach(self, name: str) -> None:
            if name == "copy.after_batch":
                await ops.cancel(op_id)
            await super().reach(name)

    await run_copy(repo, ctx, op_id, batch=1, checkpoints=CancelAfterFirstBatch())

    assert (await ops.get(op_id)).state == "cancelled"
    files_session.expire_all()
    new_root = await _new_root(files_session, repo, op_id)
    partial = await _shape(files_session, new_root.id)
    assert 0 < len(partial) < len(await _shape(files_session, ids["src"]))
    # Consistent: every row's path is its parent's plus its own label, and its
    # depth is one past its parent's — checked against the parent row, not
    # against what the loop meant to write.
    broken = (
        await files_session.execute(
            text(
                "SELECT count(*) FROM file_nodes AS n JOIN file_nodes AS p ON p.id = n.parent_id "
                "WHERE n.drive_id = :drive AND n.path_ids <@ CAST(:path AS ltree) "
                "AND (subpath(n.path_ids, 0, nlevel(n.path_ids) - 1) <> p.path_ids "
                "OR n.depth <> p.depth + 1)"
            ),
            {"path": str(new_root.path_ids), "drive": drive_id},
        )
    ).scalar_one()
    assert int(broken) == 0


async def test_the_root_is_renamed_on_collision_and_descendants_are_not(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """Only the root can collide at the destination, so only the root renames."""
    _drive, made = await _seed(files_session, files_factory)
    ctx = _ctx(files_org)
    # Copying src into its own parent is the collision case: a sibling already
    # holds the name "src", so the ROOT renames and nothing below it does.
    root = await files_session.get(FileNode, made["src"].parent_id)
    assert root is not None
    op_id = await _queue(repo, ctx, made["src"], root)
    await run_copy(repo, ctx, op_id, batch=10)

    files_session.expire_all()
    new_root = await _new_root(files_session, repo, op_id)
    assert bytes(new_root.name) != b"src"
    assert b"src" in bytes(new_root.name)
    names = (
        (
            await files_session.execute(
                text(
                    "SELECT encode(name, 'escape') FROM file_nodes "
                    "WHERE parent_id = (SELECT id FROM file_nodes WHERE parent_id = :root "
                    "AND encode(name, 'escape') = 'a') ORDER BY 1"
                ),
                {"root": new_root.id},
            )
        )
        .scalars()
        .all()
    )
    assert list(names) == ["b", "one.txt"]


async def test_acls_are_not_copied_so_the_destination_inheritance_applies(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """A grant on the source is not carried onto the copy."""
    _drive, made = await _seed(files_session, files_factory)
    drive_id = _drive.id
    ids = _ids(made)
    ctx = _ctx(files_org)
    await files_factory.grant(made["src"], "user", files_org.member_id, "viewer")
    op_id = await _queue(repo, ctx, made["src"], made["dst"])

    await run_copy(repo, ctx, op_id, batch=10)

    files_session.expire_all()
    new_root = await _new_root(files_session, repo, op_id)
    shares = (
        await files_session.execute(
            text(
                "SELECT count(*) FROM file_shares AS s JOIN file_nodes AS n ON n.id = s.node_id "
                "WHERE n.drive_id = :drive AND n.path_ids <@ CAST(:path AS ltree)"
            ),
            {"path": str(new_root.path_ids), "drive": drive_id},
        )
    ).scalar_one()
    assert int(shares) == 0
    # The source's own grant is untouched: not copying is not revoking.
    kept = (
        await files_session.execute(
            text("SELECT count(*) FROM file_shares WHERE node_id = :node"),
            {"node": ids["src"]},
        )
    ).scalar_one()
    assert int(kept) == 1


async def test_a_copy_of_a_single_file_needs_no_batch(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """The boundary below the batch loop: a root with no descendants at all."""
    _drive, made = await _seed(files_session, files_factory)
    ids = _ids(made)
    ctx = _ctx(files_org)
    op_id = await _queue(repo, ctx, made["src/two.txt"], made["dst"])

    await run_copy(repo, ctx, op_id, batch=10)

    files_session.expire_all()
    new_root = await _new_root(files_session, repo, op_id)
    assert bytes(new_root.name) == b"two.txt"
    assert new_root.parent_id == ids["dst"]
    version = await files_session.get(FileVersion, new_root.head_version_id)
    assert version is not None
    assert version.content_hash == "h-two"
    assert version.node_id == new_root.id


async def test_a_finished_copy_refuses_to_be_resumed(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """A done operation has nothing to continue; saying so beats copying twice."""
    from alkera_core.files.errors import PreconditionFailed

    _drive, made = await _seed(files_session, files_factory)
    ctx = _ctx(files_org)
    op_id = await _queue(repo, ctx, made["src"], made["dst"])
    await run_copy(repo, ctx, op_id, batch=10)

    with pytest.raises(PreconditionFailed):
        await resume_copy(repo, ctx, op_id, batch=10)


async def test_the_operation_names_the_new_root_when_it_finishes(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """A client polling the operation learns which node it now owns."""
    _drive, made = await _seed(files_session, files_factory)
    ctx = _ctx(files_org)
    op_id = await _queue(repo, ctx, made["src"], made["dst"])

    await run_copy(repo, ctx, op_id, batch=10)

    state = await Operations(repo, ctx, FakeClock(now=EPOCH)).get(op_id)
    files_session.expire_all()
    new_root = await _new_root(files_session, repo, op_id)
    assert state.result_node_id == new_root.id
    history_rows = (
        (
            await files_session.execute(
                text("SELECT kind FROM file_history WHERE op_id = :op"),
                {"op": op_id},
            )
        )
        .scalars()
        .all()
    )
    # One row for the root; the descendants are implied by a subtree read. Its
    # kind is `copy`, not the `create` a fresh node takes: the operations feed
    # and the undo path both go looking for the copy.
    assert list(history_rows) == ["copy"]


async def test_a_drive_hands_the_copy_fresh_inos_and_never_reuses_one(
    files_session: AsyncSession, files_org: FilesOrg, files_factory: FilesFactory, repo: FilesRepo
) -> None:
    """The copies take new inos off the drive's block, past every issued one."""
    _drive, made = await _seed(files_session, files_factory)
    drive_id = _drive.id
    ctx = _ctx(files_org)
    watermark = int(
        (
            await files_session.execute(
                text("SELECT next_ino FROM file_drives WHERE id = :drive"),
                {"drive": drive_id},
            )
        ).scalar_one()
    )
    op_id = await _queue(repo, ctx, made["src"], made["dst"])

    await run_copy(repo, ctx, op_id, batch=2)

    files_session.expire_all()
    new_root = await _new_root(files_session, repo, op_id)
    inos = (
        (
            await files_session.execute(
                text(
                    "SELECT ino FROM file_nodes WHERE drive_id = :drive "
                    "AND path_ids <@ CAST(:path AS ltree) ORDER BY ino"
                ),
                {"path": str(new_root.path_ids), "drive": drive_id},
            )
        )
        .scalars()
        .all()
    )
    assert min(inos) >= watermark
    assert len(set(inos)) == len(inos)
    after = await files_session.get(FileDrive, drive_id)
    assert after is not None
    assert after.next_ino > max(inos)


async def test_a_copied_file_reads_back_through_the_fail_closed_verifier(
    files_session: AsyncSession,
    files_org: FilesOrg,
    files_factory: FilesFactory,
    repo: FilesRepo,
    tmp_path: Path,
) -> None:
    """The copy's own bytes come back out of ``open``, block digests and all.

    ``open`` refuses a version whose ``version_metadata`` records no per-block
    digest for a block it is about to yield, so a copy that carried only the
    content hash is unreadable however intact the object it names is. The
    payload is one byte past the inline cap so the read goes through the store
    and the block check runs on a real object rather than on a column.
    """
    clock = FakeClock(now=EPOCH)
    drive = await files_factory.drive()
    made = await files_factory.tree("src/ src/big.bin dst/", drive=drive)
    domain_id = DomainId(drive.dedup_domain_id)
    store = _RootedDomainStore(
        FilesystemStore(tmp_path / "domains" / str(domain_id), clock=clock.now), domain_id
    )
    ctx = _ctx(files_org)
    service = ContentService(repo, ctx, clock, store)
    size = settings.files_inline_max_bytes + 1
    payload = (hashlib.sha256(b"copy-verifier").digest() * (size // 32 + 1))[:size]
    source_file_id = made["src/big.bin"].id
    written = await service.put_version(
        NodeId(source_file_id), _stream(payload), size_declared=size, if_match=0
    )

    op_id = await _queue(repo, ctx, made["src"], made["dst"])
    await run_copy(repo, ctx, op_id)

    files_session.expire_all()
    row = (
        await files_session.execute(
            text(
                "SELECT n.head_version_id FROM file_nodes AS n "
                "WHERE n.org_team_id = :org AND n.metadata ->> 'copied_from' = :src"
            ),
            {"org": files_org.org_team_id, "src": str(source_file_id)},
        )
    ).first()
    assert row is not None and row[0] is not None
    copied_version_id = VersionId(uuid.UUID(str(row[0])))
    assert copied_version_id != written.id

    chunks = [chunk async for chunk in await service.open(copied_version_id)]
    assert b"".join(chunks) == payload
