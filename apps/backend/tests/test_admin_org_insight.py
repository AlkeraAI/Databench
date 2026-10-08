"""The admin console's view into one org: chats, errors and refusals, audit.

Staff (support or admin) read any org; nobody else reads any. Each list is
the named org's alone — a row from another org never leaks in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.models import User, WorkspaceObject
from alkera_core.models.workspace_object import ChatMessage
from backend.services.audit import org_audit as org_audit_service
from backend.services.ops.org_insight import classify
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from tests.conftest import OrgWithAdmin, app_client, login

pytestmark = pytest.mark.asyncio


async def _chat(org: OrgWithAdmin, title: str) -> UUID:
    async with app_client() as tenant:
        await login(tenant, org.admin_email, org.admin_password)
        created = await tenant.post("/api/v1/chats", json={"title": title})
        assert created.status_code == 201, created.text
        return UUID(created.json()["id"])


async def _event(
    db: AsyncSession,
    *,
    org_id: UUID,
    chat_id: UUID,
    seq: int,
    kind: str,
    event: dict[str, Any],
    at: datetime | None = None,
) -> None:
    db.add(
        ChatMessage(
            chat_id=chat_id,
            org_team_id=org_id,
            seq=seq,
            role="assistant",
            kind=kind,
            event_id=str(uuid4()),
            payload={"event_id": str(uuid4()), "kind": kind, "payload": event},
            **({"created_at": at} if at is not None else {}),
        )
    )
    await db.commit()


@pytest.mark.parametrize(
    ("kind", "event", "expected"),
    [
        pytest.param(
            "turn.finished",
            {"stop_reason": "error", "error_detail": "upstream 500"},
            ("error", "upstream 500"),
            id="turn-error",
        ),
        pytest.param(
            "turn.finished",
            {"stop_reason": "refusal"},
            ("refusal", "The model refused"),
            id="turn-refusal",
        ),
        pytest.param("turn.finished", {"stop_reason": "end_turn"}, None, id="turn-ok"),
        pytest.param("turn.finished", {"stop_reason": "cancelled"}, None, id="turn-cancelled"),
        pytest.param(
            "session.status_changed",
            {"status": "error", "detail": "harness crashed"},
            ("error", "harness crashed"),
            id="session-error",
        ),
        pytest.param("session.status_changed", {"status": "idle"}, None, id="session-idle"),
        pytest.param(
            "permission.resolved",
            {"option_id": "reject_once", "decided_by": "policy"},
            ("denied", "Permission rejected by policy"),
            id="permission-rejected",
        ),
        pytest.param(
            "permission.resolved", {"option_id": "allow_once"}, None, id="permission-allowed"
        ),
        pytest.param("message.completed", {"stop_reason": "error"}, None, id="other-kind"),
    ],
)
def test_classify(kind: str, event: dict[str, Any], expected: tuple[str, str] | None) -> None:
    assert classify(kind, event) == expected


async def test_staff_see_the_orgs_chats_and_only_that_org(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    mine = await _chat(org_admin, "Quarterly review")
    elsewhere = await _chat(platform_support, "Not theirs")
    await login(client, platform_support.admin_email, platform_support.admin_password)

    resp = await client.get(f"/admin/v1/orgs/{org_admin.org_id}/chats")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [item["id"] for item in items] == [str(mine)]
    [row] = items
    owner = await real_session.get(User, org_admin.admin_id)
    assert owner is not None
    assert row["title"] == "Quarterly review"
    assert row["owner_email"] == owner.email
    assert row["machine_status"] == "none"
    assert row["machine_name"] is None
    assert str(elsewhere) not in {item["id"] for item in items}


async def test_a_deleted_chat_is_not_listed(
    client: AsyncClient, platform_support: OrgWithAdmin, org_admin: OrgWithAdmin
) -> None:
    gone = await _chat(org_admin, "Gone")
    kept = await _chat(org_admin, "Kept")
    async with app_client() as tenant:
        await login(tenant, org_admin.admin_email, org_admin.admin_password)
        assert (await tenant.delete(f"/api/v1/chats/{gone}")).status_code in (200, 204)
    await login(client, platform_support.admin_email, platform_support.admin_password)
    items = (await client.get(f"/admin/v1/orgs/{org_admin.org_id}/chats")).json()["items"]
    assert [item["id"] for item in items] == [str(kept)]


async def test_errors_lists_errors_refusals_and_denials_newest_first(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    chat = await _chat(org_admin, "Failing")
    other_org_chat = await _chat(platform_support, "Other org")
    now = datetime.now(UTC)
    org = org_admin.org_id
    await _event(
        real_session,
        org_id=org,
        chat_id=chat,
        seq=100,
        kind="turn.finished",
        event={"stop_reason": "end_turn"},
        at=now - timedelta(minutes=5),
    )
    await _event(
        real_session,
        org_id=org,
        chat_id=chat,
        seq=101,
        kind="turn.finished",
        event={"stop_reason": "error", "error_detail": "upstream 500"},
        at=now - timedelta(minutes=4),
    )
    await _event(
        real_session,
        org_id=org,
        chat_id=chat,
        seq=102,
        kind="permission.resolved",
        event={"option_id": "reject_once", "decided_by": "user", "request_id": "r"},
        at=now - timedelta(minutes=3),
    )
    await _event(
        real_session,
        org_id=org,
        chat_id=chat,
        seq=103,
        kind="turn.finished",
        event={"stop_reason": "refusal"},
        at=now - timedelta(minutes=2),
    )
    # Before the window: not reported.
    await _event(
        real_session,
        org_id=org,
        chat_id=chat,
        seq=104,
        kind="turn.finished",
        event={"stop_reason": "error", "error_detail": "old"},
        at=now - timedelta(days=30),
    )
    # Another org's error: never reported here.
    await _event(
        real_session,
        org_id=platform_support.org_id,
        chat_id=other_org_chat,
        seq=100,
        kind="turn.finished",
        event={"stop_reason": "error", "error_detail": "theirs"},
        at=now - timedelta(minutes=1),
    )

    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.get(f"/admin/v1/orgs/{org}/errors")
    assert resp.status_code == 200, resp.text
    got = [(i["kind"], i["detail"], i["chat_title"]) for i in resp.json()["items"]]
    assert got == [
        ("refusal", "The model refused", "Failing"),
        ("denied", "Permission rejected by user", "Failing"),
        ("error", "upstream 500", "Failing"),
    ]

    wide = await client.get(
        f"/admin/v1/orgs/{org}/errors",
        params={"since": (now - timedelta(days=60)).isoformat()},
    )
    assert [i["detail"] for i in wide.json()["items"]][-1] == "old"


async def test_a_chat_its_machine_refuses_is_reported(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    chat_id = await _chat(org_admin, "Refused")
    chat = await real_session.get(WorkspaceObject, chat_id)
    assert chat is not None
    chat.spec = {**chat.spec, "publisher_refusal": "gateway says no credits"}
    flag_modified(chat, "spec")
    await real_session.commit()

    await login(client, platform_support.admin_email, platform_support.admin_password)
    items = (await client.get(f"/admin/v1/orgs/{org_admin.org_id}/errors")).json()["items"]
    assert [(i["kind"], i["detail"]) for i in items] == [
        ("machine_refused", "gateway says no credits")
    ]
    chats = (await client.get(f"/admin/v1/orgs/{org_admin.org_id}/chats")).json()["items"]
    assert chats[0]["machine_refusal"] == "gateway says no credits"


async def test_audit_lists_the_orgs_events_only(
    client: AsyncClient,
    platform_support: OrgWithAdmin,
    org_admin: OrgWithAdmin,
    real_session: AsyncSession,
) -> None:
    actor = await real_session.get(User, org_admin.admin_id)
    other = await real_session.get(User, platform_support.admin_id)
    assert actor is not None and other is not None
    await org_audit_service.record(
        real_session, org_id=org_admin.org_id, actor=actor, action="storage.org_limit_set"
    )
    await org_audit_service.record(
        real_session,
        org_id=platform_support.org_id,
        actor=other,
        action="storage.org_limit_cleared",
    )
    await real_session.commit()

    await login(client, platform_support.admin_email, platform_support.admin_password)
    body = (await client.get(f"/admin/v1/orgs/{org_admin.org_id}/audit")).json()
    actions = [e["action"] for e in body["items"]]
    assert "storage.org_limit_set" in actions
    assert "storage.org_limit_cleared" not in actions
    assert body["total"] == len(body["items"])


@pytest.mark.parametrize("path", ["chats", "errors", "audit"])
async def test_an_org_admin_who_is_not_staff_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, platform_support: OrgWithAdmin, path: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    # Their own org and someone else's alike: the console is staff-only.
    for org in (org_admin.org_id, platform_support.org_id):
        resp = await client.get(f"/admin/v1/orgs/{org}/{path}")
        assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("path", ["chats", "errors", "audit"])
async def test_an_unknown_org_is_404(
    client: AsyncClient, platform_support: OrgWithAdmin, path: str
) -> None:
    await login(client, platform_support.admin_email, platform_support.admin_password)
    resp = await client.get(f"/admin/v1/orgs/{uuid4()}/{path}")
    assert resp.status_code == 404
