"""Saves of one person's preferences made at the same moment.

Each save is a merge: what is stored, with the caller's fields on top. Every
save here runs in its own session and holds its transaction open until all of
them have merged (or a second has passed), as a request does until it ends, so
the saves overlap the way requests from several tabs and a desktop client do.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import UserOrgPreference, UserPreference
from backend.services.org import preferences as preferences_service
from sqlalchemy import func, select
from tests.conftest import OrgWithAdmin

pytestmark = pytest.mark.asyncio


async def _save_together(org_admin: OrgWithAdmin, saves: list[dict[str, Any]]) -> list[Any]:
    """Run each save in a session of its own, every one holding its
    transaction until all have merged or a second has passed."""
    merged_count = 0
    all_merged = asyncio.Event()

    async def save(incoming: dict[str, Any]) -> Any:
        nonlocal merged_count
        async with AsyncSessionLocal() as db:
            await preferences_service.merge(
                db, user_id=org_admin.admin_id, org_id=org_admin.org_id, incoming=incoming
            )
            merged_count += 1
            if merged_count == len(saves):
                all_merged.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(all_merged.wait(), 1.0)
            await db.commit()
            return incoming

    runs: list[Awaitable[Any]] = [save(incoming) for incoming in saves]
    return await asyncio.wait_for(asyncio.gather(*runs, return_exceptions=True), 60)


async def _stored(org_admin: OrgWithAdmin) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        document = await preferences_service.load(
            db, user_id=org_admin.admin_id, org_id=org_admin.org_id
        )
        return document.model_dump(mode="json")


async def _count(model: Any, org_admin: OrgWithAdmin) -> int:
    async with AsyncSessionLocal() as db:
        return int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.user_id == org_admin.admin_id)
                )
            ).scalar_one()
        )


async def test_ten_first_saves_at_once_are_all_stored_on_one_row(
    org_admin: OrgWithAdmin,
) -> None:
    """Ten saves from a person who has never saved: each is taken, none is
    refused as a duplicate, and they end on one row."""
    outcomes = await _save_together(org_admin, [{"theme": f"theme-{n}"} for n in range(10)])

    assert [outcome for outcome in outcomes if isinstance(outcome, BaseException)] == []
    assert await _count(UserPreference, org_admin) == 1
    assert (await _stored(org_admin))["theme"] in {f"theme-{n}" for n in range(10)}


@pytest.mark.parametrize(
    "saves",
    [
        pytest.param(
            [
                {"theme": "dark"},
                {"show_banner": False},
                {"reduce_motion": True},
                {"tool_card_border": False},
                {"telemetry_enabled": False},
                {"model_efforts": {"claude-haiku-4.5": "high"}},
            ],
            id="six-fields-on-an-existing-row",
        ),
    ],
)
async def test_saves_of_different_fields_at_once_keep_every_field(
    org_admin: OrgWithAdmin,
    saves: list[dict[str, Any]],
    make_saved: Callable[[], Awaitable[None]],
) -> None:
    """Six saves, each of a different field, made at once: every field each
    one set is stored. A save that read before another committed and wrote
    back its whole document used to drop the other's field without a word."""
    await make_saved()
    outcomes = await _save_together(org_admin, saves)

    assert [outcome for outcome in outcomes if isinstance(outcome, BaseException)] == []
    stored = await _stored(org_admin)
    assert {
        "theme": stored["theme"],
        "show_banner": stored["show_banner"],
        "reduce_motion": stored["reduce_motion"],
        "tool_card_border": stored["tool_card_border"],
        "telemetry_enabled": stored["telemetry_enabled"],
        "model_efforts": stored["model_efforts"],
    } == {
        "theme": "dark",
        "show_banner": False,
        "reduce_motion": True,
        "tool_card_border": False,
        "telemetry_enabled": False,
        "model_efforts": {"claude-haiku-4.5": "high"},
    }
    assert await _count(UserOrgPreference, org_admin) == 1


@pytest.fixture
def make_saved(org_admin: OrgWithAdmin) -> Callable[[], Awaitable[None]]:
    """One earlier save, so the race is between writers of an existing row."""

    async def made() -> None:
        async with AsyncSessionLocal() as db:
            await preferences_service.merge(
                db, user_id=org_admin.admin_id, org_id=org_admin.org_id, incoming={"theme": ""}
            )
            await db.commit()

    return made
