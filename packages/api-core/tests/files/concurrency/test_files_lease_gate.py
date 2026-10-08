"""Every writer under a lease queues on the leased folder, and nobody else does.

The holder's tree report takes no drive row: it takes the leased folder, then
the lease row, then rows beneath it. That is safe only if every other writer
under the lease takes the same folder before any row inside it, which
``FilesRepo.lock_node`` does for all of them. The holder here is a second real
session with the folder's row locked and uncommitted -- what a running report
looks like to everyone else -- and the writer runs with a short
``lock_timeout``, so "queued behind it" is a raised ``55P03`` rather than a
hang or a sleep.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.clock import FakeClock, SystemClock
from alkera_core.files.ids import NodeId, OrgScope
from alkera_core.files.leases import LeaseService
from alkera_core.files.namespace import Namespace
from alkera_core.files.repo import FilesRepo
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.files._kit.factory import FilesFactory, FilesOrg
from tests.files._kit.fencing import held_by

pytestmark = pytest.mark.asyncio

LOCK_TIMEOUT = "55P03"


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


def _sqlstate(exc: BaseException) -> str | None:
    seen: BaseException | None = exc
    while seen is not None:
        original = getattr(seen, "orig", None)
        state = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if state is not None:
            return str(state)
        seen = seen.__cause__ or seen.__context__
    return None


class Leased:
    def __init__(self, org: FilesOrg, tree: dict[str, Any], epoch: int) -> None:
        self.org = org
        self.tree = tree
        self.epoch = epoch

    @property
    def scope(self) -> OrgScope:
        return OrgScope(org_team_id=self.org.org_team_id)


@pytest.fixture
async def leased(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> Leased:
    drive = await files_factory.drive()
    tree = await files_factory.tree(
        "box/ box/inner/ box/inner/deep/ box/inner/deep/a.txt box/b.txt home/ home/c.txt",
        drive=drive,
    )
    ctx = _ctx(files_org)
    async with repo.transaction():
        grant = await LeaseService(repo, ctx, clock).acquire(
            NodeId(tree["box"].id), instance_id="box-1", machine_id="box-mbp"
        )
    return Leased(files_org, tree, grant.epoch)


async def _hold(session: Any, node_id: Any) -> None:
    """Lock one folder row and keep the transaction open."""
    await session.execute(
        text("SELECT id FROM file_nodes WHERE id = :id FOR UPDATE"), {"id": node_id}
    )


async def _rename(session: Any, leased: Leased, path: str, name: bytes, *, fenced: bool) -> bytes:
    repo = FilesRepo(session, leased.scope)
    ctx = _ctx(leased.org)
    node = leased.tree[path]
    async with repo.transaction():
        await session.execute(text("SET LOCAL lock_timeout = '300ms'"))
        renamed = await Namespace(repo, ctx, SystemClock()).rename(
            NodeId(node.id),
            name,
            if_match=int(node.etag),
            lease=held_by(ctx, leased.epoch, "box-1") if fenced else None,
        )
        return bytes(renamed.name)


async def test_a_rename_deep_under_a_lease_queues_on_the_leased_folder(
    leased: Leased, sessions: Callable[..., Awaitable[list[Any]]]
) -> None:
    """The holder's batch holds only the leased folder; a rename two levels
    below it still waits for it, because that folder is what orders it
    against the batch's writes to the rows it is about to rewrite."""
    holder, writer = await sessions(2)
    await _hold(holder, leased.tree["box"].id)
    try:
        with pytest.raises(DBAPIError) as waited:
            await _rename(writer, leased, "box/inner/deep/a.txt", b"z.txt", fenced=True)
        assert _sqlstate(waited.value) == LOCK_TIMEOUT
    finally:
        await holder.rollback()
    renamed = await _rename(writer, leased, "box/inner/deep/a.txt", b"z.txt", fenced=True)
    assert renamed == b"z.txt"


async def test_a_rename_outside_the_lease_does_not_wait_for_its_folder(
    leased: Leased, sessions: Callable[..., Awaitable[list[Any]]]
) -> None:
    holder, writer = await sessions(2)
    await _hold(holder, leased.tree["box"].id)
    try:
        renamed = await _rename(writer, leased, "home/c.txt", b"d.txt", fenced=False)
    finally:
        await holder.rollback()
    assert renamed == b"d.txt"


async def test_the_gate_is_the_outermost_leased_folder_and_none_outside_every_lease(
    leased: Leased, repo: FilesRepo, clock: FakeClock
) -> None:
    """A holder may lease a folder inside one it already holds; the outer
    lease's report writes under the inner one, so both leases' writers must
    meet on the outer folder, not each on its own."""
    async with repo.transaction():
        await LeaseService(repo, _ctx(leased.org), clock).acquire(
            NodeId(leased.tree["box/inner"].id), instance_id="box-2", machine_id="box-mbp"
        )
    async with repo.transaction():
        under_inner = await repo.lock_lease_gate(NodeId(leased.tree["box/inner/deep/a.txt"].id))
        inner_itself = await repo.lock_lease_gate(NodeId(leased.tree["box/inner"].id))
        under_outer = await repo.lock_lease_gate(NodeId(leased.tree["box/b.txt"].id))
        outside = await repo.lock_lease_gate(NodeId(leased.tree["home/c.txt"].id))
    box = leased.tree["box"].id
    assert (under_inner, inner_itself, under_outer, outside) == (box, box, box, None)


async def test_a_released_lease_gates_nothing(
    leased: Leased, repo: FilesRepo, clock: FakeClock
) -> None:
    async with repo.transaction():
        await LeaseService(repo, _ctx(leased.org), clock).release(
            NodeId(leased.tree["box"].id), epoch=leased.epoch, instance_id="box-1"
        )
    async with repo.transaction():
        gate = await repo.lock_lease_gate(NodeId(leased.tree["box/b.txt"].id))
    assert gate is None
