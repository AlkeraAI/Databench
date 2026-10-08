"""The "N landing" count a listing shows beside a live lease.

The count walks each lease's subtree down ``parent_id``. The spelling matters
because the statement runs as ``alkera_files_app``, under which ``file_nodes``
has FORCE row security: PostgreSQL will not evaluate a non-leakproof operator
— ltree containment among them — ahead of the policy's own qual, so an
``n.path_ids <@ lease.path_ids`` join is only ever a filter, and the statement
compared every file in the org with every lease on the page. A ``Chats``
folder of live chats made that the whole cost of listing it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from alkera_core.authz.enums import CredentialKind, PrincipalKind
from alkera_core.authz.principal import ActingContext, Principal
from alkera_core.files.clock import FakeClock
from alkera_core.files.ids import NodeId
from alkera_core.files.lease_live import LiveEntriesService
from alkera_core.files.leases import LeaseService
from alkera_core.files.repo import FilesRepo
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.files._kit.factory import FilesFactory, FilesOrg

pytestmark = pytest.mark.asyncio

#: Two folders under the lease, a trashed file, a folder named like a file
#: under it, and files beside it that share the leased folder's parent — each
#: is a way for a subtree count to be wrong.
TREE = (
    "chat/ chat/a.md chat/docs/ chat/docs/b.md chat/docs/deep/ chat/docs/deep/c.md "
    "chat/gone.md chat/settled.md chat-2/ chat-2/d.md outside.md"
)
REPORTED = (
    "chat/a.md",
    "chat/docs/b.md",
    "chat/docs/deep/c.md",
    "chat/gone.md",
    "chat-2/d.md",
    "outside.md",
)


def _ctx(org: FilesOrg) -> ActingContext:
    return ActingContext(
        acting_principal=Principal(
            kind=PrincipalKind.USER,
            id=str(org.admin_id),
            org_id=org.org_team_id,
            credential=CredentialKind.JWT,
        )
    )


async def _leased_tree(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> dict[str, uuid.UUID]:
    drive = await files_factory.drive()
    nodes = await files_factory.tree(TREE, drive=drive)
    ids = {name: node.id for name, node in nodes.items()}
    async with repo.transaction():
        await LeaseService(repo, _ctx(files_org), clock).acquire(
            NodeId(ids["chat"]), instance_id="box-1", machine_id="box"
        )
    async with repo.transaction():
        await repo.session.execute(
            text(
                "UPDATE file_leases SET live_cadence = CAST('{\"debounceMs\": 300}' AS jsonb) "
                "WHERE node_id = :node"
            ),
            {"node": ids["chat"]},
        )
        # The holder has reported these files and the drive holds no version of
        # any of them, so each one's bytes are still on their way.
        await repo.session.execute(
            text(
                "UPDATE file_nodes SET holder_size = 7, holder_mtime_ns = mtime_ns "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": [ids[name] for name in REPORTED]},
        )
        await repo.session.execute(
            text("UPDATE file_nodes SET trashed_at = now() WHERE id = :id"),
            {"id": ids["chat/gone.md"]},
        )
    return ids


@contextmanager
def _captured(engine: AsyncEngine) -> Iterator[list[tuple[str, Any]]]:
    seen: list[tuple[str, Any]] = []

    def record(
        _conn: Any, _cursor: Any, statement: str, parameters: Any, _ctx: Any, many: bool
    ) -> None:
        if not many and "file_lease_live_entries" in statement:
            seen.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_the_count_is_every_reported_live_file_at_any_depth_under_the_lease(
    repo: FilesRepo, files_factory: FilesFactory, files_org: FilesOrg, clock: FakeClock
) -> None:
    """Three: ``a.md`` beside the lease's top, ``b.md`` one folder down and
    ``c.md`` two down. Not the trashed file, not the settled one the holder
    never reported, and not the files in the sibling folder or beside it."""
    ids = await _leased_tree(repo, files_factory, files_org, clock)

    async with repo.transaction():
        page = await LiveEntriesService(repo, _ctx(files_org)).facets_for_page(
            [ids["chat/a.md"]], lease_node_ids=[ids["chat"]], now=clock.now()
        )

    assert page.landing == {ids["chat"]: 3}


async def test_the_count_uses_no_ltree_operator_under_the_app_role(
    repo: FilesRepo,
    files_engine: AsyncEngine,
    files_factory: FilesFactory,
    files_org: FilesOrg,
    clock: FakeClock,
) -> None:
    """The statement the page really runs, planned as the role it runs as.

    The whole-table fallbacks are off because a test tree is small enough that
    reading all of it wins on cost whatever the predicate says. An ltree
    operator anywhere in the plan is the per-org comparison coming back: under
    the role it can only be a filter over every file the org holds.
    """
    ids = await _leased_tree(repo, files_factory, files_org, clock)
    with _captured(files_engine) as reads:
        async with repo.transaction():
            await LiveEntriesService(repo, _ctx(files_org)).facets_for_page(
                [ids["chat/a.md"]], lease_node_ids=[ids["chat"]], now=clock.now()
            )
    assert len(reads) == 1

    statement, parameters = reads[0]
    async with repo.transaction() as scoped:
        connection = await scoped.session.connection()
        role = (await connection.exec_driver_sql("SELECT current_user")).scalar_one()
        await connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
        await connection.exec_driver_sql("SET LOCAL enable_bitmapscan = off")
        rows = await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)
        plan = "\n".join(str(row[0]) for row in rows.fetchall())

    assert role == "alkera_files_app"
    assert "<@" not in plan and "@>" not in plan, plan
