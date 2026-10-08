"""How many project workspaces one person may own in one org.

The deployment sets the default (``WORKSPACES_MAX_PROJECTS_PER_MEMBER``,
200); platform staff override it per org through the admin settings route, and
an org admin cannot. A create past the cap is a 409 with a stable code and a
sentence naming the limit. Main workspaces and ended ones do not count, a
retried ``client_id`` is never refused by it, and two creates racing at one
below the cap leave exactly one standing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import WorkspaceObject
from alkera_core.models.workspace_object import DEFAULT_NAMESPACE
from httpx import AsyncClient
from sqlalchemy import func, select, text
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = pytest.mark.asyncio

WORKSPACES = "/api/v1/workspaces"
CAP_CODE = "workspace_project_cap_reached"


def _settings_url(org_id: object) -> str:
    return f"/admin/v1/orgs/{org_id}/settings"


@pytest.fixture(autouse=True)
def multi_chat(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "workspaces_multi_chat", True)
    yield


async def _create(client: AsyncClient, title: str, client_id: str | None = None) -> Any:
    body: dict[str, str] = {"title": title}
    if client_id is not None:
        body["client_id"] = client_id
    return await client.post(WORKSPACES, json=body)


async def _fill(client: AsyncClient, count: int) -> list[dict[str, Any]]:
    made = []
    for n in range(count):
        resp = await _create(client, f"Project {n}")
        assert resp.status_code == 201, resp.text
        made.append(resp.json())
    return made


async def _override(platform_admin: OrgWithAdmin, org_id: object, cap: int | None) -> None:
    async with app_client() as staff:
        await login(staff, platform_admin.admin_email, platform_admin.admin_password)
        put = await staff.put(_settings_url(org_id), json={"workspace_project_cap": cap})
        assert put.status_code == 200, put.text
        assert put.json()["workspace_project_cap"] == cap


async def _live_projects(org_id: object) -> int:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        return int(
            await db.scalar(
                select(func.count())
                .select_from(WorkspaceObject)
                .where(
                    WorkspaceObject.org_team_id == org_id,
                    WorkspaceObject.type == "workspace",
                    WorkspaceObject.namespace == DEFAULT_NAMESPACE,
                    WorkspaceObject.deleted_at == 0,
                )
            )
            or 0
        )


def _assert_cap_refusal(resp: Any, cap: int) -> None:
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == CAP_CODE
    assert error["message"] == f"You've reached the limit of {cap} project workspaces."


async def test_at_the_cap_the_next_create_is_refused_and_makes_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "workspaces_max_projects_per_member", 2)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await _fill(client, 2)

    refused = await _create(client, "One too many")

    _assert_cap_refusal(refused, 2)
    assert await _live_projects(org_admin.org_id) == 2


@pytest.mark.parametrize(
    ("override", "admitted"),
    [
        pytest.param(3, 3, id="raised-above-the-default"),
        pytest.param(1, 1, id="lowered-below-the-default"),
    ],
)
async def test_the_org_override_wins_over_the_default(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    override: int,
    admitted: int,
) -> None:
    monkeypatch.setattr(settings, "workspaces_max_projects_per_member", 2)
    await _override(platform_admin, org_admin.org_id, override)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    await _fill(client, admitted)
    _assert_cap_refusal(await _create(client, "Past the override"), override)


async def test_clearing_the_override_returns_the_org_to_the_default(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "workspaces_max_projects_per_member", 2)
    await _override(platform_admin, org_admin.org_id, 1)
    await _override(platform_admin, org_admin.org_id, None)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    await _fill(client, 2)
    _assert_cap_refusal(await _create(client, "Third"), 2)


async def test_an_org_admin_cannot_set_the_override(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The org admin's own settings route applies the rest of the body and
    drops the cap, so their members stay held to the deployment's figure."""
    monkeypatch.setattr(settings, "workspaces_max_projects_per_member", 1)
    await login(client, org_admin.admin_email, org_admin.admin_password)

    put = await client.put(
        "/api/v1/org/settings", json={"workspace_project_cap": 50, "allow_login_github": False}
    )

    assert put.status_code == 200, put.text
    assert (put.json()["allow_login_github"], put.json()["workspace_project_cap"]) == (False, None)
    await _fill(client, 1)
    _assert_cap_refusal(await _create(client, "Second"), 1)


async def test_support_staff_cannot_set_the_override(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.put(_settings_url(org_admin.org_id), json={"workspace_project_cap": 500})
    assert resp.status_code == 403


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        pytest.param(0, False, id="below-min"),
        pytest.param(1, True, id="min"),
        pytest.param(100000, True, id="max"),
        pytest.param(100001, False, id="above-max"),
    ],
)
async def test_the_override_is_bounded(
    client: AsyncClient,
    platform_admin: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    value: int,
    accepted: bool,
) -> None:
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.put(_settings_url(org_admin.org_id), json={"workspace_project_cap": value})
    assert resp.status_code == (200 if accepted else 422), resp.text


async def test_ended_workspaces_do_not_count(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "workspaces_max_projects_per_member", 2)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    first, _second = await _fill(client, 2)
    _assert_cap_refusal(await _create(client, "Third"), 2)

    assert (await client.delete(f"{WORKSPACES}/{first['id']}")).status_code == 204
    again = await _create(client, "Third")

    assert again.status_code == 201, again.text


async def test_main_workspaces_and_other_members_projects_do_not_count(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "workspaces_max_projects_per_member", 1)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.get(f"{WORKSPACES}/main")).status_code == 200
    async with AsyncSessionLocal() as db:
        member, password = await make_member(db, org_id=org_admin.org_id, verified=True)
        await db.commit()
    assert password is not None
    async with app_client() as other:
        await login(other, member.email, password)
        assert (await _create(other, "Theirs")).status_code == 201

    mine = await _create(client, "Mine")

    assert mine.status_code == 201, mine.text


async def test_a_retried_client_id_at_the_cap_returns_the_one_it_made(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "workspaces_max_projects_per_member", 1)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    client_id = f"cap-{uuid.uuid4()}"
    made = await _create(client, "Only", client_id)
    assert made.status_code == 201, made.text

    again = await _create(client, "Only", client_id)

    assert again.status_code == 201, again.text
    assert again.json()["id"] == made.json()["id"]


async def test_two_creates_racing_at_one_below_the_cap_leave_exactly_one(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "workspaces_max_projects_per_member", 3)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    await _fill(client, 2)

    answers = await asyncio.gather(*(_create(client, f"Racer {n}") for n in range(2)))

    assert sorted(a.status_code for a in answers) == [201, 409], [a.text for a in answers]
    _assert_cap_refusal(next(a for a in answers if a.status_code == 409), 3)
    assert await _live_projects(org_admin.org_id) == 3
