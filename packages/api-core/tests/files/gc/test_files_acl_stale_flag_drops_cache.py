"""Clearing a stale `acl_rewriting` flag must not resurrect the revoked share.

A node reads its grants from the interned ``acl_id`` body unless it is flagged
``acl_rewriting``, and nothing that flags a subtree ever clears that body — so
the flag is the *only* thing holding a revoked or moved-away audience out of
the answer. A repair that drops the flag and leaves the cache behind therefore
hands authority back to the union of ancestors the node has already left.

The tests below take the two repairs that can reach that state — the janitor's
``acl_rewrite_stale`` sweeper and ``fsck --repair-safe`` — and ask the only
question that matters: after the repair, what does a reader get?
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from _files_gc_factory import AdminOnlyFactory
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext
from alkera_core.authz.principal import Principal as ActingPrincipal
from alkera_core.files import acl, fsck
from alkera_core.files.authz.grants import FilesGrantSource, Principal
from alkera_core.files.clock import FakeClock
from alkera_core.files.gc import Janitor
from alkera_core.files.ids import AclId, DomainId, OperationId, OrgScope
from alkera_core.files.repo import FilesRepo
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.files.sweepers import AclRewriteStale, SweepDeps
from alkera_core.models.files.ops import FileOp
from alkera_core.models.files.stores import FileDrive
from alkera_core.models.files.tree import FileNode
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files._kit.factory import EPOCH, FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: Well past ``FLAG_STALE_AFTER``, so the sweeper's deadline comparison releases.
NOW = EPOCH.replace(year=EPOCH.year + 2)


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=ActingPrincipal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _node(session: AsyncSession, node_id: uuid.UUID) -> FileNode:
    """Re-read from the database, never from the identity map."""
    got = await session.get(FileNode, node_id, populate_existing=True)
    assert got is not None
    await session.refresh(got)
    return got


async def _latest_op(repo: FilesRepo) -> OperationId:
    return OperationId(
        (await repo.execute_scoped(select(FileOp.id).order_by(FileOp.created_at.desc())))
        .scalars()
        .first()
    )


async def _drain(repo: FilesRepo, op_id: OperationId) -> None:
    while True:
        async with repo.transaction():
            moved = await acl.rewrite(repo, op_id)
        if moved == 0:
            return


async def _roles_for(repo: FilesRepo, session: AsyncSession, node_id: uuid.UUID) -> list[str]:
    """What a reader is served for this node, through the real grant source."""
    async with repo.transaction() as scoped:
        node = await _node(session, node_id)
        chain = list(await scoped.chain(node))
        grants = await FilesGrantSource().grants_for(scoped, node, chain)
    return sorted(grant.role for grant in grants)


async def _abandon_ops(session: AsyncSession, repo: FilesRepo) -> None:
    """Every operation on this tenant ends non-terminally-failed.

    The sweeper only touches a drive with no ``queued``/``running`` operation
    on it, which is exactly the state ``OperationWatchdog`` leaves behind when
    a rewrite worker stops beating: the op is ``failed`` and no longer drives
    the flags it set.
    """
    async with repo.transaction():
        await repo.session.execute(
            repo.update_ops().where(FileOp.state.in_(("queued", "running"))).values(state="failed")
        )
    await session.commit()


async def _age_flags(session: AsyncSession, repo: FilesRepo) -> None:
    """Push every flagged node's ``updated_at`` behind both stale deadlines.

    Measured on Postgres time, not the process's: the sweeper compares against
    the instant it is handed, but ``fsck`` compares against the database's own
    ``now()``, so an age expressed in the test's fake future satisfies only one
    of the two.
    """
    async with repo.transaction():
        await repo.session.execute(
            repo.update_nodes()
            .where(FileNode.state == "acl_rewriting")
            .values(updated_at=text("now() - interval '30 days'"))
        )
    await session.commit()


@pytest.fixture
async def revoked(
    repo: FilesRepo,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    files_session: AsyncSession,
) -> Any:
    """A share on a folder, interned down onto a child, then revoked.

    After this the child is flagged ``acl_rewriting`` with an ``acl_id`` that
    still names the revoked reader — the exact shape a dead rewrite leaves.
    """
    drive = await files_factory.drive()
    made = await files_factory.tree("a/ a/b.txt", drive=drive)
    folder, child = made["a"], made["a/b.txt"]
    ctx = _ctx(files_org)

    async with repo.transaction():
        share = await acl.grant(
            repo, ctx, folder, Principal(kind="user", id=files_org.member_id), "reader"
        )
        op_id = await _latest_op(repo)
    await _drain(repo, op_id)

    # The cache is real and it names the member: without this the test would
    # pass for the wrong reason (no stale body to resurrect).
    cached = await _node(files_session, child.id)
    assert cached.acl_id is not None
    async with repo.transaction() as scoped:
        body = await scoped.acl(AclId(cached.acl_id))
    assert body is not None
    assert str(files_org.member_id) in {ace["principal_id"] for ace in body.body}

    async with repo.transaction():
        await acl.revoke(repo, ctx, await _node(files_session, folder.id), share.id)

    flagged = await _node(files_session, child.id)
    assert flagged.state == "acl_rewriting"
    assert flagged.acl_id == cached.acl_id

    await _abandon_ops(files_session, repo)
    await _age_flags(files_session, repo)
    return child


async def test_the_flag_is_what_hides_the_revoked_share(
    repo: FilesRepo, files_session: AsyncSession, revoked: Any
) -> None:
    """The premise: while the flag stands the revoked reader is already gone."""
    assert await _roles_for(repo, files_session, revoked.id) == []


async def test_sweeping_a_stale_flag_does_not_restore_the_revoked_share(
    repo: FilesRepo, files_org: FilesOrg, files_session: AsyncSession, revoked: Any
) -> None:
    """The bug: clearing the flag alone makes the pre-revoke cache truth again."""
    outcome = await AclRewriteStale(SweepDeps(repo=repo, ctx=_ctx(files_org))).run(NOW)
    assert outcome.swept >= 1

    swept = await _node(files_session, revoked.id)
    assert swept.state == "live"
    # What a reader gets, first: the repair's justification is that the chain
    # stays authoritative, and the chain says nobody.
    assert await _roles_for(repo, files_session, revoked.id) == []
    # And the reason it does — the body the flag invalidated went with it.
    assert swept.acl_id is None


async def test_fsck_repairing_a_flag_without_an_op_does_not_restore_it_either(
    repo: FilesRepo,
    files_org: FilesOrg,
    files_session: AsyncSession,
    revoked: Any,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    """The operator's repair path reaches the same state and must be as safe."""
    drive = (
        await files_session.execute(select(FileNode).where(FileNode.id == revoked.id))
    ).scalar_one()
    domain_id = DomainId(
        (
            await files_session.execute(
                select(FileDrive.dedup_domain_id).where(FileDrive.id == drive.drive_id)
            )
        ).scalar_one()
    )
    store = FilesystemStore(tmp_path / "bucket", clock=clock, layout="bucket")
    janitor = Janitor(lambda scope: FilesRepo(files_session, scope), AdminOnlyFactory(store), clock)

    report = await fsck.run_fsck(
        janitor, org=OrgScope(org_team_id=files_org.org_team_id), domain_id=domain_id
    )
    flags = [f for f in report.findings if f.code == fsck.NODE_FLAG_WITHOUT_OP]
    assert [f.ref_id for f in flags] == [str(revoked.id)]

    repaired = await fsck.run_fsck(
        janitor,
        org=OrgScope(org_team_id=files_org.org_team_id),
        domain_id=domain_id,
        repair_safe=True,
    )
    assert [f.code for f in repaired.repaired] == [fsck.NODE_FLAG_WITHOUT_OP]

    fixed = await _node(files_session, revoked.id)
    assert fixed.state == "live"
    assert await _roles_for(repo, files_session, revoked.id) == []
    assert fixed.acl_id is None
