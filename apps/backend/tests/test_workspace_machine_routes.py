"""``/api/v1/workspaces/{id}/machine``: reading, moving and canceling, driven
through the real app and real Postgres.

Who may move a workspace (Full access or Owner on it, and use of the target),
what is refused before anything changes (someone with a chat the target is not
shared with, named; a second move while one runs; a stopped target its funding
cannot start; a stale ``If-Match``; another org's workspace or machine), what a
move leaves behind (the row, the pin, the announcement, the audit entry, the
workflow started with the move's id), and the ``authz.decision`` row each
decision leaves. The workflow is reached through the Temporal client seam, so
what the backend asks the orchestrator for is what is asserted.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from alkera_core.compute.provider import EC2
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.files.authz.ladder import ROLE_READER, ROLE_WRITER
from alkera_core.models import EventOutbox, OrgAuditEvent, User, WorkspaceObject
from alkera_core.models.compute import POOL_TENANCY, ComputeAllocation
from alkera_core.models.files.tree import FileNode
from alkera_core.models.org_machines import WorkspaceMachineMove
from backend.services.infra import task_queue
from backend.services.workspaces import machine_move
from httpx import AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tests._compute_helpers import hold_with_credential, make_machine_type
from tests._org_machine_helpers import make_offering, make_org_machine, make_team, set_fallback
from tests.chat_shares import files_on  # noqa: F401
from tests.conftest import OrgWithAdmin, app_client, login, make_member, make_org_enterprise

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows, pytest.mark.usefixtures("files_on")]

WORKSPACES = "/api/v1/workspaces"
ROLE_MANAGER = "manager"
ROLE_OWNER_RUNG = "owner"


class _Client:
    """The Temporal client the backend reaches through its seam: records each
    start it is asked for."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    async def start_workflow(self, workflow: str, **kwargs: Any) -> None:
        self.started.append({"workflow": workflow, **kwargs})


@pytest.fixture(autouse=True)
def orchestrator(monkeypatch: pytest.MonkeyPatch) -> _Client:
    client = _Client()

    async def provide() -> Any:
        return client

    monkeypatch.setattr(task_queue, "temporal_client_provider", provide)
    return client


@pytest.fixture(autouse=True)
def _saas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "self_hosted", False)
    for tier in ("free", "plus", "pro", "enterprise"):
        monkeypatch.setattr(settings, f"machine_quota_{tier}", 5)


@pytest.fixture(autouse=True)
def _no_reconcile_nudge(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from backend.services.infra import task_queue

    sent: list[str] = []

    async def record() -> bool:
        sent.append("nudge")
        return True

    monkeypatch.setattr(task_queue, "nudge_org_machine_reconcile", record)
    return sent


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


async def _pool_box(session: AsyncSession, operator: OrgWithAdmin) -> ComputeAllocation:
    """A ready shared pool box: the default placement serves every org."""
    mt = await make_machine_type(session, provider=EC2)
    alloc = ComputeAllocation(
        user_id=operator.admin_id,
        org_team_id=operator.org_id,
        machine_type_id=mt.id,
        lifecycle="workspace",
        name=f"pool-{uuid.uuid4().hex[:8]}",
        tenancy=POOL_TENANCY,
        sandbox="gvisor",
        capacity=6,
        chats_served=0,
        state="ready",
        provider_machine_id=f"pod-{uuid.uuid4().hex[:8]}",
        last_heartbeat_at=datetime.now(UTC),
    )
    session.add(alloc)
    await session.commit()
    await hold_with_credential(session, alloc)
    return alloc


async def _person(
    session: AsyncSession, org: OrgWithAdmin, *, first_name: str = "Member"
) -> tuple[User, str]:
    user, password = await make_member(
        session, org_id=org.org_id, verified=True, first_name=first_name, last_name="Person"
    )
    assert password is not None
    return user, password


async def _workspace(browser: AsyncClient, **body: Any) -> dict[str, Any]:
    with _projects():
        resp = await browser.post(WORKSPACES, json={"title": "Model training", **body})
    assert resp.status_code == 201, resp.text
    created: dict[str, Any] = resp.json()
    return created


async def _grant(browser: AsyncClient, workspace: dict[str, Any], member: User, role: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET LOCAL row_security = off"))
        node = await db.get(FileNode, UUID(workspace["files_node_id"]))
        assert node is not None
        etag = node.etag
    granted = await browser.post(
        f"/api/v1/files/drives/{workspace['files_drive_id']}/items/"
        f"{workspace['files_node_id']}/permissions",
        json={"principal": {"kind": "user", "id": str(member.id)}, "role": role},
        headers={"Idempotency-Key": uuid.uuid4().hex, "If-Match": str(etag)},
    )
    assert granted.status_code == 201, granted.text


async def _chat_in(
    session: AsyncSession, *, org_id: UUID, owner_id: UUID, workspace_id: str
) -> WorkspaceObject:
    chat = WorkspaceObject(
        org_team_id=org_id,
        logical_id=f"chat-{uuid.uuid4().hex[:10]}",
        namespace="workspace",
        type="chat",
        title="chat",
        version=1,
        status="ready",
        spec={"machine_id": None, "machine_status": "none", "workspace_id": workspace_id},
        owner_user_id=owner_id,
        visibility_scope="private",
    )
    session.add(chat)
    await session.commit()
    return chat


def _code(resp: Response) -> str | None:
    body = resp.json()
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return error.get("code")
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail.get("code") if isinstance(detail, dict) else None


def _error(resp: Response) -> dict[str, Any]:
    body = resp.json()
    found = body.get("error") or body.get("detail")
    assert isinstance(found, dict), body
    return found


async def _workspace_row(workspace_id: str) -> WorkspaceObject:
    async with AsyncSessionLocal() as db:
        row = await db.get(WorkspaceObject, UUID(workspace_id))
        assert row is not None
        return row


async def _moves(workspace_id: str) -> list[WorkspaceMachineMove]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(WorkspaceMachineMove)
            .where(WorkspaceMachineMove.workspace_id == UUID(workspace_id))
            .order_by(WorkspaceMachineMove.requested_at)
        )
        return list(rows.scalars().all())


async def _decisions(org_id: UUID, entity: str) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(EventOutbox.payload)
            .where(
                EventOutbox.org_id == org_id,
                EventOutbox.type == "authz.decision",
                EventOutbox.entity == entity,
            )
            .order_by(EventOutbox.id)
        )
        return [(p["effect"], p["reason"]) for p in rows.scalars().all()]


def _without_trace(resp: Response) -> dict[str, Any]:
    error = dict(_error(resp))
    error.pop("trace_id", None)
    return error


def _move(machine_id: UUID | None, **over: Any) -> dict[str, Any]:
    return {"to_org_machine_id": str(machine_id) if machine_id else None, **over}


# --------------------------------------------------------------------------- #
# who may move it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("rung", "status", "code", "reason"),
    [
        pytest.param("creator", 202, None, "owner_moves", id="the-owner"),
        pytest.param(ROLE_OWNER_RUNG, 202, None, "full_access_moves", id="the-owner-rung"),
        pytest.param(ROLE_MANAGER, 202, None, "full_access_moves", id="full-access"),
        pytest.param(
            ROLE_WRITER,
            403,
            "workspace_move_requires_full_access",
            "full_access_required",
            id="an-editor",
        ),
        pytest.param(
            ROLE_READER,
            403,
            "workspace_move_requires_full_access",
            "full_access_required",
            id="a-viewer",
        ),
        pytest.param(
            None, 404, "not_found", "not_in_audience", id="a-member-it-was-not-shared-with"
        ),
    ],
)
async def test_who_may_move_a_workspace(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    orchestrator: _Client,
    rung: str | None,
    status: int,
    code: str | None,
    reason: str,
) -> None:
    owner, owner_pw = await _person(real_session, org_admin)
    other, other_pw = await _person(real_session, org_admin)
    machine, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser)
        if rung not in (None, "creator"):
            await _grant(browser, workspace, other, rung)
    email, password = (owner.email, owner_pw) if rung == "creator" else (other.email, other_pw)
    async with _as(email, password) as browser:
        resp = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine",
            json=_move(machine.id),
            headers={"If-Match": str(workspace["version"])},
        )
    assert resp.status_code == status, resp.text
    assert _code(resp) == code
    assert await _decisions(org_admin.org_id, "workspace_machine") == [
        ("allow" if status == 202 else "deny", reason)
    ]
    moves = await _moves(workspace["id"])
    pin = (await _workspace_row(workspace["id"])).spec.get("machine_pin")
    if status == 202:
        [move] = moves
        assert resp.json()["id"] == str(move.id)
        # Not known until the leaving boxes are asked to save.
        assert resp.json()["flushed_before_switch"] is None
        assert (move.state, move.to_org_machine_id, move.from_org_machine_id) == (
            "requested",
            machine.id,
            None,
        )
        assert pin == str(machine.id)
        [start] = orchestrator.started
        assert start["workflow"] == "workspace.machine_move"
        assert start["id"] == f"workspace.machine_move:{move.id}"
        assert start["args"] == [str(move.id), str(org_admin.org_id), ""]
    else:
        assert moves == []
        assert pin is None
        assert orchestrator.started == []


@pytest.mark.parametrize(
    ("caller", "use_mode", "grant", "status", "code"),
    [
        pytest.param("member", "assigned", "caller", 202, None, id="in-the-audience"),
        pytest.param("member", "assigned", "team", 202, None, id="through-a-team"),
        pytest.param(
            "member", "assigned", "someone_else", 404, "not_found", id="a-machine-they-cannot-see"
        ),
        pytest.param(
            "org_admin",
            "assigned",
            "someone_else",
            403,
            "machine_not_usable",
            id="an-admin-sees-it-and-still-may-not-use-it",
        ),
        pytest.param("member", "pool", None, 202, None, id="a-pool-machine-is-everyones"),
    ],
)
async def test_the_caller_must_be_able_to_use_the_target(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    caller: str,
    use_mode: str,
    grant: str | None,
    status: int,
    code: str | None,
) -> None:
    if caller == "org_admin":
        owner = await real_session.get(User, org_admin.admin_id)
        assert owner is not None
        owner_pw = org_admin.admin_password
    else:
        owner, owner_pw = await _person(real_session, org_admin)
    stranger, _ = await _person(real_session, org_admin)
    team = await make_team(real_session, org_id=org_admin.org_id)
    from backend.services.org import memberships as membership_service

    await membership_service.add_member(real_session, team_id=team.id, user_id=owner.id)
    await real_session.commit()
    audience: tuple[tuple[str, UUID], ...] | tuple[()] = {
        "caller": (("user", owner.id),),
        "team": (("team", team.id),),
        "someone_else": (("user", stranger.id),),
        None: (),
    }[grant]
    machine, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        use_mode=use_mode,
        audience=audience,
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser)
        resp = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(machine.id))
    assert resp.status_code == status, resp.text
    assert _code(resp) == code
    assert len(await _moves(workspace["id"])) == (1 if status == 202 else 0)


# --------------------------------------------------------------------------- #
# what is refused before anything changes
# --------------------------------------------------------------------------- #


async def test_a_target_not_shared_with_someone_who_has_a_chat_names_them(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    # The default placement serves the org: a move to it may go.
    await _pool_box(real_session, org_admin)
    owner, owner_pw = await _person(real_session, org_admin, first_name="Olive")
    shared, _ = await _person(real_session, org_admin, first_name="Sam")
    left_out, _ = await _person(real_session, org_admin, first_name="Lena")
    machine, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        audience=(("user", owner.id), ("user", shared.id)),
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser)
    for person in (owner, shared, left_out):
        await _chat_in(
            real_session, org_id=org_admin.org_id, owner_id=person.id, workspace_id=workspace["id"]
        )
    async with _as(owner.email, owner_pw) as browser:
        refused = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine", json=_move(machine.id)
        )
        to_default = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(None))
    assert refused.status_code == 409, refused.text
    error = _error(refused)
    assert error["code"] == "move_target_not_shared"
    assert error["details"]["names"] == ["Lena Person"]
    # The default placement is everyone's: nobody is left out of it.
    assert to_default.status_code == 202, to_default.text
    [move] = await _moves(workspace["id"])
    assert move.to_org_machine_id is None


async def test_a_second_move_while_one_runs_is_refused(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    owner, owner_pw = await _person(real_session, org_admin)
    first, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    second, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser)
        ok = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(first.id))
        again = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(second.id))
    assert ok.status_code == 202, ok.text
    assert (again.status_code, _code(again)) == (409, "move_in_progress"), again.text
    [move] = await _moves(workspace["id"])
    assert move.to_org_machine_id == first.id
    assert (await _workspace_row(workspace["id"])).spec["machine_pin"] == str(first.id)


async def test_two_requests_racing_past_the_read_are_held_by_the_index(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read of the active move can be passed by a request racing this one;
    the partial unique index is what refuses the second, as the same 409."""
    # The default placement serves the org: a move to it may go.
    await _pool_box(real_session, org_admin)
    owner, owner_pw = await _person(real_session, org_admin)
    machine, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser)
        ok = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(machine.id))

        async def nothing_seen(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(machine_move, "active_move", nothing_seen)
        raced = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(None))
    assert ok.status_code == 202, ok.text
    assert (raced.status_code, _code(raced)) == (409, "move_in_progress"), raced.text
    assert len(await _moves(workspace["id"])) == 1
    assert (await _workspace_row(workspace["id"])).spec["machine_pin"] == str(machine.id)


async def test_a_stopped_target_its_funding_cannot_start_is_402(
    real_session: AsyncSession, org_admin: OrgWithAdmin, _no_reconcile_nudge: list[str]
) -> None:
    owner, owner_pw = await _person(real_session, org_admin)
    pricey = await make_offering(real_session, fixed_rate=10**15)
    stopped, stopped_alloc = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        look="stopped",
        offering=pricey,
    )
    # A wake proves the rate the sleeping machine pinned when it last ran.
    stopped_alloc.price_per_minute_nanos = 10**15
    await real_session.commit()
    running, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        offering=pricey,
    )
    free_stopped, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, look="stopped"
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser)
        broke = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine", json=_move(stopped.id)
        )
        assert (broke.status_code, _code(broke)) == (402, "insufficient_credit"), broke.text
        assert await _moves(workspace["id"]) == []
        # A running machine is already admitted, whatever it costs to start.
        on = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(running.id))
        assert on.status_code == 202, on.text
    other, other_pw = await _person(real_session, org_admin)
    async with _as(other.email, other_pw) as browser:
        second = await _workspace(browser)
        woken = await browser.post(
            f"{WORKSPACES}/{second['id']}/machine", json=_move(free_stopped.id)
        )
    assert woken.status_code == 202, woken.text
    # A stopped target is asked to start as soon as the move is on record.
    assert _no_reconcile_nudge == ["nudge"]


async def test_a_stale_if_match_is_409_and_changes_nothing(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    owner, owner_pw = await _person(real_session, org_admin)
    machine, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser)
        stale = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine",
            json=_move(machine.id),
            headers={"If-Match": str(workspace["version"] + 1)},
        )
        current = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine",
            json=_move(machine.id),
            headers={"If-Match": str(workspace["version"])},
        )
    assert (stale.status_code, _code(stale)) == (409, "stale_write"), stale.text
    assert current.status_code == 202, current.text
    assert (await _workspace_row(workspace["id"])).version == workspace["version"] + 1


async def test_another_orgs_workspace_and_machine_are_not_found(
    real_session: AsyncSession, org_admin: OrgWithAdmin, platform_admin: OrgWithAdmin
) -> None:
    owner, owner_pw = await _person(real_session, org_admin)
    foreign_machine, _ = await make_org_machine(
        real_session, org_id=platform_admin.org_id, operator_id=platform_admin.admin_id
    )
    async with _as(platform_admin.admin_email, platform_admin.admin_password) as browser:
        foreign_workspace = await _workspace(browser)
    async with _as(owner.email, owner_pw) as browser:
        mine = await _workspace(browser)
        onto_theirs = await browser.post(
            f"{WORKSPACES}/{mine['id']}/machine", json=_move(foreign_machine.id)
        )
        missing = await browser.post(f"{WORKSPACES}/{mine['id']}/machine", json=_move(uuid.uuid4()))
        theirs = await browser.post(
            f"{WORKSPACES}/{foreign_workspace['id']}/machine", json=_move(None)
        )
        read_theirs = await browser.get(f"{WORKSPACES}/{foreign_workspace['id']}/machine")
    for resp in (onto_theirs, missing, theirs, read_theirs):
        assert resp.status_code == 404, resp.text
    # Another org's machine says no more than a missing one.
    assert _without_trace(onto_theirs) == _without_trace(missing)
    assert await _moves(mine["id"]) == []
    assert await _moves(foreign_workspace["id"]) == []
    assert await _decisions(org_admin.org_id, "org_machine") == [
        ("deny", "machine_not_visible"),
        ("deny", "machine_not_visible"),
    ]


# --------------------------------------------------------------------------- #
# what a move leaves on record
# --------------------------------------------------------------------------- #


async def test_a_move_is_announced_and_audited_from_and_to(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    # The default placement serves the org: a move to it may go.
    await _pool_box(real_session, org_admin)
    owner, owner_pw = await _person(real_session, org_admin)
    first, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser, machine_pin=str(first.id))
        resp = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(None))
    assert resp.status_code == 202, resp.text
    move_id = resp.json()["id"]
    async with AsyncSessionLocal() as db:
        audit = (
            await db.execute(
                select(OrgAuditEvent).where(
                    OrgAuditEvent.org_team_id == org_admin.org_id,
                    OrgAuditEvent.action == "workspace.machine_moved",
                )
            )
        ).scalar_one()
        frames = (
            (
                await db.execute(
                    select(EventOutbox.payload).where(
                        EventOutbox.org_id == org_admin.org_id,
                        EventOutbox.type == "workspace.machine_move",
                        EventOutbox.entity_id == workspace["id"],
                    )
                )
            )
            .scalars()
            .all()
        )
    assert audit.target == workspace["id"]
    assert (audit.detail["from"], audit.detail["to"], audit.detail["move_id"]) == (
        str(first.id),
        None,
        move_id,
    )
    assert [dict(frame) for frame in frames] == [
        {"move_id": move_id, "state": "requested", "error_code": ""}
    ]
    # Moving to the default clears the pin.
    assert (await _workspace_row(workspace["id"])).spec["machine_pin"] is None


# --------------------------------------------------------------------------- #
# canceling
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("state", "status", "restored"),
    [
        pytest.param("requested", 202, True, id="before-it-started"),
        pytest.param("draining", 202, True, id="while-turns-finish"),
        pytest.param("switching", 409, False, id="not-once-chats-move"),
        pytest.param("waking", 409, False, id="not-while-waking"),
    ],
)
async def test_cancel_puts_the_pin_back_only_before_chats_move(
    real_session: AsyncSession, org_admin: OrgWithAdmin, state: str, status: int, restored: bool
) -> None:
    owner, owner_pw = await _person(real_session, org_admin)
    first, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    second, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser, machine_pin=str(first.id))
        moved = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(second.id))
        assert moved.status_code == 202, moved.text
        async with AsyncSessionLocal() as db:
            row = await db.get(WorkspaceMachineMove, UUID(moved.json()["id"]))
            assert row is not None
            row.state = state
            await db.commit()
        resp = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine/moves/{moved.json()['id']}/cancel"
        )
        unknown = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine/moves/{uuid.uuid4()}/cancel"
        )
    assert resp.status_code == status, resp.text
    assert unknown.status_code == 404, unknown.text
    [move] = await _moves(workspace["id"])
    pin = (await _workspace_row(workspace["id"])).spec["machine_pin"]
    if restored:
        assert move.state == "canceled"
        assert move.finished_at is not None
        assert pin == str(first.id)
    else:
        assert _code(resp) == "move_not_cancelable"
        assert move.state == state
        assert pin == str(second.id)


async def test_an_editor_may_not_cancel_a_move(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    owner, owner_pw = await _person(real_session, org_admin)
    editor, editor_pw = await _person(real_session, org_admin)
    machine, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser)
        await _grant(browser, workspace, editor, ROLE_WRITER)
        moved = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine", json=_move(machine.id)
        )
    async with _as(editor.email, editor_pw) as browser:
        resp = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine/moves/{moved.json()['id']}/cancel"
        )
    assert resp.status_code == 403, resp.text
    [move] = await _moves(workspace["id"])
    assert move.state == "requested"


# --------------------------------------------------------------------------- #
# the read
# --------------------------------------------------------------------------- #


async def test_the_read_names_the_pin_the_move_and_where_the_caller_may_go(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    orchestrator: _Client,
) -> None:
    await _pool_box(real_session, platform_admin)
    owner, owner_pw = await _person(real_session, org_admin)
    viewer, viewer_pw = await _person(real_session, org_admin)
    mine, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        audience=(("user", owner.id),),
        name="A100 Lab",
    )
    hidden, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        audience=(("user", viewer.id),),
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser)
        await _grant(browser, workspace, viewer, ROLE_READER)
        before = await browser.get(f"{WORKSPACES}/{workspace['id']}/machine")
        moved = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(mine.id))
        after = await browser.get(f"{WORKSPACES}/{workspace['id']}/machine")
    async with _as(viewer.email, viewer_pw) as browser:
        as_viewer = await browser.get(f"{WORKSPACES}/{workspace['id']}/machine")
    assert before.status_code == 200, before.text
    first = before.json()
    assert first["card"]["kind"] == "shared"
    assert first["card"]["name"] == "Standard"
    assert first["pin"] is None
    assert first["active_move"] is None
    assert first["can_move"] is True
    assert [t["kind"] for t in first["targets"]] == ["shared", "org_machine"]
    assert first["targets"][1]["org_machine_id"] == str(mine.id)
    assert str(hidden.id) not in {t["org_machine_id"] for t in first["targets"]}
    assert first["targets"][1]["spec"]["rate_per_minute_nanos"] == 0

    second = after.json()
    assert second["pin"] == str(mine.id)
    assert second["card"]["org_machine_id"] == str(mine.id)
    assert second["card"]["name"] == "A100 Lab"
    assert second["active_move"]["id"] == moved.json()["id"]
    assert second["active_move"]["state"] == "requested"

    seen = as_viewer.json()
    assert as_viewer.status_code == 200, as_viewer.text
    assert seen["can_move"] is False
    assert seen["targets"] == []
    # Someone the machine is not shared with sees its card without a rate.
    assert seen["card"]["spec"]["rate_per_minute_nanos"] is None


async def test_the_default_is_offered_only_while_something_serves_the_orgs_chats(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """An Enterprise org with no org pool machine and no shared fallback has
    nowhere for its regular chats: the read offers only its own machines, and
    agrees with ``GET /machines/current``."""
    await make_org_enterprise(org_admin.org_id)
    await set_fallback(real_session, org_id=org_admin.org_id, fallback=False)
    owner, owner_pw = await _person(real_session, org_admin)
    mine, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        audience=(("user", owner.id),),
        name="A100 Lab",
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser, machine_pin=None)
        read = await browser.get(f"{WORKSPACES}/{workspace['id']}/machine")
        current = await browser.get("/api/v1/machines/current")
        await set_fallback(real_session, org_id=org_admin.org_id, fallback=True)
        await _pool_box(real_session, org_admin)
        served = await browser.get(f"{WORKSPACES}/{workspace['id']}/machine")
    assert read.status_code == 200, read.text
    assert current.json()["status"] == "none"
    body = read.json()
    assert body["can_move"] is True
    assert [t["kind"] for t in body["targets"]] == ["org_machine"]
    assert body["targets"][0]["org_machine_id"] == str(mine.id)
    # The fallback back on with a pool box up: the default is offered again, first.
    assert [t["kind"] for t in served.json()["targets"]] == ["shared", "org_machine"]


async def test_reading_a_move_whose_workflow_went_quiet_starts_it_again(
    real_session: AsyncSession, org_admin: OrgWithAdmin, orchestrator: _Client
) -> None:
    owner, owner_pw = await _person(real_session, org_admin)
    machine, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser)
        moved = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine", json=_move(machine.id)
        )
        await browser.get(f"{WORKSPACES}/{workspace['id']}/machine")
        assert len(orchestrator.started) == 1  # fresh: nothing to re-arm
        async with AsyncSessionLocal() as db:
            await db.execute(
                text(
                    "UPDATE workspace_machine_moves SET updated_at = now() - interval '5 minutes' "
                    "WHERE id = :id"
                ),
                {"id": moved.json()["id"]},
            )
            await db.commit()
        await browser.get(f"{WORKSPACES}/{workspace['id']}/machine")
    assert len(orchestrator.started) == 2
    assert orchestrator.started[1]["id"] == f"workspace.machine_move:{moved.json()['id']}"


# --------------------------------------------------------------------------- #
# run on, at creation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("machine", "status"),
    [
        pytest.param("usable", 201, id="a-machine-the-creator-may-use"),
        pytest.param("not_shared", 404, id="a-machine-not-shared-with-them"),
        pytest.param("foreign", 404, id="another-orgs-machine"),
        pytest.param("unfunded_stopped", 402, id="a-stopped-machine-nothing-can-start"),
    ],
)
async def test_a_new_workspace_may_name_the_machine_it_runs_on(
    real_session: AsyncSession,
    org_admin: OrgWithAdmin,
    platform_admin: OrgWithAdmin,
    machine: str,
    status: int,
) -> None:
    owner, owner_pw = await _person(real_session, org_admin)
    stranger, _ = await _person(real_session, org_admin)
    if machine == "foreign":
        target, _ = await make_org_machine(
            real_session, org_id=platform_admin.org_id, operator_id=platform_admin.admin_id
        )
    elif machine == "unfunded_stopped":
        target, target_alloc = await make_org_machine(
            real_session,
            org_id=org_admin.org_id,
            operator_id=org_admin.admin_id,
            look="stopped",
            offering=await make_offering(real_session, fixed_rate=10**15),
        )
        target_alloc.price_per_minute_nanos = 10**15
        await real_session.commit()
    else:
        target, _ = await make_org_machine(
            real_session,
            org_id=org_admin.org_id,
            operator_id=org_admin.admin_id,
            audience=(("user", owner.id if machine == "usable" else stranger.id),),
        )
    async with _as(owner.email, owner_pw) as browser, _projects_ctx():
        resp = await browser.post(
            WORKSPACES, json={"title": "Pinned", "machine_pin": str(target.id)}
        )
    assert resp.status_code == status, resp.text
    async with AsyncSessionLocal() as db:
        made = (
            (
                await db.execute(
                    select(WorkspaceObject).where(
                        WorkspaceObject.org_team_id == org_admin.org_id,
                        WorkspaceObject.type == "workspace",
                        WorkspaceObject.title == "Pinned",
                    )
                )
            )
            .scalars()
            .all()
        )
    if status == 201:
        [workspace] = made
        assert workspace.spec["machine_pin"] == str(target.id)
        assert workspace.version == 1
    else:
        assert made == []


@asynccontextmanager
async def _projects_ctx() -> AsyncIterator[None]:
    with _projects():
        yield


async def test_an_org_at_its_machine_quota_still_moves_onto_and_runs_on_a_machine_it_holds(
    real_session: AsyncSession, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quota caps what an org may buy; starting a machine it already
    holds is a question of funding alone. An org holding more machines than
    its plan now allows moves a workspace onto a stopped one, and starts a new
    workspace on it."""
    owner, owner_pw = await _person(real_session, org_admin)
    stopped, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id, look="stopped"
    )
    await make_org_machine(real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id)
    for tier in ("free", "plus", "pro", "enterprise"):
        monkeypatch.setattr(settings, f"machine_quota_{tier}", 0)
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser)
        moved = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine", json=_move(stopped.id)
        )
        with _projects():
            created = await browser.post(
                WORKSPACES, json={"title": "At quota", "machine_pin": str(stopped.id)}
            )
    assert moved.status_code == 202, moved.text
    assert created.status_code == 201, created.text
    assert created.json()["id"] != workspace["id"]
    assert (await _workspace_row(created.json()["id"])).spec["machine_pin"] == str(stopped.id)


async def test_the_read_says_how_the_last_move_ended_and_where_it_was_going(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """Once a move's run is over the header still needs it: why it failed
    (no hardware offers "Start on new hardware") and which machine it was for.
    The latest one to end is the one read; a move still running is not it."""
    # The default placement serves the org: a move to it may go.
    await _pool_box(real_session, org_admin)
    owner, owner_pw = await _person(real_session, org_admin)
    first, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    second, _ = await make_org_machine(
        real_session, org_id=org_admin.org_id, operator_id=org_admin.admin_id
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser)
        none_yet = (await browser.get(f"{WORKSPACES}/{workspace['id']}/machine")).json()
        earlier = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine", json=_move(first.id)
        )
        await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine/moves/{earlier.json()['id']}/cancel"
        )
        failing = await browser.post(
            f"{WORKSPACES}/{workspace['id']}/machine", json=_move(second.id)
        )
        async with AsyncSessionLocal() as db:
            row = await db.get(WorkspaceMachineMove, UUID(failing.json()["id"]))
            assert row is not None
            row.state = "failed"
            row.error_code = "target_capacity"
            row.error = "The machine has no hardware available right now."
            row.finished_at = datetime.now(UTC) + timedelta(seconds=1)
            await db.commit()
        running = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(None))
        read = (await browser.get(f"{WORKSPACES}/{workspace['id']}/machine")).json()
    assert none_yet["last_move"] is None
    last = read["last_move"]
    assert last["id"] == failing.json()["id"]
    assert (last["state"], last["error_code"], last["to_org_machine_id"]) == (
        "failed",
        "target_capacity",
        str(second.id),
    )
    assert last["error"] == "The machine has no hardware available right now."
    assert last["finished_at"] is not None
    assert read["active_move"]["id"] == running.json()["id"]


async def test_a_move_to_the_default_is_refused_while_nothing_serves_the_orgs_chats(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The route asks the question the read asks: with nowhere for the org's
    regular chats, a move off the workspace's machine to the default is a 409
    that changes nothing; once something serves them, the same move goes."""
    await make_org_enterprise(org_admin.org_id)
    await set_fallback(real_session, org_id=org_admin.org_id, fallback=False)
    owner, owner_pw = await _person(real_session, org_admin)
    mine, _ = await make_org_machine(
        real_session,
        org_id=org_admin.org_id,
        operator_id=org_admin.admin_id,
        audience=(("user", owner.id),),
        name="A100 Lab",
    )
    async with _as(owner.email, owner_pw) as browser:
        workspace = await _workspace(browser, machine_pin=str(mine.id))
        refused = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(None))
        assert (refused.status_code, _code(refused)) == (409, "move_default_unavailable")
        assert await _moves(workspace["id"]) == []
        row = await _workspace_row(workspace["id"])
        assert (row.spec or {}).get("machine_pin") == str(mine.id)
        await set_fallback(real_session, org_id=org_admin.org_id, fallback=True)
        await _pool_box(real_session, org_admin)
        moved = await browser.post(f"{WORKSPACES}/{workspace['id']}/machine", json=_move(None))
    assert moved.status_code == 202, moved.text
