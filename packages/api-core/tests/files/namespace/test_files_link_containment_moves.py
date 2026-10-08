"""A move or a copy never turns a stored link into one pointing out of the drive.

A relative link is read from the folder it sits in, so ``../../../x`` stays
inside three folders down and climbs above the root two folders down. A create refuses
an escaping target; these pin the two writes that change where a link sits
without creating it: a move (inline, and the queued kind a large folder takes)
and a copy, whose descendants are inserted by the runner rather than created.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files import errors
from alkera_core.files import namespace as namespace_module
from alkera_core.files.clock import FakeClock
from alkera_core.files.copy import run_copy, start_copy
from alkera_core.files.ids import DriveId, NodeId
from alkera_core.files.namespace import Namespace
from alkera_core.files.ops import Operations
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
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


async def _root(repo: FilesRepo, drive: FileDrive) -> FileNode:
    assert drive.root_node_id is not None
    node = await repo.node(NodeId(drive.root_node_id))
    assert node is not None
    return node


async def _tree(
    repo: FilesRepo, namespace: Namespace, drive: FileDrive, *, link: bytes, kind: str = "relative"
) -> dict[str, FileNode]:
    """``/a/b/pkg/`` holding ``pkg/link -> link`` (made where it stays inside:
    three folders under the root, so ``../../../x`` reaches the root itself),
    and ``/shallow/`` and ``/a/b/c/deep/`` to move it to."""
    async with repo.transaction():
        root = await _root(repo, drive)
        made: dict[str, FileNode] = {}

        async def folder(path: str, parent: FileNode) -> FileNode:
            node = await namespace.create(
                DriveId(drive.id), NodeId(parent.id), "folder", path.rsplit("/", 1)[-1].encode()
            )
            made[path] = node
            return node

        a = await folder("a", root)
        b = await folder("a/b", a)
        pkg = await folder("a/b/pkg", b)
        await folder("shallow", root)
        c = await folder("a/b/c", b)
        await folder("a/b/c/deep", c)
        made["a/b/pkg/link"] = await namespace.create(
            DriveId(drive.id),
            NodeId(pkg.id),
            "symlink",
            b"link",
            symlink_target=link,
            symlink_kind=kind,
        )
    return made


async def _parent_of(session: AsyncSession, node_id: uuid.UUID) -> uuid.UUID:
    row = (
        await session.execute(
            text("SELECT parent_id FROM file_nodes WHERE id = :id"), {"id": node_id}
        )
    ).scalar_one()
    return uuid.UUID(str(row))


@pytest.mark.parametrize(
    ("link", "kind", "to", "moved"),
    [
        pytest.param(b"../../../x", "relative", "shallow", False, id="a-climb-that-would-escape"),
        pytest.param(b"a/../../../../x", "relative", "shallow", False, id="a-nested-escape"),
        pytest.param(b"../../x", "relative", "shallow", True, id="a-climb-to-the-root-still-fits"),
        pytest.param(b"../x", "relative", "shallow", True, id="a-short-climb-fits"),
        pytest.param(b"../../x", "relative", "a/b/c/deep", True, id="deeper-is-always-inside"),
        pytest.param(b"sibling", "relative", "shallow", True, id="no-climb-at-all"),
        pytest.param(b"/data/x", "canonical", "shallow", True, id="an-org-path-does-not-move"),
    ],
)
async def test_a_move_that_would_point_a_link_out_of_the_drive_is_refused(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
    link: bytes,
    kind: str,
    to: str,
    moved: bool,
) -> None:
    drive = await files_factory.drive()
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    made = await _tree(repo, namespace, drive, link=link, kind=kind)
    pkg, destination = made["a/b/pkg"], made[to]

    async with repo.transaction():
        current = await repo.node(NodeId(pkg.id))
        assert current is not None
        if moved:
            await namespace.move(NodeId(pkg.id), NodeId(destination.id), if_match=current.etag)
        else:
            with pytest.raises(errors.InvalidRequest) as refused:
                await namespace.move(NodeId(pkg.id), NodeId(destination.id), if_match=current.etag)
            assert refused.value.code == "files.link_outside_tree"
            assert refused.value.status == 422

    parent = await _parent_of(files_session, pkg.id)
    assert parent == (destination.id if moved else made["a/b"].id)


async def test_a_queued_large_move_is_refused_before_it_is_queued(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder over the inline cap becomes an operation the runner applies
    later, without this check: so it is made when the move is asked for, and
    no operation is queued."""
    monkeypatch.setattr(namespace_module, "MAX_INLINE_MOVE_NODES", 0)
    drive = await files_factory.drive()
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    made = await _tree(repo, namespace, drive, link=b"../../../x")
    pkg = made["a/b/pkg"]
    before = (await files_session.execute(text("SELECT count(*) FROM file_ops"))).scalar_one()

    async with repo.transaction():
        current = await repo.node(NodeId(pkg.id))
        assert current is not None
        with pytest.raises(errors.InvalidRequest) as refused:
            await namespace.move(NodeId(pkg.id), NodeId(made["shallow"].id), if_match=current.etag)
    assert refused.value.code == "files.link_outside_tree"
    after = (await files_session.execute(text("SELECT count(*) FROM file_ops"))).scalar_one()
    assert after == before


async def test_a_link_already_pointing_out_does_not_hold_a_move_back(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    """A link stored before the rule already points out wherever it sits; the
    move makes it no worse, so the folder still moves."""
    drive = await files_factory.drive()
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    made = await _tree(repo, namespace, drive, link=b"sibling")
    await files_session.execute(
        text("UPDATE file_nodes SET symlink_target = :t, symlink_kind = 'host' WHERE id = :id"),
        {"t": b"/etc/passwd", "id": made["a/b/pkg/link"].id},
    )
    await files_session.commit()
    pkg = made["a/b/pkg"]

    async with repo.transaction():
        current = await repo.node(NodeId(pkg.id))
        assert current is not None
        await namespace.move(NodeId(pkg.id), NodeId(made["shallow"].id), if_match=current.etag)

    assert await _parent_of(files_session, pkg.id) == made["shallow"].id


async def _copy(repo: FilesRepo, org: FilesOrg, source: FileNode, destination: FileNode) -> Any:
    ops = Operations(repo, _ctx(org), FakeClock(now=EPOCH))
    async with repo.transaction():
        return await start_copy(repo, ops, node=source, dest_parent=destination)


async def test_a_copy_that_would_point_a_link_out_of_the_drive_is_refused_before_it_queues(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
) -> None:
    drive = await files_factory.drive()
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    made = await _tree(repo, namespace, drive, link=b"../../../x")
    before = (await files_session.execute(text("SELECT count(*) FROM file_ops"))).scalar_one()

    with pytest.raises(errors.InvalidRequest) as refused:
        await _copy(repo, files_org, made["a/b/pkg"], made["shallow"])

    assert refused.value.code == "files.link_outside_tree"
    after = (await files_session.execute(text("SELECT count(*) FROM file_ops"))).scalar_one()
    assert after == before


@pytest.mark.parametrize(
    ("link", "kind"),
    [
        pytest.param(b"../x", "relative", id="a-relative-link-that-still-fits"),
        pytest.param(b"/data/x", "canonical", id="an-org-path"),
    ],
)
async def test_a_copied_link_keeps_its_target_and_its_kind(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
    files_session: AsyncSession,
    link: bytes,
    kind: str,
) -> None:
    """The runner inserts descendants itself, not through a create: the copy
    of a link carries the same text and the same kind, so a canonical link is
    still re-anchored at the machine that pulls it."""
    drive = await files_factory.drive()
    namespace = Namespace(repo, _ctx(files_org), clock, None)
    made = await _tree(repo, namespace, drive, link=link, kind=kind)
    op_id = await _copy(repo, files_org, made["a/b/pkg"], made["shallow"])

    await run_copy(repo, _ctx(files_org), op_id, batch=10)

    rows = (
        await files_session.execute(
            text(
                "SELECT n.symlink_target, n.symlink_kind FROM file_nodes n "
                "JOIN file_nodes p ON p.id = n.parent_id "
                "WHERE n.kind = 'symlink' AND p.parent_id = :shallow"
            ),
            {"shallow": made["shallow"].id},
        )
    ).all()
    assert [(bytes(r.symlink_target), r.symlink_kind) for r in rows] == [(link, kind)]
