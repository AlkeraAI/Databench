"""The org's default machine for new workspaces, driven through the real app
and real Postgres.

The setting: an org admin sets it to a live machine of the org and clears it
with null; another org's machine, a deleted one and an id that names nothing
are the same 404; anyone else is refused; each decision is on record. A
deleted default reads as none, and deleting the machine clears it.

Where it applies: a project workspace created with no ``machine_pin`` is
pinned to the default when its creator may use it, and so is a member's main
workspace made later. A ``machine_pin`` sent (a machine, or null for the
default placement) wins, and a creator outside the default's audience gets an
unpinned workspace, not a refusal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import EventOutbox, OrgAuditEvent, User, WorkspaceObject
from alkera_core.models.org_machines import OrgComputeSettings, OrgMachine
from backend.services.org import memberships as membership_service
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests._org_machine_helpers import Grant, make_org_machine, make_team
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows, pytest.mark.usefixtures("files_on")]

SETTINGS = "/api/v1/org/compute/settings"
MACHINES = "/api/v1/org/machines"
WORKSPACES = "/api/v1/workspaces"


@pytest.fixture(autouse=True)
def _saas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "self_hosted", False)
    for tier in ("free", "plus", "pro", "enterprise"):
        monkeypatch.setattr(settings, f"machine_quota_{tier}", 5)


@pytest.fixture(autouse=True)
def _no_reconcile_nudge(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reconcile nudge talks to Temporal; a test here never needs it."""
    from backend.services.infra import task_queue

    async def skip() -> bool:
        return True

    monkeypatch.setattr(task_queue, "nudge_org_machine_reconcile", skip)


@contextmanager
def _projects() -> Iterator[None]:
    previous = settings.workspaces_multi_chat
    settings.workspaces_multi_chat = True
    try:
        yield
    finally:
        settings.workspaces_multi_chat = previous


@asynccontextmanager
async def _as(email: str, password: str) -> AsyncIterator[AsyncClient]:
    async with app_client() as browser:
        await login(browser, email, password)
        yield browser


async def _member(session: AsyncSession, org: OrgWithAdmin) -> tuple[User, str]:
    user, password = await make_member(session, org_id=org.org_id, verified=True)
    assert password is not None
    return user, password


async def _machine(
    session: AsyncSession, org: OrgWithAdmin, audience: tuple[Grant, ...] = (("org",),)
) -> OrgMachine:
    machine, _ = await make_org_machine(
        session, org_id=org.org_id, operator_id=org.admin_id, audience=audience
    )
    return machine


async def _set_default(org_id: UUID, machine_id: UUID | None) -> None:
    """The setting as the back-fill or an earlier admin left it."""
    async with AsyncSessionLocal() as db:
        row = await db.get(OrgComputeSettings, org_id)
        if row is None:
            db.add(OrgComputeSettings(org_team_id=org_id, default_org_machine_id=machine_id))
        else:
            row.default_org_machine_id = machine_id
        await db.commit()


async def _stored_default(org_id: UUID) -> UUID | None:
    async with AsyncSessionLocal() as db:
        row = await db.get(OrgComputeSettings, org_id)
        return row.default_org_machine_id if row is not None else None


async def _put(browser: AsyncClient, body: dict[str, Any]) -> Response:
    current = await browser.get(SETTINGS)
    assert current.status_code == 200, current.text
    return await browser.put(
        SETTINGS, json=body, headers={"If-Match": str(current.json()["version"])}
    )


def _without_trace(resp: Response) -> dict[str, Any]:
    body = resp.json()
    return {**body, "error": {k: v for k, v in body["error"].items() if k != "trace_id"}}


async def _decisions(org_id: UUID) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox.payload)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == "org_machine",
            )
            .order_by(EventOutbox.id)
        )
        return [(p["effect"], p["reason"]) for p in rows.scalars().all()]


async def _pin_of(workspace_id: str) -> str | None:
    async with AsyncSessionLocal() as db:
        row = await db.get(WorkspaceObject, UUID(workspace_id))
        assert row is not None
        pin = (row.spec or {}).get("machine_pin")
        return str(pin) if pin is not None else None


async def _create(browser: AsyncClient, **body: Any) -> str:
    with _projects():
        resp = await browser.post(WORKSPACES, json={"title": "Model training", **body})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


# --------------------------------------------------------------------------- #
# the setting
# --------------------------------------------------------------------------- #


async def test_an_org_admin_sets_and_clears_the_default_and_both_are_on_record(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine = await _machine(real_session, org_admin)
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        before = await browser.get(SETTINGS)
        chosen = await _put(browser, {"default_org_machine_id": str(machine.id)})
        listed = await browser.get(MACHINES)
        # A write that does not name the default keeps it.
        other_field = await _put(browser, {"min_awake_pool": 1})
        cleared = await _put(browser, {"default_org_machine_id": None})
    assert before.json()["default_org_machine_id"] is None
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["default_org_machine_id"] == str(machine.id)
    assert [(m["id"], m["org_default"]) for m in listed.json()] == [(str(machine.id), True)]
    assert other_field.json()["default_org_machine_id"] == str(machine.id)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["default_org_machine_id"] is None
    assert await _stored_default(org_admin.org_id) is None
    reasons = await _decisions(org_admin.org_id)
    assert ("allow", "org_admin_sets_settings") in reasons
    assert reasons.count(("allow", "org_admin_sets_settings")) == 3
    async with AsyncSessionLocal() as db:
        details = (
            (
                await db.execute(
                    select(OrgAuditEvent.detail)
                    .where(
                        OrgAuditEvent.org_team_id == org_admin.org_id,
                        OrgAuditEvent.action == "org.compute_settings_changed",
                    )
                    .order_by(OrgAuditEvent.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert [d.get("default_org_machine_id", "absent") for d in details] == [
        str(machine.id),
        "absent",
        None,
    ]


async def test_another_orgs_machine_a_deleted_one_and_a_missing_id_are_the_same_404(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    current = await _machine(real_session, org_admin)
    await _set_default(org_admin.org_id, current.id)
    foreign, _ = await make_org_machine(
        real_session, org_id=platform_admin.org_id, operator_id=platform_admin.admin_id
    )
    deleted, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, look="deleted"
    )
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        answers = [
            await _put(browser, {"default_org_machine_id": str(target)})
            for target in (foreign.id, deleted.id, uuid4(), "not-a-uuid")
        ]
    assert [a.status_code for a in answers] == [404] * 4
    assert len({str(_without_trace(a)) for a in answers}) == 1
    # Nothing changed.
    assert await _stored_default(org_admin.org_id) == current.id


async def test_only_an_org_admin_may_set_it(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine = await _machine(real_session, org_admin)
    member, password = await _member(real_session, org_admin)
    async with _as(member.email, password) as browser:
        read = await browser.get(SETTINGS)
        write = await browser.put(SETTINGS, json={"default_org_machine_id": str(machine.id)})
    assert read.status_code == 403
    assert write.status_code == 403
    assert await _stored_default(org_admin.org_id) is None
    assert ("deny", "settings_need_org_admin") in await _decisions(org_admin.org_id)


async def test_a_deleted_default_reads_as_none(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine = await _machine(real_session, org_admin)
    await _set_default(org_admin.org_id, machine.id)
    async with AsyncSessionLocal() as db:
        # Deleted without going through the delete path that clears it.
        row = await db.get(OrgMachine, machine.id)
        assert row is not None
        row.deleted_at = datetime.now(UTC)
        await db.commit()
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        read = await browser.get(SETTINGS)
    assert read.json()["default_org_machine_id"] is None


async def test_deleting_the_machine_clears_the_default(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine = await _machine(real_session, org_admin)
    kept = await _machine(real_session, org_admin)
    async with _as(org_admin.admin_email, org_admin.admin_password) as browser:
        await _put(browser, {"default_org_machine_id": str(machine.id)})
        before = (await browser.get(SETTINGS)).json()["version"]
        deleted_other = await browser.delete(f"{MACHINES}/{kept.id}")
        unchanged = (await browser.get(SETTINGS)).json()
        deleted = await browser.delete(f"{MACHINES}/{machine.id}")
        after = (await browser.get(SETTINGS)).json()
    assert deleted_other.status_code == 202
    # Deleting another machine leaves the default alone.
    assert unchanged["default_org_machine_id"] == str(machine.id)
    assert unchanged["version"] == before
    assert deleted.status_code == 202
    assert after["default_org_machine_id"] is None
    assert after["version"] == before + 1
    assert await _stored_default(org_admin.org_id) is None


# --------------------------------------------------------------------------- #
# where it applies
# --------------------------------------------------------------------------- #


async def test_a_new_workspace_runs_on_the_default(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    machine = await _machine(real_session, org_admin)
    member, password = await _member(real_session, org_admin)
    async with _as(member.email, password) as browser:
        before = await _create(browser)
        await _set_default(org_admin.org_id, machine.id)
        after = await _create(browser, title="Second")
    # With no default the workspace takes the default placement.
    assert await _pin_of(before) is None
    assert await _pin_of(after) == str(machine.id)


async def test_a_run_on_choice_wins_over_the_default(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    default = await _machine(real_session, org_admin)
    chosen = await _machine(real_session, org_admin)
    await _set_default(org_admin.org_id, default.id)
    member, password = await _member(real_session, org_admin)
    async with _as(member.email, password) as browser:
        on_chosen = await _create(browser, machine_pin=str(chosen.id))
        on_shared = await _create(browser, title="Shared", machine_pin=None)
    assert await _pin_of(on_chosen) == str(chosen.id)
    assert await _pin_of(on_shared) is None


@pytest.mark.parametrize(
    "outside",
    [
        pytest.param("audience", id="a-default-not-shared-with-the-creator"),
        pytest.param("deleted", id="a-default-since-deleted"),
    ],
)
async def test_a_default_the_creator_may_not_use_leaves_the_workspace_unpinned(
    real_session: AsyncSession, org_admin: OrgWithAdmin, outside: str
) -> None:
    member, password = await _member(real_session, org_admin)
    someone, _ = await _member(real_session, org_admin)
    if outside == "audience":
        machine = await _machine(real_session, org_admin, audience=(("user", someone.id),))
    else:
        machine, _ = await make_org_machine(
            real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, look="deleted"
        )
    await _set_default(org_admin.org_id, machine.id)
    async with _as(member.email, password) as browser:
        workspace = await _create(browser)
    assert await _pin_of(workspace) is None


async def test_the_default_reaches_the_creator_through_their_team(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    team = await make_team(real_session, org_id=org_admin.org_id)
    member, password = await _member(real_session, org_admin)
    await membership_service.add_member(real_session, team_id=team.id, user_id=member.id)
    await real_session.commit()
    machine = await _machine(real_session, org_admin, audience=(("team", team.id),))
    await _set_default(org_admin.org_id, machine.id)
    async with _as(member.email, password) as browser:
        workspace = await _create(browser)
    assert await _pin_of(workspace) == str(machine.id)


@pytest.mark.parametrize(
    ("in_audience", "pinned"),
    [
        pytest.param(True, True, id="a-member-who-may-use-it"),
        pytest.param(False, False, id="a-member-outside-its-audience"),
    ],
)
async def test_a_main_workspace_made_later_runs_on_the_default(
    real_session: AsyncSession, org_admin: OrgWithAdmin, in_audience: bool, pinned: bool
) -> None:
    member, password = await _member(real_session, org_admin)
    someone, _ = await _member(real_session, org_admin)
    machine = await _machine(
        real_session, org_admin, audience=(("user", member.id if in_audience else someone.id),)
    )
    await _set_default(org_admin.org_id, machine.id)
    async with _as(member.email, password) as browser:
        main = await browser.get(f"{WORKSPACES}/main")
    assert main.status_code == 200, main.text
    assert main.json()["kind"] == "main"
    assert await _pin_of(main.json()["id"]) == (str(machine.id) if pinned else None)
