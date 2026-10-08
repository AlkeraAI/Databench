"""The repair a grant queues has to be run, and the janitor is what runs it.

A grant on a folder marks the subtree ``acl_rewriting`` and writes a queued
``file_ops`` row for the repair. Nothing else in the deployment advances that
row, so until the janitor drains it the descendants keep the cached body they
had before the grant, ``AclRewriteStale`` cannot clear their flag (it refuses
while any operation is queued on the drive — including this one), and every
reader that treats the mark as "no answer" refuses the new grantee.

Both halves are asserted separately on purpose: a pass that cleared the flag
without recomputing the body would leave the node advertising the permissions
of a chain it no longer has, which is a wrong answer rather than a slow one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.principal import Principal as ActingPrincipal
from alkera_core.files import acl, sweepers
from alkera_core.files.authz.grants import ACL_REWRITING, Principal
from alkera_core.files.repo import FilesRepo
from alkera_core.models.files.acl import FileAcl
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.tree import FileNode
from sqlalchemy import select
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=ActingPrincipal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _node(repo: FilesRepo, node_id: uuid.UUID) -> FileNode:
    """Re-read the row, never the identity map: the repair is an ``UPDATE``."""
    got = await repo.session.get(FileNode, node_id, populate_existing=True)
    assert got is not None
    return got


async def _body(repo: FilesRepo, node: FileNode) -> list[dict[str, Any]]:
    """The interned ACEs the node's ``acl_id`` points at."""
    if node.acl_id is None:
        return []
    row = (
        await repo.execute_scoped(select(FileAcl.body).where(FileAcl.id == node.acl_id))
    ).scalar_one()
    return [ace for ace in row if isinstance(ace, dict)]


async def _rewrite_ops(repo: FilesRepo) -> list[FileOp]:
    return list(
        (await repo.execute_scoped(repo.select_ops().where(FileOp.kind == acl.OP_ACL_REWRITE)))
        .scalars()
        .all()
    )


async def test_the_janitor_runs_the_repair_a_grant_only_queued(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """One janitor pass turns the queued rewrite into a done one, and the
    descendant's cache names the grantee it inherited."""
    drive = await files_factory.drive()
    made = await files_factory.tree("top/ top/mid/ top/mid/leaf.txt", drive=drive)
    top, leaf = made["top"], made["top/mid/leaf.txt"]

    async with repo.transaction():
        await acl.grant(
            repo, _ctx(files_org), top, Principal(kind="user", id=files_org.member_id), "writer"
        )

    # What the grant leaves behind: a marked descendant and work nobody has run.
    async with repo.transaction():
        marked = await _node(repo, leaf.id)
        assert marked.state == ACL_REWRITING
        queued = await _rewrite_ops(repo)
        assert [op.state for op in queued] == ["queued"]

    repo.session.expunge_all()
    report = await sweepers.run_janitor(sweepers.SweepDeps(repo=repo, ctx=_ctx(files_org)), NOW)

    async with repo.transaction():
        repaired = await _node(repo, leaf.id)
        assert repaired.state == "live"
        assert [op.state for op in await _rewrite_ops(repo)] == ["done"]
        # Cleared AND recomputed: the flag going away without the body being
        # rebuilt would leave the leaf advertising the chain it had before.
        assert [
            (ace["principal_id"], ace["role"], ace["origin"]) for ace in await _body(repo, repaired)
        ] == [(str(files_org.member_id), "writer", "inherited")]
    assert report.by_name("acl_rewrite_drain").swept >= 1


async def test_the_queued_repair_is_what_blocks_the_stale_flag_sweeper(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg
) -> None:
    """Without the drain the flag sweeper is no recovery: it refuses to clear a
    node while its own drive has an operation queued, which the un-run rewrite
    is — so the mark would stand however long the janitor ran."""
    drive = await files_factory.drive()
    made = await files_factory.tree("top/ top/leaf.txt", drive=drive)
    top, leaf = made["top"], made["top/leaf.txt"]

    async with repo.transaction():
        await acl.grant(
            repo, _ctx(files_org), top, Principal(kind="user", id=files_org.member_id), "reader"
        )
    repo.session.expunge_all()

    # Long past the staleness deadline, and with only the flag sweeper running.
    late = NOW + sweepers.FLAG_STALE_AFTER * 10
    outcome = await sweepers.AclRewriteStale(
        sweepers.SweepDeps(repo=repo, ctx=_ctx(files_org))
    ).run(late)

    assert outcome.swept == 0
    async with repo.transaction():
        assert (await _node(repo, leaf.id)).state == ACL_REWRITING
