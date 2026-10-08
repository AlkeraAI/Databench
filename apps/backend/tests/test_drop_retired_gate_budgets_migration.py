"""Migration ``0212_drop_retired_gate_budgets`` against the real schema: the
retired per-repo budget table is gone at head, a downgrade puts it back as it
was, and the revision upgrades again over the result. The retired columns the
previous release still names in its inserts are left alone throughout."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_core.db.schema_head import EXPECTED_SCHEMA_HEAD
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch

_BACKEND = Path(__file__).resolve().parents[1]
_MIGRATION = _BACKEND / "alembic" / "versions" / "0212_drop_retired_gate_budgets.py"
_REVISION = _MIGRATION.name.split("_", 1)[0]
_PARENT = "0211"

_Column = tuple[str, str, str | None]

#: The retired columns this revision must NOT drop, as (data_type, is_nullable,
#: column_default), spelled the way the catalog reports what their creating
#: migrations made.
_KEPT_COLUMNS: dict[tuple[str, str], _Column] = {
    ("org_settings", "gate_pr_budget_nanos"): ("bigint", "YES", None),
    ("org_settings", "gate_monthly_budget_nanos"): ("bigint", "YES", None),
    ("gate_runs", "spend_nanos"): ("bigint", "NO", "'0'::bigint"),
    ("connection_verifications", "vantage_kind"): (
        "character varying",
        "NO",
        "'server'::character varying",
    ),
}

#: The retired table's columns, in the same shape.
_TABLE_COLUMNS: dict[str, _Column] = {
    "id": ("uuid", "NO", None),
    "org_team_id": ("uuid", "NO", None),
    "repo": ("character varying", "NO", None),
    "pr_budget_nanos": ("bigint", "NO", None),
    "created_at": ("timestamp with time zone", "NO", "now()"),
    "updated_at": ("timestamp with time zone", "NO", "now()"),
}

#: The retired table's constraints, by name, as Postgres prints each definition.
_TABLE_CONSTRAINTS = {
    "gate_repo_budgets_pkey": "PRIMARY KEY (id)",
    "gate_repo_budgets_org_team_id_fkey": (
        "FOREIGN KEY (org_team_id) REFERENCES teams(id) ON DELETE CASCADE"
    ),
    "uq_gate_repo_budgets_org_repo": "UNIQUE (org_team_id, repo)",
}


async def _columns(session: AsyncSession, table: str) -> dict[str, _Column]:
    rows = await session.execute(
        text(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    )
    return {
        str(name): (str(kind), str(nullable), None if default is None else str(default))
        for name, kind, nullable, default in rows
    }


async def _kept_columns(session: AsyncSession) -> dict[tuple[str, str], _Column | None]:
    return {
        (table, column): (await _columns(session, table)).get(column)
        for table, column in _KEPT_COLUMNS
    }


async def _table_constraints(session: AsyncSession) -> dict[str, str] | None:
    exists = (await session.execute(text("SELECT to_regclass('gate_repo_budgets')"))).scalar()
    if exists is None:
        return None
    rows = await session.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'gate_repo_budgets'::regclass"
        )
    )
    return {str(name): str(definition) for name, definition in rows}


def test_the_drop_is_part_of_the_schema_the_code_expects() -> None:
    assert _MIGRATION.exists()
    assert _REVISION == "0212"
    assert int(EXPECTED_SCHEMA_HEAD) >= int(_REVISION)


@pytest.mark.asyncio
async def test_the_retired_table_is_gone_and_comes_back_on_downgrade() -> None:
    async with migration_scratch() as db:
        async with db.session() as session:
            assert await _table_constraints(session) is None
            assert await _columns(session, "gate_repo_budgets") == {}
            assert await _kept_columns(session) == _KEPT_COLUMNS

        await db.downgrade(_PARENT)
        async with db.session() as session:
            assert await _columns(session, "gate_repo_budgets") == _TABLE_COLUMNS
            assert await _table_constraints(session) == _TABLE_CONSTRAINTS
            assert await _kept_columns(session) == _KEPT_COLUMNS

        await db.upgrade()
        async with db.session() as session:
            assert await _table_constraints(session) is None
            assert await _kept_columns(session) == _KEPT_COLUMNS
