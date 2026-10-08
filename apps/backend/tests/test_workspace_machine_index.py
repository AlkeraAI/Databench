"""A pool box's reach is read through indexes on every request it makes.

``held_orgs_query`` asks for the orgs of the chats bound to a machine and of
the workspaces it reports holding. Each arm matches the predicate of its own
partial index, so a box's every request pays two index probes, not a scan of
every object row.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from alkera_core.compute.workspace_lease import held_orgs_query, holds_work_in_query
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import WorkspaceObject
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

CHAT_MACHINE = "ix_workspace_objects_chat_machine"
WORKSPACE_MACHINE = "ix_workspace_objects_workspace_machine"


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


async def _plan(session: AsyncSession, stmt: Any) -> Any:
    compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = (await session.execute(text(f"EXPLAIN (FORMAT JSON) {compiled}"))).scalar_one()
    return json.loads(plan) if isinstance(plan, str) else plan


def _seq_scans(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        if node.get("Node Type") == "Seq Scan":
            found.append(str(node.get("Relation Name")))
        for value in node.values():
            found += _seq_scans(value)
    elif isinstance(node, list):
        for item in node:
            found += _seq_scans(item)
    return found


async def test_both_arms_of_a_boxs_reach_are_index_served() -> None:
    async with AsyncSessionLocal() as session, session.begin():
        plan = await _plan(session, held_orgs_query(str(uuid4())))
    assert {CHAT_MACHINE, WORKSPACE_MACHINE} <= _index_names(plan)
    assert "workspace_objects" not in _seq_scans(plan)


async def test_the_workspace_arm_needs_the_reported_binding_to_use_its_index() -> None:
    """The index covers only workspaces a box reported holding: a read that
    drops the ``binding_authority`` clause does not imply its predicate and
    cannot use it, which is what keeps the reach to reported holdings."""
    unreported = select(WorkspaceObject.org_team_id).where(
        WorkspaceObject.type == "workspace",
        WorkspaceObject.deleted_at == 0,
        WorkspaceObject.spec["machine_id"].astext == str(uuid4()),
    )
    async with AsyncSessionLocal() as session, session.begin():
        assert WORKSPACE_MACHINE not in _index_names(await _plan(session, unreported))


async def test_the_index_is_the_one_the_model_declares() -> None:
    async with AsyncSessionLocal() as session:
        definition = (
            await session.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                {"name": WORKSPACE_MACHINE},
            )
        ).scalar_one()
    assert "(spec ->> 'machine_id'::text)" in definition
    assert "'workspace'::text" in definition
    assert "binding_authority" in definition


async def test_a_boxs_standing_in_one_org_is_index_served() -> None:
    """``holds_work_in`` is asked on every Files drive a pool box names and on
    every worker request: its chat and workspace arms probe their partial
    indexes, and nothing scans the object or lease rows."""
    stmt = holds_work_in_query(str(uuid4()), uuid4())
    async with AsyncSessionLocal() as session, session.begin():
        plan = await _plan(session, stmt)
    assert {CHAT_MACHINE, WORKSPACE_MACHINE} <= _index_names(plan)
    assert not {"workspace_objects", "file_leases"} & set(_seq_scans(plan))
