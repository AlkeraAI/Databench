"""The index on a chat's workspace serves the workspace's readers and nobody else's.

``ix_workspace_objects_chat_workspace`` is partial on the live chats that name
a workspace. Its predicate includes ``workspace_id IS NOT NULL`` on purpose: an
index whose predicate were only the live chats would be interchangeable with
the bound-machine index on every query over live chats, and the planner could
serve a box's machine listing from the workspace index instead. A query that
names a workspace implies the extra clause; a query that names a machine does
not, so each reader gets its own index.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import WorkspaceObject
from backend.services import workspaces
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

WORKSPACE_INDEX = "ix_workspace_objects_chat_workspace"
MACHINE_INDEX = "ix_workspace_objects_chat_machine"


def _index_names(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        if "Index Name" in node:
            found.add(str(node["Index Name"]))
        for value in node.values():
            found |= _index_names(value)
    elif isinstance(node, list):
        for item in node:
            found |= _index_names(item)
    return found


async def _indexes_used_by(session: AsyncSession, stmt: Any) -> set[str]:
    compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = (await session.execute(text(f"EXPLAIN (FORMAT JSON) {compiled}"))).scalar_one()
    return _index_names(json.loads(plan) if isinstance(plan, str) else plan)


def _live_chats() -> Any:
    return select(WorkspaceObject.id).where(
        WorkspaceObject.type == "chat", WorkspaceObject.deleted_at == 0
    )


async def test_the_chats_of_a_workspace_are_read_through_its_index() -> None:
    """The read ``chats_by_workspace`` issues is one the workspace index serves.

    On a near-empty table the planner may pick any index whose predicate the
    query implies, the bound-machine one included, on cost alone. So the
    machine index is dropped inside the transaction (DDL is transactional, the
    rollback restores it) and the question asked is the one that matters: can
    this index serve this read at all."""
    stmt = workspaces.chats_in_statement([uuid4(), uuid4()])
    async with AsyncSessionLocal() as session:
        await session.begin()
        try:
            await session.execute(text(f"DROP INDEX {MACHINE_INDEX}"))
            assert WORKSPACE_INDEX in await _indexes_used_by(session, stmt)
        finally:
            await session.rollback()


async def test_a_query_over_every_live_chat_is_never_read_through_the_workspace_index() -> None:
    """Asking for live chats without naming a workspace does not imply the
    index's ``workspace_id IS NOT NULL``, so the planner cannot use it there."""
    async with AsyncSessionLocal() as session, session.begin():
        assert WORKSPACE_INDEX not in await _indexes_used_by(session, _live_chats())


async def test_a_machines_chats_are_never_read_through_the_workspace_index() -> None:
    by_machine = _live_chats().where(WorkspaceObject.spec["machine_id"].astext == str(uuid4()))
    async with AsyncSessionLocal() as session, session.begin():
        used = await _indexes_used_by(session, by_machine)
    assert WORKSPACE_INDEX not in used
    assert MACHINE_INDEX in used
