"""Erasing an account end to end, on seeded data, across the grace boundary.

One person (``U``) belongs to a shared org (with its owner ``B`` and a
colleague ``C``) and to an org they founded alone. A third org with its own
person (``D``) exists beside them. U schedules the deletion through the route;
the lifecycle sweep runs across the grace boundary with a frozen clock; then
the database is searched for anything personal U left behind.

What must hold afterwards:

* nothing anywhere in the database still carries U's address or surname;
* every erased disposition left no row naming U;
* retained rows still exist, pointing at the tombstone;
* U's shared chat moved to B with its transcript; the private one is gone,
  folder and all; the solo org closed, keeping only its root and billing;
* the shared org's audit chain still verifies and records its reseal;
* B's, C's and D's data is exactly what it was.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alkera_core.account import dispositions, lifecycle
from alkera_core.account.dispositions import TOMBSTONE_LABEL, Kind, tombstone_email
from alkera_core.account.erasure import CLOSED_ORG_NAME
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.email import drain_background_sends
from alkera_core.email.account import send_account_notice
from alkera_core.files.store.filesystem import FilesystemStore
from alkera_core.models import (
    AccountDeletionRequest,
    ChatMessage,
    Invitation,
    InvitationStatus,
    OAuthIdentity,
    OrgAuditEvent,
    OrgMembership,
    Team,
    TeamMembership,
    TeamRole,
    User,
    UserPreference,
    WorkspaceObject,
)
from alkera_core.models.files.tree import FileNode
from alkera_core.notebooks.models import NotebookEdit, NotebookRun
from backend.services.audit import org_audit as org_audit_service
from backend.services.org import own_orgs
from backend.services.org import teams as team_service
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import func, select, text
from tests.chat_shares import files_on, share_chat  # noqa: F401 - fixture
from tests.conftest import OrgWithAdmin, app_client, login, make_member
from tests.files._files_kit import FilesFixtures

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("files_on", "multi_org")]

SURNAME = "Zarathustrova"


async def _new_chat(client: AsyncClient, title: str) -> uuid.UUID:
    response = await client.post(
        "/api/v1/chats", json={"title": title, "clientId": secrets.token_hex(8)}
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _say(chat_id: uuid.UUID, org_id: uuid.UUID, author: uuid.UUID, *lines: str) -> None:
    async with AsyncSessionLocal() as session:
        start = int(
            await session.scalar(
                select(func.coalesce(func.max(ChatMessage.seq), 0)).where(
                    ChatMessage.chat_id == chat_id
                )
            )
            or 0
        )
        for offset, line in enumerate(lines, start=1):
            session.add(
                ChatMessage(
                    chat_id=chat_id,
                    org_team_id=org_id,
                    seq=start + offset,
                    role="user",
                    kind="prompt",
                    event_id=f"evt:{uuid.uuid4().hex}",
                    payload={"text": line, "user_id": str(author)},
                )
            )
        await session.commit()


async def _count(model: Any, *where: Any) -> int:
    async with AsyncSessionLocal() as session:
        return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def _text_columns() -> list[tuple[str, str, str]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT c.table_name, c.column_name, c.data_type FROM information_schema.columns c "
                "JOIN information_schema.tables t ON t.table_name = c.table_name "
                "AND t.table_schema = c.table_schema AND t.table_type = 'BASE TABLE' "
                "WHERE c.table_schema = 'public' AND c.data_type IN "
                "('text', 'character varying', 'character', 'jsonb', 'json', 'bytea', 'ARRAY') "
                "AND c.table_name NOT LIKE 'alembic%'"
            )
        )
        return [(t, c, kind) for t, c, kind in rows.all()]


async def _rows_mentioning(needle: str) -> dict[str, int]:
    """Every (table.column) holding ``needle`` anywhere, case-insensitively.
    Byte columns (file names are bytes) are read with their printable bytes
    as text, so a name is found whichever way it is stored."""
    found: dict[str, int] = {}
    async with AsyncSessionLocal() as session:
        for table, column, kind in await _text_columns():
            expr = f"encode(\"{column}\", 'escape')" if kind == "bytea" else f'"{column}"::text'
            count = await session.scalar(
                text(f'SELECT count(*) FROM "{table}" WHERE {expr} ILIKE :n'),
                {"n": f"%{needle}%"},
            )
            if count:
                found[f"{table}.{column}"] = int(count)
    return found


async def _run_due(user_id: Any, store: Any, *, ledger_store: Any = None) -> str:
    """Run the person's own deletion request through the lifecycle's processing
    (the step the sweep takes for each due request), so a test reads its own
    outcome whatever other tests left in the shared database."""
    async with AsyncSessionLocal() as session:
        request_id = await session.scalar(
            select(AccountDeletionRequest.id)
            .where(AccountDeletionRequest.user_id == user_id)
            .order_by(AccountDeletionRequest.requested_at.desc())
            .limit(1)
        )
    assert request_id is not None
    outcome = await lifecycle.process_deletion(
        request_id, store=store, ledger_store=ledger_store, notify=send_account_notice
    )
    return outcome.status


class World:
    """The seeded people, orgs and things, with the counts taken before."""

    def __init__(self) -> None:
        self.before: dict[str, int] = {}


async def _seed(org_admin: OrgWithAdmin, client: AsyncClient) -> World:
    w = World()
    # Unique per seed: the erasure checks scan every table for it, and another
    # person seeded on the same database must not answer for this one.
    w.surname = f"{SURNAME}{uuid.uuid4().hex[:8]}"
    w.org_a = org_admin.org_id
    w.b_id = org_admin.admin_id
    async with AsyncSessionLocal() as session:
        u, u_password = await make_member(
            session,
            org_id=w.org_a,
            first_name="Quill",
            last_name=w.surname,
            email=f"quill-{uuid.uuid4().hex[:8]}@erasure.dev",
            verified=True,
        )
        c, c_password = await make_member(session, org_id=w.org_a, verified=True)
        w.u_id, w.u_email, w.u_password = u.id, u.email, u_password
        w.c_id, w.c_password = c.id, c_password
        w.c_email = c.email
        solo = await own_orgs.found_org(
            session, user=u, name=f"{w.surname} Labs", now=datetime.now(UTC)
        )
        w.org_s = solo.id
        other_org, d = await team_service.create_org_with_admin(
            session,
            org_name=f"Elsewhere {uuid.uuid4().hex[:6]}",
            admin_email=f"d-{uuid.uuid4().hex[:8]}@elsewhere.dev",
            admin_first_name="Dana",
            admin_last_name="Elsewhere",
            admin_password="d-pass-12345",
        )
        w.org_y, w.d_id = other_org.id, d.id
        session.add(UserPreference(user_id=u.id, preferences={"theme": "dark"}))
        session.add(
            OAuthIdentity(
                user_id=u.id,
                provider="google",
                subject=f"g-{uuid.uuid4().hex}",
                email_at_link=u.email,
                raw_profile={"email": u.email, "family_name": w.surname},
            )
        )
        session.add(
            Invitation(
                team_id=w.org_a,
                email=f"friend-{uuid.uuid4().hex[:6]}@erasure.dev",
                role=TeamRole.MEMBER,
                token=uuid.uuid4().hex,
                status=InvitationStatus.PENDING,
                invited_by_id=u.id,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        # U ran and edited a cell of an org notebook: the org keeps the
        # history, the name it recorded must not survive.
        notebook = uuid.uuid4()
        session.add(
            NotebookRun(
                org_id=w.org_a,
                drive_id=uuid.uuid4(),
                item_id=notebook,
                actor_kind="person",
                requested_by_user_id=w.u_id,
                actor_display=f"Una {w.surname}",
                trigger="run",
                target={"kind": "all"},
                status="ok",
            )
        )
        session.add(
            NotebookEdit(
                org_id=w.org_a,
                item_id=notebook,
                cell_id="a0b1c2d3e4",
                actor_key=f"user:{w.u_id}",
                actor_kind="person",
                user_id=w.u_id,
                actor_display=f"Una {w.surname}",
                first_at=datetime.now(UTC),
                last_at=datetime.now(UTC),
            )
        )
        await session.commit()
        await org_audit_service.record(
            session,
            org_id=w.org_a,
            actor=u,
            action="kb.promoted",
            target=u.email,
            detail={"note": f"by {u.email}"},
        )
        b = await session.get(User, w.b_id)
        await org_audit_service.record(
            session, org_id=w.org_a, actor=b, action="member.role_changed", target=u.email
        )
        await session.commit()

    # Content through the product's own routes.
    await login(client, w.u_email, w.u_password)
    w.shared_chat = await _new_chat(client, "Team roadmap")
    w.private_chat = await _new_chat(client, f"{w.surname} diary")
    await _say(w.shared_chat, w.org_a, w.u_id, "shared line one", "shared line two")
    await _say(w.private_chat, w.org_a, w.u_id, f"my address is {w.u_email}")
    async with AsyncSessionLocal() as session:
        chat = await session.get(WorkspaceObject, w.shared_chat)
        owner = await session.get(User, w.u_id)
        colleague = await session.get(User, w.c_id)
        assert chat and owner and colleague
        await share_chat(session, chat=chat, owner=owner, user=colleague, role="reader")
    async with app_client() as b_client:
        await login(b_client, org_admin.admin_email, org_admin.admin_password)
        w.b_chat = await _new_chat(b_client, "B's planning")
    await _say(w.b_chat, w.org_a, w.b_id, "B speaks")
    await _say(w.b_chat, w.org_a, w.u_id, "U replies in B's chat")

    # A file U made in their solo org, on its drive.
    async with AsyncSessionLocal() as session:
        fx = FilesFixtures(session, w.org_s, w.u_id)
        node = await fx.node(
            f"{w.surname}-cv.txt".encode(), parent=await fx.shared(), created_by=w.u_id
        )
        await fx.version(node, created_by=w.u_id)
        w.solo_file = node.id

    w.before = {
        "b_chat_messages": await _count(ChatMessage, ChatMessage.chat_id == w.b_chat),
        "shared_chat_messages": await _count(ChatMessage, ChatMessage.chat_id == w.shared_chat),
        "org_y_objects": await _count(WorkspaceObject, WorkspaceObject.org_team_id == w.org_y),
        "org_y_members": await _count(OrgMembership, OrgMembership.org_team_id == w.org_y),
        "c_memberships": await _count(OrgMembership, OrgMembership.user_id == w.c_id),
        "invited_by_u": await _count(Invitation, Invitation.invited_by_id == w.u_id),
    }
    return w


async def _schedule(client: AsyncClient, w: World) -> datetime:
    response = await client.post(
        "/api/v1/me/account/deletion",
        json={"confirm_email": w.u_email, "current_password": w.u_password},
    )
    assert response.status_code == 202, response.text
    return datetime.fromisoformat(response.json()["purge_after"])


async def test_erasure_leaves_nothing_personal_and_touches_nobody_else(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    account_mail: list[dict[str, Any]],
    account_archives: FilesystemStore,
) -> None:
    w = await _seed(org_admin, client)
    plan = (await client.get("/api/v1/me/account/deletion/plan")).json()
    fates = {o["org_id"]: o for o in plan["orgs"]}
    assert fates[str(w.org_a)]["fate"] == "leave"
    assert fates[str(w.org_a)]["transfer_to_name"] == "Test Admin"
    assert fates[str(w.org_a)]["shared_items"] == 1  # the shared chat
    assert fates[str(w.org_s)]["fate"] == "close"

    purge_after = await _schedule(client, w)

    # Just inside the grace window: the sweep leaves the account alone.
    with freeze_time(purge_after - timedelta(seconds=1), real_asyncio=True):
        assert await _run_due(w.u_id, account_archives) == "skipped"
    async with AsyncSessionLocal() as session:
        assert (await session.get(User, w.u_id)).deleted_at is None  # type: ignore[union-attr]

    # Just past it: erased.
    with freeze_time(purge_after + timedelta(seconds=1), real_asyncio=True):
        assert await _run_due(w.u_id, account_archives) == "completed"

    # Nothing anywhere still carries the address or the surname.
    assert await _rows_mentioning(w.u_email) == {}
    assert await _rows_mentioning(w.surname) == {}

    # The identity row is the tombstone.
    async with AsyncSessionLocal() as session:
        tomb = await session.get(User, w.u_id)
        assert tomb is not None
        assert tomb.email == tombstone_email(w.u_id)
        assert (tomb.first_name, tomb.last_name, tomb.password_hash) == ("", "", None)
        assert tomb.is_active is False and tomb.deleted_at is not None

    # Every erased column left no row naming U.
    async with AsyncSessionLocal() as session:
        for d in dispositions.registry().values():
            if d.kind is Kind.ERASE and d.step is not None:
                left = await session.scalar(
                    text(f"SELECT count(*) FROM {d.table} WHERE {d.column} = :u"),
                    {"u": w.u_id},
                )
                assert left == 0, d.key
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OrgMembership)
                .where(OrgMembership.user_id == w.u_id)
            )
            == 0
        )

    # The shared chat moved to the org's owner, transcript intact; the private
    # chat, its transcript and its folder are gone.
    async with AsyncSessionLocal() as session:
        shared = await session.get(WorkspaceObject, w.shared_chat)
        assert shared is not None and shared.owner_user_id == w.b_id
        assert shared.title == "Team roadmap"
        assert await session.get(WorkspaceObject, w.private_chat) is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(FileNode)
                .where(FileNode.target_object_id == w.private_chat)
            )
            == 0
        )
    assert (
        await _count(ChatMessage, ChatMessage.chat_id == w.shared_chat)
        == w.before["shared_chat_messages"]
    )
    assert await _count(ChatMessage, ChatMessage.chat_id == w.private_chat) == 0

    # The solo org closed: renamed, emptied, its billing kept.
    async with AsyncSessionLocal() as session:
        solo = await session.get(Team, w.org_s)
        assert solo is not None and solo.name == CLOSED_ORG_NAME
        assert await session.get(FileNode, w.solo_file) is None
    assert await _count(WorkspaceObject, WorkspaceObject.org_team_id == w.org_s) == 0
    # A kept row (the invitation U sent, revoked but on record) still names the
    # tombstone. Billing's retained rows are pinned in test_account_billing.py.
    assert await _count(Invitation, Invitation.invited_by_id == w.u_id) == w.before["invited_by_u"]

    # The shared org's audit chain still verifies and says it was resealed.
    async with AsyncSessionLocal() as session:
        check = await org_audit_service.verify_chain(session, org_id=w.org_a)
        assert check.ok, check
        actions = (
            await session.execute(
                select(OrgAuditEvent.action, OrgAuditEvent.actor_email, OrgAuditEvent.target)
                .where(OrgAuditEvent.org_team_id == w.org_a)
                .order_by(OrgAuditEvent.created_at)
            )
        ).all()
    assert actions[-1][0] == "audit.chain_resealed"
    assert ("kb.promoted", TOMBSTONE_LABEL, TOMBSTONE_LABEL) in actions

    # Nobody else changed.
    assert await _count(ChatMessage, ChatMessage.chat_id == w.b_chat) == w.before["b_chat_messages"]
    assert (
        await _count(WorkspaceObject, WorkspaceObject.org_team_id == w.org_y)
        == w.before["org_y_objects"]
    )
    assert (
        await _count(OrgMembership, OrgMembership.org_team_id == w.org_y)
        == w.before["org_y_members"]
    )
    assert await _count(OrgMembership, OrgMembership.user_id == w.c_id) == w.before["c_memberships"]
    async with AsyncSessionLocal() as session:
        b = await session.get(User, w.b_id)
        assert b is not None and b.is_active and b.deleted_at is None
        assert (
            await session.scalar(
                select(TeamMembership.role).where(
                    TeamMembership.user_id == w.b_id, TeamMembership.team_id == w.org_a
                )
            )
            == TeamRole.ADMIN
        )

    # The request records what happened; the person was told at the old address.
    async with AsyncSessionLocal() as session:
        request = await session.scalar(
            select(AccountDeletionRequest).where(AccountDeletionRequest.user_id == w.u_id)
        )
        assert request is not None and request.status == "completed"
        cert = request.certificate or {}
        assert cert["orgs_left"] == [str(w.org_a)]
        assert cert["orgs_closed"] == [str(w.org_s)]
        assert cert["rows"]["workspace_objects.owner_user_id:transfer"] == 1
    await drain_background_sends()
    assert [m["event"] for m in account_mail if m["to"] == w.u_email][-1] == (
        "email.account_deletion_completed.sent"
    )

    # The old credentials are worthless.
    async with app_client() as fresh:
        r = await fresh.post(
            "/api/v1/auth/login", json={"email": w.u_email, "password": w.u_password}
        )
    assert r.status_code == 401


async def test_a_cancelled_deletion_erases_nothing_after_the_grace_window(
    client: AsyncClient, org_admin: OrgWithAdmin, account_archives: FilesystemStore
) -> None:
    w = await _seed(org_admin, client)
    purge_after = await _schedule(client, w)
    assert (await client.delete("/api/v1/me/account/deletion")).status_code == 200
    with freeze_time(purge_after + timedelta(days=1), real_asyncio=True):
        assert await _run_due(w.u_id, account_archives) == "skipped"
    async with AsyncSessionLocal() as session:
        user = await session.get(User, w.u_id)
        assert user is not None and user.email == w.u_email and user.deleted_at is None
    assert await _count(WorkspaceObject, WorkspaceObject.id == w.private_chat) == 1


async def test_a_blocker_that_appears_during_the_grace_window_holds_the_erasure(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    account_mail: list[dict[str, Any]],
    account_archives: FilesystemStore,
) -> None:
    w = await _seed(org_admin, client)
    # U is an admin of the shared org beside B, so nothing blocks the request.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE team_memberships SET role = 'admin' WHERE user_id = :u AND team_id = :t"),
            {"u": w.u_id, "t": w.org_a},
        )
        await session.commit()
    purge_after = await _schedule(client, w)
    # During the grace window B steps down, leaving U the last admin.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE team_memberships SET role = 'member' WHERE user_id = :u AND team_id = :t"),
            {"u": w.b_id, "t": w.org_a},
        )
        await session.commit()

    for hours in (1, 2):
        with freeze_time(purge_after + timedelta(hours=hours), real_asyncio=True):
            assert await _run_due(w.u_id, account_archives) == "blocked"
    async with AsyncSessionLocal() as session:
        request = await session.scalar(
            select(AccountDeletionRequest).where(AccountDeletionRequest.user_id == w.u_id)
        )
        assert request is not None
        assert (request.status, request.blocked_reason) == ("scheduled", "last_admin")
        assert (await session.get(User, w.u_id)).deleted_at is None  # type: ignore[union-attr]
    await drain_background_sends()
    blocked = [m for m in account_mail if m["event"] == "email.account_deletion_blocked.sent"]
    assert len(blocked) == 1, "told once, not on every pass"

    # C becomes an admin: the next pass goes ahead, and C receives the shared chat.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE team_memberships SET role = 'admin' WHERE user_id = :u AND team_id = :t"),
            {"u": w.c_id, "t": w.org_a},
        )
        await session.commit()
    with freeze_time(purge_after + timedelta(hours=3), real_asyncio=True):
        assert await _run_due(w.u_id, account_archives) == "completed"
    async with AsyncSessionLocal() as session:
        shared = await session.get(WorkspaceObject, w.shared_chat)
        assert shared is not None and shared.owner_user_id == w.c_id
