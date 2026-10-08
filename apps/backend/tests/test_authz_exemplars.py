"""Routes that decide through authorize(): every policy branch driven through
the real route, with the decision rows the outbox holds afterwards.

The chat read and promote (on a throwaway route ahead of the chat surface) and
the org agent-activity ingest. Each case pins the status contract the route
has always had AND the row that now records it — allow rows in the request
transaction, deny rows surviving the refused request's rollback. The
decision readers live in ``apps/backend/tests/_authz_rows.py``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from alkera_core.authz import (
    ActingContext,
    Action,
    Resource,
    ResourceType,
)
from alkera_core.authz.chat_scope import scope_for_team
from alkera_core.authz.policies import chat as chat_policy
from alkera_core.compute.machines import verify_machine_assertion
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    TeamMembership,
    User,
    WorkspaceObject,
)
from alkera_core.observability.asgi import install_exception_handlers
from alkera_core.verification import is_verified
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, DbSession
from backend.authz import enforce, role_resolver
from backend.services.org import teams as team_service
from backend.services.realtime.chat_lookup import lookup_chat_doc
from backend.services.realtime.filters import load_entitlements
from backend.services.sharing.access import admin_reads_private
from backend.services.sharing.node_role import shared_object_role
from fastapi import APIRouter, FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from tests._authz_rows import audit_rows as _audit_rows
from tests._authz_rows import chain as _chain
from tests._authz_rows import decisions as _decisions
from tests._authz_rows import effects as _effects
from tests._authz_rows import other_org_member as _other_org_member
from tests.conftest import OrgWithAdmin, app_client, login, make_member, make_org_enterprise

pytestmark = pytest.mark.asyncio


# =========================================================================== #
# chat.access — an exemplar route, ahead of the chat surface itself
# =========================================================================== #
#
# The chat routes land in another change; the policy that governs them is here
# now, because a route calling enforce() on an unregistered type hard-denies
# itself. So this section drives the policy through a REAL route — the
# dependencies, the status contract, the decision row — on a throwaway app.
# The route resolves its facts the way the real one will: the chat's owner and
# audience through the lookup seam, roles through the resolver, memberships
# through the entitlement snapshot.


class ChatRead(BaseModel):
    chat_id: str
    can_promote: bool


def _chat_router() -> APIRouter:
    router = APIRouter(prefix="/_exemplar/chats")

    async def _facts(
        request: Request, db: AsyncSession, ctx: ActingContext, user: User, chat_id: str
    ) -> tuple[Resource, dict[str, object]]:
        scope = await lookup_chat_doc(db, org_id=user.home_org_team_id, chat_id=chat_id)
        if scope is None:
            # Nothing to decide about: an id nobody declared is not a resource.
            raise HTTPException(status_code=404, detail=chat_policy.NOT_FOUND)
        resolver = role_resolver(request, db, ctx)
        # Roles at the org root: membership of the ORG is what the policy asks
        # about, and the chat's team narrows the audience from there.
        team = await resolver.for_team(user.home_org_team_id)
        ent = await load_entitlements(db, user, org_id=user.home_org_team_id)
        resource = Resource(
            ResourceType.CHAT, id=chat_id, org_id=user.home_org_team_id, team_id=scope.team_id
        )
        # The rung a share on the chat's node gives this caller, resolved
        # through the same seam the real doors use: the policy asks for it on
        # every branch, so a door that skipped it would be denied by the engine
        # rather than decided by the policy.
        shared_role = await shared_object_role(
            db,
            org_id=user.home_org_team_id,
            object_id=UUID(chat_id),
            user_id=ctx.effective_user_id,
            team_ids=ent.team_ids,
        )
        return resource, {
            "in_org": team.in_org,
            "roles": team.roles,
            "is_org_admin": await resolver.is_org_admin(),
            "owner_user_id": str(scope.owner_user_id) if scope.owner_user_id else "",
            "visibility_scope": scope.visibility_scope,
            "team_ids": ent.team_ids,
            "email_verified": is_verified(user),
            "team_id": str(scope.team_id) if scope.team_id else "",
            "shared_role": shared_role or "",
            "bound_machine_id": scope.machine_id or "",
            # A machine assertion counts only once the request proves it is
            # that machine — the same read the product's doors make.
            "asserted_machine_verified": ctx.is_agent
            and await verify_machine_assertion(
                db,
                machine_id=ctx.acting_principal.id,
                org_id=user.home_org_team_id,
                operator_user_id=ctx.effective_user_id,
                credential_id=ctx.credential_id,
            ),
            # Whether this deployment lets an org admin open a chat nobody
            # shared with them — read through the same accessor the product's
            # doors use, never off the settings object here, so the exemplar
            # cannot answer a question the real doors answer differently.
            "admin_reads_private": admin_reads_private(),
            # The exemplar's chats are workspaces of one: the chat's own rung
            # decides who drives it.
            "in_multi_chat_workspace": False,
            "workspace_role": "",
            "owner_departed": False,
        }

    @router.get("/{chat_id}", response_model=ChatRead)
    async def read_chat(
        request: Request, chat_id: str, user: CurrentUser, db: DbSession, ctx: CurrentPrincipal
    ) -> ChatRead:
        resource, attrs = await _facts(request, db, ctx, user, chat_id)
        decision = await enforce(request, db, ctx, Action.READ, resource, attrs)
        return ChatRead(chat_id=chat_id, can_promote=decision.allowed)

    @router.post("/{chat_id}/promote", response_model=ChatRead, status_code=201)
    async def promote_from_chat(
        request: Request, chat_id: str, user: CurrentUser, db: DbSession, ctx: CurrentPrincipal
    ) -> ChatRead:
        resource, attrs = await _facts(request, db, ctx, user, chat_id)
        await enforce(request, db, ctx, Action.PROMOTE, resource, attrs)
        return ChatRead(chat_id=chat_id, can_promote=True)

    return router


@pytest.fixture
def chat_app() -> FastAPI:
    """A throwaway app carrying only the exemplar routes: the real
    dependencies, none of the product's other surface."""
    application = FastAPI()
    application.include_router(_chat_router())
    # The canonical {error: {...}} envelope the product's 4xx bodies carry, so
    # the status contract asserted here is the one a client actually sees.
    install_exception_handlers(application)
    return application


async def _as(chat_app: FastAPI, client: AsyncClient, email: str, password: str) -> AsyncClient:
    """Log ``email`` in on the real app, then point a client carrying that
    session at the exemplar app."""
    await login(client, email, password)
    return AsyncClient(
        transport=ASGITransport(app=chat_app), base_url="http://test", cookies=client.cookies
    )


async def _declared(org: OrgWithAdmin, *, owner_user_id: UUID, team_id: UUID | None = None) -> str:
    """A chat declared in ``org`` — the workspace object POST /api/v1/chats
    writes. Returns the chat id."""
    async with AsyncSessionLocal() as session:
        chat = WorkspaceObject(
            id=uuid4(),
            org_team_id=org.org_id,
            logical_id=f"exemplar-{uuid4().hex[:10]}",
            namespace="workspace",
            type="chat",
            title="",
            version=1,
            status="ready",
            spec={},
            owner_user_id=owner_user_id,
            team_id=team_id,
            visibility_scope=scope_for_team(team_id),
        )
        session.add(chat)
        await session.commit()
        return str(chat.id)


async def test_a_chat_read_by_its_owner_is_allowed_and_the_allow_is_recorded(
    chat_app: FastAPI, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    chat_id = await _declared(org_admin, owner_user_id=org_admin.admin_id)
    caller = await _as(chat_app, client, org_admin.admin_email, org_admin.admin_password)
    resp = await caller.get(f"/_exemplar/chats/{chat_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["chat_id"] == chat_id

    (row,) = [
        r for r in await _decisions(org_admin.org_id, entity="chat") if r.entity_id == chat_id
    ]
    assert row.payload["effect"] == "allow"
    assert row.payload["policy"] == chat_policy.POLICY
    assert row.payload["reason"] == "owner_reads"
    assert row.payload["action"] == "read"
    assert row.visibility == "platform", "a decision row never reaches a tenant stream"
    assert row.payload["path"] == f"/_exemplar/chats/{chat_id}"
    assert row.payload["method"] == "GET"
    assert _chain(row) == [("user", str(org_admin.admin_id))]
    assert set(row.payload["attrs"]) <= chat_policy.AUDITED
    assert row.payload["attrs"]["is_org_admin"] is True
    await caller.aclose()


async def test_a_team_scoped_chat_is_an_opaque_not_found_to_an_outsider(
    chat_app: FastAPI, client: AsyncClient, real_session: Any, org_admin: OrgWithAdmin
) -> None:
    """The status contract: a member of the org who is not in the chat's team
    is told the chat does not exist, and the reason survives their rollback."""
    team = await team_service.create_subteam(
        real_session, org_team_id=org_admin.org_id, name="Data", parent_team_id=org_admin.org_id
    )
    await real_session.commit()
    outsider, pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    chat_id = await _declared(org_admin, owner_user_id=org_admin.admin_id, team_id=team.id)

    caller = await _as(chat_app, client, outsider.email, pw or "")
    resp = await caller.get(f"/_exemplar/chats/{chat_id}")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["message"] == chat_policy.NOT_FOUND

    (row,) = [
        r for r in await _decisions(org_admin.org_id, entity="chat") if r.entity_id == chat_id
    ]
    assert (row.payload["effect"], row.payload["reason"]) == ("deny", "not_in_audience")
    assert row.payload["as_not_found"] is True
    assert row.visibility == "platform"
    await caller.aclose()


async def test_an_unshared_member_may_neither_read_nor_promote_and_learns_nothing(
    chat_app: FastAPI, client: AsyncClient, real_session: Any, org_admin: OrgWithAdmin
) -> None:
    """A chat is private until its node is shared: a member of the org with no
    rung is told the chat does not exist on the read AND on the promote, so
    the status never confirms the id is live; both denials are on record."""
    reader, pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    chat_id = await _declared(org_admin, owner_user_id=org_admin.admin_id)
    caller = await _as(chat_app, client, reader.email, pw or "")

    readable = await caller.get(f"/_exemplar/chats/{chat_id}")
    assert readable.status_code == 404, "an unshared chat is nobody else's to read"
    assert readable.json()["error"]["message"] == chat_policy.NOT_FOUND
    resp = await caller.post(f"/_exemplar/chats/{chat_id}/promote")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["message"] == chat_policy.NOT_FOUND

    rows = [r for r in await _decisions(org_admin.org_id, entity="chat") if r.entity_id == chat_id]
    assert _effects(rows) == [("deny", "not_in_audience"), ("deny", "not_in_audience")]
    assert all(r.payload["as_not_found"] is True for r in rows)
    await caller.aclose()


async def test_an_unverified_owner_is_refused_with_the_structured_code(
    chat_app: FastAPI, client: AsyncClient, real_session: Any, org_admin: OrgWithAdmin
) -> None:
    """A policy that names an error code makes the 403 detail an object, so a
    client can key off the code rather than the prose."""
    owner, pw = await make_member(real_session, org_id=org_admin.org_id, verified=False)
    chat_id = await _declared(org_admin, owner_user_id=owner.id)
    caller = await _as(chat_app, client, owner.email, pw or "")

    resp = await caller.post(f"/_exemplar/chats/{chat_id}/promote")
    assert resp.status_code == 403, resp.text
    body = resp.json()["error"]
    assert (body["code"], body["message"]) == (
        chat_policy.VERIFY_EMAIL_CODE,
        chat_policy.VERIFY_EMAIL_MESSAGE,
    )
    (row,) = [
        r for r in await _decisions(org_admin.org_id, entity="chat") if r.entity_id == chat_id
    ]
    assert (row.payload["effect"], row.payload["reason"]) == (
        "deny",
        "email_verification_required",
    )
    assert row.payload["error_code"] == chat_policy.VERIFY_EMAIL_CODE
    assert row.payload["attrs"]["email_verified"] is False
    await caller.aclose()


async def test_the_owner_promotes_and_the_allow_rides_the_request_transaction(
    chat_app: FastAPI, client: AsyncClient, real_session: Any, org_admin: OrgWithAdmin
) -> None:
    owner, pw = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    chat_id = await _declared(org_admin, owner_user_id=owner.id)
    caller = await _as(chat_app, client, owner.email, pw or "")

    resp = await caller.post(f"/_exemplar/chats/{chat_id}/promote")
    assert resp.status_code == 201, resp.text
    rows = [r for r in await _decisions(org_admin.org_id, entity="chat") if r.entity_id == chat_id]
    assert _effects(rows) == [("allow", "owner_promotes")]
    assert rows[0].payload["action"] == "promote"
    await caller.aclose()


async def test_a_chat_of_another_org_is_a_not_found_before_any_policy_runs(
    chat_app: FastAPI, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The tenancy floor as a client sees it: the outsider's org has no such
    chat, so the route cannot even name a resource — and nothing is recorded
    against the org whose chat was probed."""
    chat_id = await _declared(org_admin, owner_user_id=org_admin.admin_id)
    _org_id, outsider = await _other_org_member(client)
    caller = AsyncClient(
        transport=ASGITransport(app=chat_app), base_url="http://test", cookies=outsider.cookies
    )
    resp = await caller.get(f"/_exemplar/chats/{chat_id}")
    assert resp.status_code == 404, resp.text
    assert [
        r for r in await _decisions(org_admin.org_id, entity="chat") if r.entity_id == chat_id
    ] == []
    await caller.aclose()
    await outsider.aclose()


async def test_an_undeclared_chat_never_reaches_a_decision(
    chat_app: FastAPI, client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    caller = await _as(chat_app, client, org_admin.admin_email, org_admin.admin_password)
    resp = await caller.get("/_exemplar/chats/sess-nobody-declared")
    assert resp.status_code == 404, resp.text
    assert [
        r
        for r in await _decisions(org_admin.org_id, entity="chat")
        if r.entity_id == "sess-nobody-declared"
    ] == []
    await caller.aclose()


# =========================================================================== #
# org agent-activity ingest — the one door a machine knocks on
# =========================================================================== #

_AGENT_INGEST = "/api/v1/org/audit-events/agent"


def _agent_batch() -> dict[str, Any]:
    return {
        "events": [
            {
                "action": "agent.session_started",
                "session_id": "sess-authz",
                "occurred_at": "2026-07-19T10:00:00+00:00",
                "target": "scratch-project",
                "detail": {"project": "scratch"},
            }
        ]
    }


async def _daemon(client: AsyncClient, org_id: UUID) -> tuple[User, AsyncClient]:
    """A verified member of ``org_id`` with their own client, as a box's
    credential reaches the ingest."""
    async with AsyncSessionLocal() as session:
        member, pw = await make_member(session, org_id=org_id, verified=True)
    daemon = app_client()
    await login(daemon, member.email, pw or "")
    return member, daemon


async def _drop_membership(user_id: UUID) -> None:
    """Take away every team membership row the user holds — what removing
    someone from the org leaves behind for a CLI token minted before it."""
    async with AsyncSessionLocal() as session:
        await session.execute(delete(TeamMembership).where(TeamMembership.user_id == user_id))
        await session.commit()


async def test_a_members_daemon_reports_and_the_allow_is_recorded(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await make_org_enterprise(org_admin.org_id)
    member, daemon = await _daemon(client, org_admin.org_id)

    resp = await daemon.post(_AGENT_INGEST, json=_agent_batch())
    assert resp.status_code == 201, resp.text
    assert resp.json() == {"accepted": 1}

    rows = await _decisions(org_admin.org_id, entity="org_audit")
    assert _effects(rows) == [("allow", "org_member_reports")]
    (row,) = rows
    assert row.entity_id == str(org_admin.org_id)
    assert row.visibility == "platform"
    assert row.payload["policy"] == "org_audit.agent_report"
    assert row.payload["action"] == "write"
    assert row.payload["attrs"] == {"is_org_root": True, "org_member": True}
    assert row.payload["path"] == _AGENT_INGEST
    assert _chain(row) == [("user", str(member.id))]
    # The trail the decision authorised is there beside the decision.
    (event,) = await _audit_rows(org_admin.org_id, "agent.session_started")
    assert event.actor_email == member.email


async def test_a_plan_less_orgs_box_still_reports_and_the_allow_is_recorded(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """No Enterprise plan on this org, and the box reports anyway: evidence is
    kept for every organization, and the plan decides who may read it. The
    decision says so — membership is what it weighed, and nothing else."""
    _member, daemon = await _daemon(client, org_admin.org_id)

    resp = await daemon.post(_AGENT_INGEST, json=_agent_batch())
    assert resp.status_code == 201, resp.text

    rows = await _decisions(org_admin.org_id, entity="org_audit")
    assert _effects(rows) == [("allow", "org_member_reports")]
    assert rows[0].payload["attrs"] == {"is_org_root": True, "org_member": True}
    assert len(await _audit_rows(org_admin.org_id, "agent.session_started")) == 1


async def test_a_token_whose_membership_is_gone_reports_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A CLI token lives ninety days; a membership does not. Before the policy
    nothing on this route looked, so an entitled org kept taking entries from
    someone it had removed."""
    await make_org_enterprise(org_admin.org_id)
    member, daemon = await _daemon(client, org_admin.org_id)
    await _drop_membership(member.id)

    resp = await daemon.post(_AGENT_INGEST, json=_agent_batch())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "org_member_required"

    rows = await _decisions(org_admin.org_id, entity="org_audit")
    assert _effects(rows) == [("deny", "not_in_org")]
    assert rows[0].payload["attrs"]["org_member"] is False
    assert await _audit_rows(org_admin.org_id, "agent.session_started") == []


async def test_a_forged_action_is_refused_after_the_allow_and_leaves_no_allow_row(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The vocabulary guard is content, not authority: it answers 422 after the
    decision, and the allow row rolls back with the batch it would have claimed
    — an allow on record never describes a write that did not land."""
    await make_org_enterprise(org_admin.org_id)
    _member, daemon = await _daemon(client, org_admin.org_id)
    batch = _agent_batch()
    batch["events"][0]["action"] = "authz.decision"

    resp = await daemon.post(_AGENT_INGEST, json=batch)
    assert resp.status_code == 422
    assert await _decisions(org_admin.org_id, entity="org_audit") == []
    assert await _audit_rows(org_admin.org_id, "authz.decision") == []
