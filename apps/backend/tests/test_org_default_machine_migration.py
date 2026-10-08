"""Revision 0196: a self-serve org's back-filled box is its default for new
workspaces.

Driven by Alembic against a scratch copy: seed the schema before 0194 with an
``org_compute_assignments`` row (the box 0194 turns into an org machine), go
up to 0195, then through 0196, and check which orgs got a default. Then down
(the column goes) and up again (the back-fill repeats), and a re-run over a
schema that already has it (nothing is bumped twice).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from alembic import command
from alkera_core.config import settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.migration_harness import migration_scratch
from tests.test_org_machines_migration import _seed

pytestmark = [pytest.mark.asyncio]

_BEFORE_MACHINES = "0195"
_PARENT = "0197"
_REVISION = "0198"


async def _scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar()


async def _bare_org(session: AsyncSession) -> uuid.UUID:
    org = uuid.uuid4()
    await session.execute(
        text("INSERT INTO teams (id, name, is_root) VALUES (:id, :name, true)"),
        {"id": org, "name": f"om-default-{org.hex[:8]}"},
    )
    await session.commit()
    return org


async def _setting(session: AsyncSession, org: uuid.UUID) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text(
                    "SELECT default_org_machine_id, shared_pool_fallback, version "
                    "FROM org_compute_settings WHERE org_team_id = :org"
                ),
                {"org": org},
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


async def _machine_of(session: AsyncSession, org: uuid.UUID) -> uuid.UUID:
    machine: uuid.UUID = await _scalar(
        session, "SELECT id FROM org_machines WHERE org_team_id = :org", org=org
    )
    return machine


async def _has_column(session: AsyncSession) -> bool:
    found = await _scalar(
        session,
        "SELECT count(*) FROM information_schema.columns WHERE table_name = "
        "'org_compute_settings' AND column_name = 'default_org_machine_id'",
    )
    return bool(found)


@pytest.mark.parametrize(
    "settings_row",
    [
        pytest.param(True, id="the-orgs-settings-row-gains-the-default"),
        pytest.param(False, id="an-org-without-a-settings-row-gets-one"),
    ],
)
async def test_a_self_serve_orgs_box_becomes_its_default_and_round_trips(
    monkeypatch: pytest.MonkeyPatch, settings_row: bool
) -> None:
    monkeypatch.setattr(settings, "self_hosted", False)
    async with migration_scratch() as scratch:
        await scratch.downgrade(_BEFORE_MACHINES)
        async with scratch.session() as session:
            self_serve = await _seed(session, plan="none")
            enterprise = await _seed(session, plan="enterprise")
            bare = await _bare_org(session)

        await scratch.upgrade(_PARENT)
        async with scratch.session() as session:
            assert not await _has_column(session)
            if not settings_row:
                await session.execute(
                    text("DELETE FROM org_compute_settings WHERE org_team_id = :org"),
                    {"org": self_serve["org"]},
                )
                await session.commit()
            box = await _machine_of(session, self_serve["org"])
            enterprise_box = await _machine_of(session, enterprise["org"])

        await scratch.upgrade(_REVISION)
        async with scratch.session() as session:
            first = await _setting(session, self_serve["org"])
            assert first is not None
            assert first["default_org_machine_id"] == box
            # 0194 carried the assignment's fallback; a row made here takes
            # the column's default.
            assert first["shared_pool_fallback"] is (not settings_row)
            # The enterprise org's box went into its org pool: no default.
            enterprise_setting = await _setting(session, enterprise["org"])
            assert enterprise_setting is not None
            assert enterprise_setting["default_org_machine_id"] is None
            assert enterprise_box is not None
            # An org with no box has nothing to default to, and no row.
            assert await _setting(session, bare) is None

        # Re-run over a schema that already has it: nothing changes.
        command.stamp(scratch.config, _PARENT)
        await scratch.upgrade(_REVISION)
        async with scratch.session() as session:
            assert await _setting(session, self_serve["org"]) == first

        await scratch.downgrade(_PARENT)
        async with scratch.session() as session:
            assert not await _has_column(session)
            assert (
                await _scalar(
                    session,
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'fk_org_compute_settings_default_org_machine_tenant'",
                )
                == 0
            )

        await scratch.upgrade(_REVISION)
        async with scratch.session() as session:
            again = await _setting(session, self_serve["org"])
            assert again is not None
            assert again["default_org_machine_id"] == box


async def test_a_default_already_chosen_is_kept_and_a_lost_machine_clears_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "self_hosted", False)
    async with migration_scratch() as scratch:
        await scratch.downgrade(_BEFORE_MACHINES)
        async with scratch.session() as session:
            ids = await _seed(session, plan="none")
        await scratch.upgrade(_REVISION)
        async with scratch.session() as session:
            box = await _machine_of(session, ids["org"])
            other = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO org_machines (id, org_team_id, owner_team_id, offering_id, "
                    "name, acquisition, use_mode, storage_gb, desired_power) "
                    "SELECT :id, org_team_id, owner_team_id, offering_id, 'Second', "
                    "'purchased', 'assigned', 1, 'off' FROM org_machines WHERE id = :box"
                ),
                {"id": other, "box": box},
            )
            await session.execute(
                text(
                    "UPDATE org_compute_settings SET default_org_machine_id = :m "
                    "WHERE org_team_id = :org"
                ),
                {"m": other, "org": ids["org"]},
            )
            await session.commit()

        command.stamp(scratch.config, _PARENT)
        await scratch.upgrade(_REVISION)
        async with scratch.session() as session:
            chosen = await _setting(session, ids["org"])
            assert chosen is not None
            assert chosen["default_org_machine_id"] == other
            # Losing the machine clears the default and nothing else.
            await session.execute(text("DELETE FROM org_machines WHERE id = :m"), {"m": other})
            await session.commit()
            cleared = await _setting(session, ids["org"])
            assert cleared is not None
            assert cleared["default_org_machine_id"] is None
            assert cleared["shared_pool_fallback"] is chosen["shared_pool_fallback"]
