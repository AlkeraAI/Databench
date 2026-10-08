"""Migration ``0165_chat_machine_index`` against the real schema: the index on
a chat's bound machine exists as the model spells it, the readers that walk
that binding are served by it, and the revision downgrades to its parent and
back.

``alembic check`` proves the model and the migration agree on the index; what
it cannot see is whether the predicate the readers issue is one the index
admits — a partial index serves a query only when the query implies its
``WHERE`` — so that half is held here with the planner's own answer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alkera_core.db.schema_head import EXPECTED_SCHEMA_HEAD
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models.workspace_object import WorkspaceObject
from backend.services.compute.placement import _stranded_chats
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0165_chat_machine_index.py"
_REVISION = _MIGRATION.name.split("_", 1)[0]
_PARENT = "0164"
INDEX = "ix_workspace_objects_chat_machine"


async def _index_definition(session: AsyncSession) -> str | None:
    row = await session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"), {"name": INDEX}
    )
    value = row.scalar_one_or_none()
    return None if value is None else str(value)


def _plan_index_names(plan: Any) -> set[str]:
    """Every index a plan tree touches, however deep the node sits."""
    found: set[str] = set()
    if isinstance(plan, dict):
        name = plan.get("Index Name")
        if isinstance(name, str):
            found.add(name)
        for value in plan.values():
            found |= _plan_index_names(value)
    elif isinstance(plan, list):
        for item in plan:
            found |= _plan_index_names(item)
    return found


async def _indexes_used_by(session: AsyncSession, stmt: Any) -> set[str]:
    """The indexes the planner picks for ``stmt`` when a sequential scan is
    made expensive — on a near-empty table it would otherwise win on cost
    alone, and the question here is whether the index CAN serve the query."""
    compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    row = await session.execute(text(f"EXPLAIN (FORMAT JSON) {compiled}"))
    plan = row.scalar_one()
    return _plan_index_names(json.loads(plan) if isinstance(plan, str) else plan)


def test_the_index_migration_is_part_of_the_schema_the_code_expects() -> None:
    assert _MIGRATION.exists()
    assert _REVISION == "0165"
    assert int(EXPECTED_SCHEMA_HEAD) >= int(_REVISION)


async def test_the_bound_machine_index_exists_as_the_model_spells_it() -> None:
    """Named as the ORM names it (so a later revision can drop it by name), on
    the bound machine id, partial on the live chat rows."""
    async with AsyncSessionLocal() as session:
        definition = await _index_definition(session)
    assert definition is not None, "the index is not on the migrated schema"
    assert "workspace_objects" in definition
    assert "spec ->> 'machine_id'" in definition
    assert "WHERE" in definition
    assert "'chat'" in definition
    # Postgres spells the epoch column's zero with its type: ``(0)::double precision``.
    assert re.search(r"deleted_at = \(?0\)?", definition), definition
    assert "UNIQUE" not in definition, "many chats bind to one machine"


async def test_the_readers_of_a_machines_chats_are_served_by_the_index() -> None:
    """The predicate every reader issues — the bound machine id over live chat
    rows — is one the partial index admits, whether it names a machine (a
    box's own listing, a release) or asks for the chats no live machine holds
    (placement's stranded set)."""
    machine_id = str(uuid4())
    bound_to = WorkspaceObject.spec["machine_id"].astext
    named = select(WorkspaceObject.id).where(
        WorkspaceObject.type == "chat",
        WorkspaceObject.deleted_at == 0,
        bound_to == machine_id,
    )
    async with AsyncSessionLocal() as session:
        async with session.begin():
            assert INDEX in await _indexes_used_by(session, named)
        async with session.begin():
            assert INDEX in await _indexes_used_by(session, _stranded_chats(None, [machine_id]))


async def test_a_query_outside_the_partial_index_is_not_served_by_it() -> None:
    """A partial index serves only a query that implies its ``WHERE``: the
    same binding asked over every row, deleted chats included, cannot use it —
    the reason the readers spell ``deleted_at = 0`` rather than rely on it."""
    every_row = select(WorkspaceObject.id).where(
        WorkspaceObject.spec["machine_id"].astext == str(uuid4())
    )
    async with AsyncSessionLocal() as session, session.begin():
        assert INDEX not in await _indexes_used_by(session, every_row)


async def test_downgrade_drops_the_index_and_upgrade_restores_it() -> None:
    async with migration_scratch() as db:
        await db.downgrade(_PARENT)
        async with db.session() as session:
            assert await _index_definition(session) is None

        await db.upgrade()
        async with db.session() as session:
            assert await _index_definition(session) is not None
