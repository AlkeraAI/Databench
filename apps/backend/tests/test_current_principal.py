"""Every credential shape resolves to one acting context — through the real
dependency on a test-only router mounted for the duration of each test.

Cookie session, Bearer user JWT, either with the agent assertion headers, CI
token, proxy token, personal access token, machine credential. The agent
headers are valid on a user JWT only: on any token they are a 400, and a
malformed pair is a 400 on a JWT too — except on a machine credential, where
they may name the box's own machine or a chat bound to it and nothing else.
Statement counting proves a route that declares both ``CurrentUser`` and
``CurrentPrincipal`` reads ``users`` once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from _statement_log import counting
from alkera_core.auth.last_used import stamp_last_used
from alkera_core.authz import agent_headers
from alkera_core.authz.headers import ACTOR_AGENT, ACTOR_HEADER, AGENT_ID_HEADER
from alkera_core.compute.machines import WORKSPACE
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    CiToken,
    MachineCredential,
    OrgComputeAssignment,
    PersonalAccessToken,
    ProxyToken,
    WorkspaceObject,
)
from alkera_core.models.compute import COMPUTE_ACTIVE_STATES, ComputeAllocation
from backend.auth.dependencies import CurrentPrincipal, CurrentUser, PrincipalUser
from backend.services.credentials import ci_tokens as ci_token_service
from backend.services.credentials import machine_credentials as machine_credential_service
from backend.services.credentials import pats as pat_service
from backend.services.credentials import proxy_tokens as proxy_token_service
from fastapi import APIRouter, HTTPException, Request
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import select
from tests._compute_helpers import make_machine_type
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, login, make_member, mint_cli_token

pytestmark = pytest.mark.asyncio

_PREFIX = "/api/v1/_test/principal"
_router = APIRouter(prefix=_PREFIX)


def _describe(request: Request, ctx: Any) -> dict[str, Any]:
    return {
        "actor": ctx.audit_dict(),
        "org_id": str(ctx.org_id),
        "subject_id": ctx.subject.id,
        "effective_user_id": str(ctx.effective_user_id) if ctx.effective_user_id else None,
        "state_user_id": getattr(request.state, "user_id", None),
    }


@_router.get("/only")
async def _principal_only(request: Request, ctx: CurrentPrincipal) -> dict[str, Any]:
    return _describe(request, ctx)


@_router.get("/both")
async def _user_then_principal(
    request: Request, user: CurrentUser, ctx: CurrentPrincipal
) -> dict[str, Any]:
    body = _describe(request, ctx)
    body["user_id"] = str(user.id)
    return body


@_router.get("/user-only")
async def _user_only(user: CurrentUser) -> dict[str, Any]:
    return {"user_id": str(user.id)}


@_router.get("/then-fail")
async def _principal_then_fail(ctx: CurrentPrincipal) -> dict[str, Any]:
    raise HTTPException(status_code=409, detail="the route refused after authenticating")


@_router.get("/machine-or-user")
async def _machine_or_user(
    request: Request, ctx: CurrentPrincipal, user: PrincipalUser
) -> dict[str, Any]:
    body = _describe(request, ctx)
    body["user_id"] = str(user.id) if user is not None else None
    body["served_org_ids"] = sorted(str(org) for org in ctx.served_org_ids)
    body["serves_every_org"] = ctx.serves_every_org
    return body


@pytest.fixture(autouse=True)
def _mounted_router() -> Iterator[None]:
    before = len(fastapi_app.router.routes)
    fastapi_app.include_router(_router)
    try:
        yield
    finally:
        del fastapi_app.router.routes[before:]


def _users_reads(statements: list[str]) -> int:
    return sum(1 for s in statements if "FROM users" in s)


# --------------------------------------------------------------------------- #
# credential factories
# --------------------------------------------------------------------------- #


async def _ci_token(org: OrgWithAdmin, **overrides: Any) -> tuple[UUID, str]:
    async with AsyncSessionLocal() as session:
        row, raw = await ci_token_service.mint(
            session, org_id=org.org_id, created_by_id=org.admin_id, label="ci-label", **overrides
        )
        await session.commit()
        return row.id, raw


async def _proxy_token(org: OrgWithAdmin, **overrides: Any) -> tuple[UUID, str]:
    async with AsyncSessionLocal() as session:
        row, raw = await proxy_token_service.mint(
            session, org_id=org.org_id, created_by_id=org.admin_id, label="proxy-label", **overrides
        )
        await session.commit()
        return row.id, raw


async def _pat(
    org: OrgWithAdmin, *, user_id: UUID | None = None, **overrides: Any
) -> tuple[UUID, str]:
    async with AsyncSessionLocal() as session:
        row, raw = await pat_service.mint(
            session,
            org_id=org.org_id,
            user_id=user_id or org.admin_id,
            label="pat-label",
            **overrides,
        )
        await session.commit()
        return row.id, raw


class _Box:
    """A platform box's credential and the machine it holds."""

    def __init__(self, raw: str, credential_id: UUID, machine_id: UUID | None) -> None:
        self.raw = raw
        self.credential_id = credential_id
        self.machine_id = machine_id


async def _machine_credential(
    org: OrgWithAdmin,
    *,
    tenancy: str = "dedicated",
    served_org: UUID | None = None,
    claimed: bool = True,
    state: str = COMPUTE_ACTIVE_STATES[-1],
) -> _Box:
    """A credential minted in ``org`` holding a live workspace machine there —
    the shape ``/machines/claim`` leaves — dedicated to ``served_org`` (the
    minting org when none is named) or serving the whole pool."""
    async with AsyncSessionLocal() as session:
        machine_type = await make_machine_type(session)
        credential, raw = await machine_credential_service.mint(
            session,
            org_id=org.org_id,
            created_by=org.admin_id,
            machine_type=machine_type,
            tenancy=tenancy,
            label="box",
        )
        machine_id: UUID | None = None
        if claimed:
            machine = ComputeAllocation(
                user_id=org.admin_id,
                org_team_id=org.org_id,
                machine_type_id=machine_type.id,
                lifecycle=WORKSPACE,
                state=state,
                tenancy=tenancy,
            )
            session.add(machine)
            await session.flush()
            credential.machine_id = machine.id
            machine_id = machine.id
            if tenancy == "dedicated":
                session.add(
                    OrgComputeAssignment(
                        org_team_id=served_org or org.org_id, machine_id=machine.id
                    )
                )
        await session.commit()
        return _Box(raw, credential.id, machine_id)


async def _chat_bound_to(org: OrgWithAdmin, machine_id: UUID | None) -> str:
    async with AsyncSessionLocal() as session:
        chat = WorkspaceObject(
            org_team_id=org.org_id,
            logical_id=f"principal-{uuid4().hex[:10]}",
            namespace="workspace",
            type="chat",
            title="",
            version=1,
            status="ready",
            spec={"machine_id": str(machine_id)} if machine_id else {},
            owner_user_id=org.admin_id,
            visibility_scope="private",
        )
        session.add(chat)
        await session.commit()
        return str(chat.id)


async def _bearer_jwt(org: OrgWithAdmin) -> str:
    return await mint_cli_token(user_id=org.admin_id, email=org.admin_email, org_team_id=org.org_id)


def _bearer(raw: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}", **extra}


async def _last_used(model: type[Any], token_id: UUID) -> datetime | None:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(model).where(model.id == token_id))).scalar_one()
        return row.last_used_at


def _error(resp: Any) -> dict[str, Any]:
    body = resp.json()
    assert "error" in body, body
    return body["error"]


def _link(kind: str, id: str, org_id: UUID, label: str, credential: str | None) -> dict[str, Any]:
    """One persisted chain link, exactly as the versioned record dumps it."""
    return {
        "schema_version": "1.0.0",
        "metadata": {},
        "kind": kind,
        "id": id,
        "org_id": str(org_id),
        "label": label,
        "credential": credential,
    }


# --------------------------------------------------------------------------- #
# a user's own session
# --------------------------------------------------------------------------- #


async def test_a_cookie_session_is_the_user_acting_directly(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(f"{_PREFIX}/only")
    assert resp.status_code == 200
    body = resp.json()
    actor = body["actor"]
    assert actor["acting"] == _link(
        "user", str(org_admin.admin_id), org_admin.org_id, org_admin.admin_email, "jwt"
    )
    assert actor["delegating_user"] is None
    assert len(actor["chain"]) == 1
    assert actor["agent_id_asserted_by"] is None
    assert body["effective_user_id"] == str(org_admin.admin_id)
    assert body["state_user_id"] == str(org_admin.admin_id)
    assert body["org_id"] == str(org_admin.org_id)


async def test_a_bearer_jwt_is_the_user_acting_directly(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    jwt = await _bearer_jwt(org_admin)
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(jwt))
    assert resp.status_code == 200
    actor = resp.json()["actor"]
    assert (actor["acting"]["kind"], actor["acting"]["credential"]) == ("user", "jwt")
    assert actor["acting"]["id"] == str(org_admin.admin_id)
    assert len(actor["chain"]) == 1


@pytest.mark.parametrize(
    "via", [pytest.param("cookie", id="cookie"), pytest.param("bearer", id="bearer")]
)
async def test_agent_headers_on_a_user_jwt_make_the_caller_an_agent_for_that_user(
    client: AsyncClient, org_admin: OrgWithAdmin, via: str
) -> None:
    headers = agent_headers("sess-42")
    if via == "cookie":
        await login(client, org_admin.admin_email, org_admin.admin_password)
    else:
        headers = _bearer(await _bearer_jwt(org_admin), **headers)
    resp = await client.get(f"{_PREFIX}/only", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    actor = body["actor"]
    assert actor["acting"] == _link("agent", "sess-42", org_admin.org_id, "sess-42", "agent_header")
    assert actor["delegating_user"]["kind"] == "user"
    assert actor["delegating_user"]["id"] == str(org_admin.admin_id)
    assert actor["delegating_user"]["credential"] == "jwt"
    assert [(link["kind"], link["id"]) for link in actor["chain"]] == [
        ("user", str(org_admin.admin_id)),
        ("agent", "sess-42"),
    ]
    assert actor["agent_id_asserted_by"] == "client"
    # The human behind the agent is still the request's user.
    assert body["effective_user_id"] == str(org_admin.admin_id)
    assert body["subject_id"] == str(org_admin.admin_id)
    assert body["state_user_id"] == str(org_admin.admin_id)


def _malformed_cases() -> list[Any]:
    valid = agent_headers("sess-1")
    return [
        pytest.param({ACTOR_HEADER: ACTOR_AGENT}, id="actor-without-id"),
        pytest.param({AGENT_ID_HEADER: "sess-1"}, id="id-without-actor"),
        pytest.param({**valid, ACTOR_HEADER: "user"}, id="actor-not-agent"),
        pytest.param({**valid, ACTOR_HEADER: "Agent"}, id="actor-wrong-case"),
        pytest.param({**valid, AGENT_ID_HEADER: "has space"}, id="id-with-space"),
        pytest.param({**valid, AGENT_ID_HEADER: "x" * 129}, id="id-too-long"),
        pytest.param({**valid, AGENT_ID_HEADER: "-leading"}, id="id-leading-dash"),
        pytest.param({**valid, AGENT_ID_HEADER: ""}, id="id-empty"),
    ]


@pytest.mark.parametrize("headers", _malformed_cases())
async def test_a_malformed_agent_assertion_on_a_user_jwt_is_a_400(
    client: AsyncClient, org_admin: OrgWithAdmin, headers: dict[str, str]
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(f"{_PREFIX}/only", headers=headers)
    assert resp.status_code == 400
    assert _error(resp)["code"] == "invalid_agent_headers"


async def test_a_malformed_agent_assertion_is_refused_on_every_jwt_route(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The refusal lives in current_user, so an ordinary route sees it too: a
    half-present assertion never passes as a plain session anywhere."""
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get("/api/v1/auth/me", headers={AGENT_ID_HEADER: "sess-1"})
    assert resp.status_code == 400
    assert _error(resp)["code"] == "invalid_agent_headers"


async def test_no_credential_is_a_401(client: AsyncClient) -> None:
    resp = await client.get(f"{_PREFIX}/only")
    assert resp.status_code == 401


async def test_a_cookie_wins_over_a_bearer_token(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The same precedence current_user has: SPA traffic is the common path."""
    _pat_id, raw = await _pat(org_admin)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw))
    assert resp.status_code == 200
    assert resp.json()["actor"]["acting"]["kind"] == "user"


# --------------------------------------------------------------------------- #
# CI token
# --------------------------------------------------------------------------- #


async def test_a_ci_token_is_a_service_for_its_org(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token_id, raw = await _ci_token(org_admin)
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw))
    assert resp.status_code == 200
    body = resp.json()
    actor = body["actor"]
    assert actor["acting"] == _link(
        "service", str(token_id), org_admin.org_id, "ci-label", "ci_token"
    )
    assert actor["delegating_user"] is None
    assert len(actor["chain"]) == 1
    assert body["effective_user_id"] is None
    assert body["state_user_id"] is None
    assert await _last_used(CiToken, token_id) is not None


async def test_a_ci_token_with_agent_headers_is_refused_but_still_stamped(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token_id, raw = await _ci_token(org_admin)
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw, **agent_headers("sess-1")))
    assert resp.status_code == 400
    assert _error(resp)["code"] == "agent_actor_requires_user_session"
    assert await _last_used(CiToken, token_id) is not None


async def test_a_ci_token_with_agent_headers_is_refused_on_the_real_gate_route(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The gate routes depend on require_ci_token directly; the forgery guard
    sits inside it, so they refuse too."""
    _token_id, raw = await _ci_token(org_admin)
    resp = await client.post(
        "/api/v1/gate/snapshots/lookup",
        json={"repo": "acme/warehouse", "candidates": ["a" * 40]},
        headers=_bearer(raw, **agent_headers("sess-1")),
    )
    assert resp.status_code == 400
    assert _error(resp)["code"] == "agent_actor_requires_user_session"


@pytest.mark.parametrize("headers", _malformed_cases())
async def test_a_ci_token_with_even_a_malformed_agent_header_is_refused(
    client: AsyncClient, org_admin: OrgWithAdmin, headers: dict[str, str]
) -> None:
    """Any agent header on a token is a forgery attempt, well-formed or not."""
    _token_id, raw = await _ci_token(org_admin)
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw, **headers))
    assert resp.status_code == 400
    assert _error(resp)["code"] == "agent_actor_requires_user_session"


async def test_a_revoked_ci_token_is_a_401(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    token_id, raw = await _ci_token(org_admin)
    async with AsyncSessionLocal() as session:
        await ci_token_service.revoke(session, org_id=org_admin.org_id, token_id=token_id)
        await session.commit()
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw))
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# proxy token
# --------------------------------------------------------------------------- #


async def test_a_proxy_token_is_a_service_for_its_org(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token_id, raw = await _proxy_token(org_admin)
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw))
    assert resp.status_code == 200
    body = resp.json()
    actor = body["actor"]
    assert actor["acting"] == _link(
        "service", str(token_id), org_admin.org_id, "proxy-label", "proxy_token"
    )
    assert actor["delegating_user"] is None
    assert body["effective_user_id"] is None
    assert body["state_user_id"] is None
    assert await _last_used(ProxyToken, token_id) is not None


async def test_a_proxy_token_with_agent_headers_is_refused_but_still_stamped(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token_id, raw = await _proxy_token(org_admin)
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw, **agent_headers("sess-1")))
    assert resp.status_code == 400
    assert _error(resp)["code"] == "agent_actor_requires_user_session"
    assert await _last_used(ProxyToken, token_id) is not None


async def test_a_revoked_proxy_token_is_a_401(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    token_id, raw = await _proxy_token(org_admin)
    async with AsyncSessionLocal() as session:
        await proxy_token_service.revoke(session, org_id=org_admin.org_id, token_id=token_id)
        await session.commit()
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw))
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


async def test_an_expired_proxy_token_is_a_401(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    _token_id, raw = await _proxy_token(
        org_admin, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw))
    assert resp.status_code == 401


async def test_an_unknown_proxy_shaped_secret_is_a_401(client: AsyncClient) -> None:
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer("alk_proxy_" + "x" * 64))
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# personal access token
# --------------------------------------------------------------------------- #


async def test_a_personal_access_token_acts_for_its_owner(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token_id, raw = await _pat(org_admin)
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw))
    assert resp.status_code == 200
    body = resp.json()
    actor = body["actor"]
    assert actor["acting"] == _link("pat", str(token_id), org_admin.org_id, "pat-label", "pat")
    # The owner presented nothing themselves, so their link carries no credential.
    assert actor["delegating_user"] == _link(
        "user", str(org_admin.admin_id), org_admin.org_id, org_admin.admin_email, None
    )
    assert [(link["kind"], link["id"]) for link in actor["chain"]] == [
        ("user", str(org_admin.admin_id)),
        ("pat", str(token_id)),
    ]
    assert actor["agent_id_asserted_by"] is None
    assert body["effective_user_id"] == str(org_admin.admin_id)
    assert body["subject_id"] == str(org_admin.admin_id)
    # No JWT was decoded, so the JWT-derived request state is not set.
    assert body["state_user_id"] is None
    assert await _last_used(PersonalAccessToken, token_id) is not None


async def test_a_personal_access_token_with_agent_headers_is_refused_but_still_stamped(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token_id, raw = await _pat(org_admin)
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw, **agent_headers("sess-1")))
    assert resp.status_code == 400
    assert _error(resp)["code"] == "agent_actor_requires_user_session"
    assert await _last_used(PersonalAccessToken, token_id) is not None


async def test_the_last_used_stamp_survives_a_request_that_later_fails(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The stamp is written in its own committed session: a route that 4xxes
    after authenticating rolls the request back, and the usage trace stays."""
    token_id, raw = await _pat(org_admin)
    resp = await client.get(f"{_PREFIX}/then-fail", headers=_bearer(raw))
    assert resp.status_code == 409
    assert await _last_used(PersonalAccessToken, token_id) is not None


async def test_a_revoked_personal_access_token_is_a_401(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token_id, raw = await _pat(org_admin)
    async with AsyncSessionLocal() as session:
        await pat_service.revoke(session, org_id=org_admin.org_id, token_id=token_id)
        await session.commit()
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw))
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"
    assert await _last_used(PersonalAccessToken, token_id) is None


async def test_an_expired_personal_access_token_is_a_401(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Minted with one second of life, presented after it has gone."""
    import asyncio

    token_id, raw = await _pat(org_admin, expires_at=datetime.now(UTC) + timedelta(seconds=1))
    await asyncio.sleep(1.1)
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw))
    assert resp.status_code == 401
    assert await _last_used(PersonalAccessToken, token_id) is None


async def test_a_personal_access_token_of_a_deactivated_owner_is_a_401(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    async with AsyncSessionLocal() as session:
        member, _pw = await make_member(session, org_id=org_admin.org_id, verified=True)
        member_id = member.id
    _token_id, raw = await _pat(org_admin, user_id=member_id)
    async with AsyncSessionLocal() as session:
        from alkera_core.models import User

        user = (await session.execute(select(User).where(User.id == member_id))).scalar_one()
        user.is_active = False
        await session.commit()
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw))
    assert resp.status_code == 401


async def test_a_personal_access_token_whose_org_no_longer_matches_its_owner_is_a_401(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A token must not carry a tenancy its owner no longer has; a credential
    that fails to resolve is refused like every other token shape."""
    token_id, raw = await _pat(org_admin)
    async with AsyncSessionLocal() as session:
        from backend.services.org import teams as team_service

        other, _ = await team_service.create_org_with_admin(
            session,
            org_name="Elsewhere",
            admin_email=f"elsewhere-{token_id.hex[:8]}@alkera.dev",
            admin_first_name="Else",
            admin_last_name="Where",
            admin_password="pw-1234567890",
        )
        row = (
            await session.execute(
                select(PersonalAccessToken).where(PersonalAccessToken.id == token_id)
            )
        ).scalar_one()
        row.org_team_id = other.id
        await session.commit()
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer(raw))
    assert resp.status_code == 401


async def test_an_unknown_pat_shaped_secret_is_a_401(client: AsyncClient) -> None:
    resp = await client.get(f"{_PREFIX}/only", headers=_bearer("alk_pat_" + "x" * 64))
    assert resp.status_code == 401


async def test_a_personal_access_token_is_not_a_session_for_jwt_only_routes(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Deliberately: no existing route accepts a PAT until a route opts into
    CurrentPrincipal — current_user still wants a JWT."""
    _token_id, raw = await _pat(org_admin)
    resp = await client.get(f"{_PREFIX}/user-only", headers=_bearer(raw))
    assert resp.status_code == 401
    resp = await client.get("/api/v1/auth/me", headers=_bearer(raw))
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# composition: one users read
# --------------------------------------------------------------------------- #


async def test_a_route_with_both_dependencies_reads_users_once(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    with counting() as statements:
        resp = await client.get(f"{_PREFIX}/user-only")
    assert resp.status_code == 200
    baseline = _users_reads(statements)
    assert baseline >= 1

    with counting() as statements:
        resp = await client.get(f"{_PREFIX}/both")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(org_admin.admin_id)
    assert body["actor"]["acting"]["id"] == str(org_admin.admin_id)
    assert _users_reads(statements) == baseline


async def test_both_dependencies_agree_on_the_agent_shape(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.get(f"{_PREFIX}/both", headers=agent_headers("sess-7"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(org_admin.admin_id)
    assert body["actor"]["acting"]["kind"] == "agent"
    assert body["actor"]["delegating_user"]["id"] == body["user_id"]


async def test_a_gateway_token_is_refused_on_every_api_route(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The chat's gateway token is the agent's inference credential and nothing
    else: the API refuses it as a session on ``/auth/me`` (a ``CurrentUser``
    route) and on a ``CurrentPrincipal`` route alike, with or without the agent
    assertion — the same session it was minted from is accepted on both."""
    from alkera_core.auth import decode_session_token, mint_gateway_token

    session_token = await mint_cli_token(
        user_id=org_admin.admin_id, email=org_admin.admin_email, org_team_id=org_admin.org_id
    )
    session = decode_session_token(session_token)
    gateway_token, _ = mint_gateway_token(
        user_id=org_admin.admin_id,
        org_team_id=org_admin.org_id,
        chat_id=UUID(int=7),
        session=session,
    )
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {session_token}"})
    assert me.status_code == 200, "the session it was minted from is a session"

    for headers in (
        {"Authorization": f"Bearer {gateway_token}"},
        {"Authorization": f"Bearer {gateway_token}", **agent_headers(UUID(int=7).hex)},
    ):
        assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
        assert (await client.get(f"{_PREFIX}/only", headers=headers)).status_code == 401
        assert (await client.get(f"{_PREFIX}/both", headers=headers)).status_code == 401
    # Nor is it a cookie session.
    client.cookies.set("alkera_session", gateway_token)
    try:
        assert (await client.get("/api/v1/auth/me")).status_code == 401
    finally:
        client.cookies.delete("alkera_session")


# --------------------------------------------------------------------------- #
# a machine credential as the bearer
# --------------------------------------------------------------------------- #


async def test_a_machine_credential_is_the_machine_acting_for_nobody(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box = await _machine_credential(org_admin)
    resp = await client.get(f"{_PREFIX}/machine-or-user", headers=_bearer(box.raw))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    machine = _link("machine", str(box.machine_id), org_admin.org_id, "box", "machine")
    assert body["actor"]["acting"] == machine
    assert body["actor"]["delegating_user"] is None
    assert body["actor"]["chain"] == [machine]
    assert body["actor"]["agent_id_asserted_by"] is None
    assert body["org_id"] == str(org_admin.org_id)
    assert body["subject_id"] == str(box.machine_id)
    assert body["effective_user_id"] is None
    assert body["user_id"] is None
    assert body["state_user_id"] is None
    assert body["served_org_ids"] == [str(org_admin.org_id)]
    assert body["serves_every_org"] is False
    assert await _last_used(MachineCredential, box.credential_id) is not None


async def test_a_dedicated_box_serves_the_org_it_is_assigned_to(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    served = uuid4()
    async with AsyncSessionLocal() as session:
        from backend.services.org import teams as team_service
        from tests.conftest import _unique_email, _unique_org_name

        other, _ = await team_service.create_org_with_admin(
            session,
            org_name=_unique_org_name(),
            admin_email=_unique_email("served"),
            admin_first_name="S",
            admin_last_name="O",
            admin_password="pw-1234567890",
        )
        await session.commit()
        served = other.id
    box = await _machine_credential(org_admin, served_org=served)
    resp = await client.get(f"{_PREFIX}/machine-or-user", headers=_bearer(box.raw))
    assert resp.status_code == 200, resp.text
    assert resp.json()["served_org_ids"] == [str(served)]
    assert resp.json()["org_id"] == str(org_admin.org_id)


async def test_a_pool_box_serves_every_org(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    box = await _machine_credential(org_admin, tenancy="pool")
    resp = await client.get(f"{_PREFIX}/machine-or-user", headers=_bearer(box.raw))
    assert resp.status_code == 200, resp.text
    assert (resp.json()["served_org_ids"], resp.json()["serves_every_org"]) == ([], True)


async def test_an_unclaimed_credential_is_the_credential_itself_and_serves_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box = await _machine_credential(org_admin, tenancy="pool", claimed=False)
    resp = await client.get(f"{_PREFIX}/machine-or-user", headers=_bearer(box.raw))
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject_id"] == str(box.credential_id)
    assert (resp.json()["served_org_ids"], resp.json()["serves_every_org"]) == ([], False)


async def _refused(resp: Any) -> None:
    assert resp.status_code == 401, resp.text
    assert _error(resp)["code"] == "machine_credential_refused"
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_a_revoked_machine_credential_is_a_401(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box = await _machine_credential(org_admin)
    async with AsyncSessionLocal() as session:
        await machine_credential_service.revoke(session, box.credential_id)
        await session.commit()
    await _refused(await client.get(f"{_PREFIX}/only", headers=_bearer(box.raw)))
    assert await _last_used(MachineCredential, box.credential_id) is None


@pytest.mark.parametrize("state", ["released", "failed", "lost"])
async def test_a_credential_whose_machine_is_gone_is_a_401(
    client: AsyncClient, org_admin: OrgWithAdmin, state: str
) -> None:
    box = await _machine_credential(org_admin, state=state)
    await _refused(await client.get(f"{_PREFIX}/only", headers=_bearer(box.raw)))


async def test_an_unknown_machine_shaped_secret_is_the_same_401(client: AsyncClient) -> None:
    await _refused(await client.get(f"{_PREFIX}/only", headers=_bearer("alk_machine_" + "x" * 64)))


async def test_a_machine_credential_is_not_a_session_for_jwt_only_routes(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A box is refused wherever a person is required: the route family that
    binds ``CurrentUser`` — ``/auth/me`` included — never sees it as anyone.
    The chat listing is the door that takes either: to the box it is its own
    listing, the chats bound to it, and this box holds none."""
    box = await _machine_credential(org_admin)
    for path in (f"{_PREFIX}/user-only", "/api/v1/auth/me", "/api/v1/machines/current"):
        resp = await client.get(path, headers=_bearer(box.raw))
        assert resp.status_code == 401, (path, resp.text)
    resp = await client.get("/api/v1/chats", headers=_bearer(box.raw))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "next_cursor": None}


async def test_agent_headers_naming_the_boxs_own_machine_change_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box = await _machine_credential(org_admin)
    resp = await client.get(
        f"{_PREFIX}/machine-or-user",
        headers=_bearer(box.raw, **agent_headers(str(box.machine_id))),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["actor"]["acting"]["kind"] == "machine"
    assert resp.json()["actor"]["delegating_user"] is None


async def test_agent_headers_naming_a_chat_bound_to_the_box_are_accepted(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box = await _machine_credential(org_admin)
    chat_id = await _chat_bound_to(org_admin, box.machine_id)
    resp = await client.get(
        f"{_PREFIX}/machine-or-user", headers=_bearer(box.raw, **agent_headers(chat_id))
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["subject_id"] == str(box.machine_id)
    assert resp.json()["effective_user_id"] is None


@pytest.mark.parametrize(
    "named",
    ["unbound", "other-box", "not-a-chat", "booting"],
)
async def test_agent_headers_naming_a_chat_the_box_does_not_hold_are_a_400(
    client: AsyncClient, org_admin: OrgWithAdmin, named: str
) -> None:
    box = await _machine_credential(org_admin)
    other = await _machine_credential(org_admin, tenancy="pool")
    chat_id = {
        "unbound": lambda: _chat_bound_to(org_admin, None),
        "other-box": lambda: _chat_bound_to(org_admin, other.machine_id),
    }.get(named)
    session_id = await chat_id() if chat_id else ("booting" if named == "booting" else str(uuid4()))
    resp = await client.get(
        f"{_PREFIX}/machine-or-user", headers=_bearer(box.raw, **agent_headers(session_id))
    )
    assert resp.status_code == 400, resp.text
    assert _error(resp)["code"] == "agent_actor_requires_bound_chat"
    # Still stamped: the credential authenticated before the assertion was judged.
    assert await _last_used(MachineCredential, box.credential_id) is not None


async def test_a_malformed_agent_assertion_on_a_machine_credential_is_a_400(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box = await _machine_credential(org_admin)
    resp = await client.get(
        f"{_PREFIX}/machine-or-user", headers=_bearer(box.raw, **{AGENT_ID_HEADER: "sess"})
    )
    assert resp.status_code == 400, resp.text
    assert _error(resp)["code"] == "invalid_agent_headers"


async def test_a_machine_or_user_route_hands_a_person_their_user_in_one_read(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """``PrincipalUser`` is ``CurrentUser`` for everyone who is not a machine,
    and costs no second ``users`` read on top of the principal's."""
    raw = await _bearer_jwt(org_admin)
    with counting() as statements:
        resp = await client.get(f"{_PREFIX}/machine-or-user", headers=_bearer(raw))
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == str(org_admin.admin_id)
    assert resp.json()["actor"]["acting"]["kind"] == "user"
    assert _users_reads(statements) == 1


@pytest.mark.parametrize("shape", ["ci", "proxy", "pat"])
async def test_a_machine_or_user_route_still_refuses_every_token_shape(
    client: AsyncClient, org_admin: OrgWithAdmin, shape: str
) -> None:
    """Admitting a machine on a route admits nothing else: a CI, proxy or
    personal access token is refused by ``current_user`` exactly as on a
    ``CurrentUser`` route."""
    if shape == "ci":
        _id, raw = await _ci_token(org_admin)
    elif shape == "proxy":
        _id, raw = await _proxy_token(org_admin)
    else:
        _id, raw = await _pat(org_admin)
    resp = await client.get(f"{_PREFIX}/machine-or-user", headers=_bearer(raw))
    assert resp.status_code == 401, resp.text


@pytest.mark.compute_rows
async def test_a_provisioned_nodes_secret_is_the_machine_and_never_a_person(
    client: AsyncClient, platform_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one secret provisioning ships to a node resolves to the machine
    principal for that allocation — no user behind it, nothing delegated — and
    is refused wherever a person is required, as the admin who provisioned it
    included."""
    from alkera_core.compute.provider import EC2
    from alkera_test_support.compute.fake_nodes import FakeNodeProvider
    from backend.services.compute import provisioning

    fake = FakeNodeProvider(kind=EC2)
    monkeypatch.setattr(provisioning, "make_node_provider", lambda kind, config: fake)
    async with AsyncSessionLocal() as session:
        machine_type = await make_machine_type(session, provider=EC2)
        await session.commit()
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.post(
        "/admin/v1/machines/provision",
        json={
            "provider": "ec2",
            "machine_type_code": machine_type.provider_type_id,
            "storage_gb": 100,
            "tenancy": "pool",
            "name": "pool-a",
        },
    )
    assert resp.status_code == 202, resp.text
    row = resp.json()
    (secret,) = fake.secrets[UUID(row["id"])].values()
    client.cookies.clear()

    resp = await client.get(f"{_PREFIX}/machine-or-user", headers=_bearer(secret))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["actor"]["acting"]["kind"] == "machine"
    assert body["actor"]["delegating_user"] is None
    assert body["subject_id"] == row["id"]
    assert body["effective_user_id"] is None
    for path in (f"{_PREFIX}/user-only", "/api/v1/auth/me"):
        refused = await client.get(path, headers=_bearer(secret))
        assert refused.status_code == 401, (path, refused.text)


# --------------------------------------------------------------------------- #
# the usage stamp never makes a caller's requests queue on its own row
# --------------------------------------------------------------------------- #


async def test_a_machine_request_does_not_wait_on_its_credentials_row(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Another transaction holds the credential's row (a concurrent request
    mid-stamp, under a slow commit). The box's next request must answer at
    once rather than queue behind that lock: the stamp is skipped, not waited
    for. Writing ``last_used_at`` unconditionally made every request of a box
    wait here, which timed its heartbeats out."""
    box = await _machine_credential(org_admin)
    async with AsyncSessionLocal() as holder:
        await holder.execute(
            select(MachineCredential.id)
            .where(MachineCredential.id == box.credential_id)
            .with_for_update()
        )
        resp = await asyncio.wait_for(
            client.get(f"{_PREFIX}/machine-or-user", headers=_bearer(box.raw)), timeout=20
        )
        await holder.rollback()
    assert resp.status_code == 200, resp.text


async def test_concurrent_machine_requests_all_answer(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A burst of one box's requests all authenticate, and the stamp lands once."""
    box = await _machine_credential(org_admin)
    answers = await asyncio.gather(
        *(client.get(f"{_PREFIX}/machine-or-user", headers=_bearer(box.raw)) for _ in range(8))
    )
    assert [answer.status_code for answer in answers] == [200] * 8
    assert await _last_used(MachineCredential, box.credential_id) is not None


async def test_the_usage_stamp_is_written_at_most_once_per_resolution(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Inside the resolution a request writes nothing; past it, the next one
    moves the stamp. The clock is driven across the boundary rather than
    pinned on either side of it."""
    box = await _machine_credential(org_admin)
    with freeze_time("2026-10-04T12:00:00Z", real_asyncio=True) as frozen:
        assert (
            await client.get(f"{_PREFIX}/machine-or-user", headers=_bearer(box.raw))
        ).status_code == 200
        first = await _last_used(MachineCredential, box.credential_id)
        assert first == datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

        frozen.move_to("2026-10-04T12:00:59Z")
        assert (
            await client.get(f"{_PREFIX}/machine-or-user", headers=_bearer(box.raw))
        ).status_code == 200
        assert await _last_used(MachineCredential, box.credential_id) == first

        frozen.move_to("2026-10-04T12:01:01Z")
        assert (
            await client.get(f"{_PREFIX}/machine-or-user", headers=_bearer(box.raw))
        ).status_code == 200
        assert await _last_used(MachineCredential, box.credential_id) == datetime(
            2026, 10, 4, 12, 1, 1, tzinfo=UTC
        )


@pytest.mark.parametrize(
    ("stored", "written"),
    [
        pytest.param(None, True, id="never-stamped"),
        pytest.param(timedelta(seconds=61), True, id="older-than-the-resolution"),
        pytest.param(timedelta(seconds=59), False, id="within-the-resolution"),
        pytest.param(timedelta(seconds=0), False, id="stamped-this-instant"),
    ],
)
async def test_stamp_last_used_writes_only_a_stale_row(
    org_admin: OrgWithAdmin, stored: timedelta | None, written: bool
) -> None:
    box = await _machine_credential(org_admin)
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    async with AsyncSessionLocal() as session:
        row = await session.get(MachineCredential, box.credential_id)
        assert row is not None
        row.last_used_at = None if stored is None else now - stored
        await session.commit()
        before = row.last_used_at
    async with AsyncSessionLocal() as session:
        assert await stamp_last_used(session, MachineCredential, box.credential_id, now) is written
        await session.commit()
    assert await _last_used(MachineCredential, box.credential_id) == (now if written else before)


async def test_stamp_last_used_skips_a_row_another_transaction_holds(
    org_admin: OrgWithAdmin,
) -> None:
    box = await _machine_credential(org_admin)
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    async with AsyncSessionLocal() as holder, AsyncSessionLocal() as session:
        await holder.execute(
            select(MachineCredential.id)
            .where(MachineCredential.id == box.credential_id)
            .with_for_update()
        )
        stamped = await asyncio.wait_for(
            stamp_last_used(session, MachineCredential, box.credential_id, now), timeout=20
        )
        await session.commit()
        await holder.rollback()
    assert stamped is False
    assert await _last_used(MachineCredential, box.credential_id) is None
