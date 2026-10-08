"""Audit-log tests — capture on success, redaction, read gating, pagination.

Reuses the shared dev DB (like the other backend tests). Existence/ordering
assertions scope to a unique marker (org name, actor id, or a random path
segment) so accumulated rows from other tests or runs don't interfere.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import AuditLog, Team
from backend.api.admin._audit import AuditedRoute
from httpx import AsyncClient
from sqlalchemy import select
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, login


async def _rows(*, action: str | None = None, actor_id: UUID | None = None) -> Sequence[AuditLog]:
    async with AsyncSessionLocal() as session:
        stmt = select(AuditLog)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        stmt = stmt.order_by(AuditLog.created_at.desc())
        return (await session.execute(stmt)).scalars().all()


async def _create_org(client: AsyncClient, *, password: str = "supersecret123") -> tuple[str, dict]:
    suffix = secrets.token_hex(6)
    name = f"Audited Org {suffix}"
    resp = await client.post(
        "/admin/v1/orgs",
        json={
            "name": name,
            "admin_email": f"audited-{suffix}@alkera.dev",
            "admin_first_name": "Aud",
            "admin_last_name": "It",
            "admin_password": password,
        },
    )
    assert resp.status_code == 201, resp.text
    return name, resp.json()


# --- capture on success ----------------------------------------------------


@pytest.mark.asyncio
async def test_support_write_action_is_audited(client: AsyncClient, platform_support: OrgWithAdmin):
    await login(client, platform_support.admin_email, platform_support.admin_password)
    name, _ = await _create_org(client)

    rows = await _rows(action="create_org", actor_id=platform_support.admin_id)
    mine = [r for r in rows if r.detail and r.detail.get("body", {}).get("name") == name]
    assert len(mine) == 1
    entry = mine[0]
    assert entry.actor_email == platform_support.admin_email
    assert entry.actor_platform_role == "alkera_support"
    assert entry.method == "POST"
    assert entry.path == "/admin/v1/orgs"
    assert entry.status_code == 201


@pytest.mark.asyncio
async def test_admin_set_platform_role_is_audited(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
):
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.patch(
        f"/admin/v1/users/{org_admin.admin_id}/platform_role",
        json={"platform_role": "alkera_support"},
    )
    assert resp.status_code == 200

    rows = await _rows(action="set_platform_role", actor_id=platform_admin.admin_id)
    mine = [r for r in rows if str(org_admin.admin_id) in r.path]
    assert mine, "expected an audit row for the platform_role change"
    entry = mine[0]
    assert entry.actor_platform_role == "alkera_admin"
    assert entry.detail is not None
    assert entry.detail["body"]["platform_role"] == "alkera_support"
    assert entry.detail["path_params"]["user_id"] == str(org_admin.admin_id)


@pytest.mark.asyncio
async def test_secrets_are_redacted(client: AsyncClient, platform_support: OrgWithAdmin):
    await login(client, platform_support.admin_email, platform_support.admin_password)
    name, _ = await _create_org(client, password="hunter2-should-not-leak")

    rows = await _rows(action="create_org", actor_id=platform_support.admin_id)
    mine = next(r for r in rows if r.detail and r.detail.get("body", {}).get("name") == name)
    assert mine.detail["body"]["admin_password"] == "***"
    assert "hunter2-should-not-leak" not in str(mine.detail)


# --- what is NOT audited ---------------------------------------------------


@pytest.mark.asyncio
async def test_failed_action_is_not_audited(client: AsyncClient, platform_admin: OrgWithAdmin):
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    missing = uuid4()
    resp = await client.patch(f"/admin/v1/users/{missing}", json={"first_name": "Nope"})
    assert resp.status_code == 404

    rows = await _rows(action="update_user")
    assert not any(str(missing) in r.path for r in rows)


@pytest.mark.asyncio
async def test_reads_are_not_audited(client: AsyncClient, platform_admin: OrgWithAdmin):
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    assert (await client.get("/admin/v1/orgs")).status_code == 200
    assert (await client.get("/admin/v1/users")).status_code == 200
    # GET handlers are never recorded — no row carries their route names.
    for read_action in ("list_orgs", "list_users", "list_audit_logs"):
        assert await _rows(action=read_action) == []


# --- read endpoint: gating + pagination ------------------------------------


@pytest.mark.asyncio
async def test_audit_view_requires_auth(client: AsyncClient):
    assert (await client.get("/admin/v1/audit-logs")).status_code == 401


@pytest.mark.asyncio
async def test_audit_view_is_admin_only(client: AsyncClient, platform_support: OrgWithAdmin):
    """Support performs audited actions but cannot read the log."""
    await login(client, platform_support.admin_email, platform_support.admin_password)
    assert (await client.get("/admin/v1/audit-logs")).status_code == 403


@pytest.mark.asyncio
async def test_admin_can_view_audit_log(client: AsyncClient, platform_admin: OrgWithAdmin):
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    name, _ = await _create_org(client)

    resp = await client.get("/admin/v1/audit-logs", params={"page": 1, "page_size": 50})
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert body["total"] >= 1
    assert len(body["items"]) <= 50
    # Newest-first: the org we just created is the freshest write.
    top = body["items"][0]
    assert top["action"] == "create_org"
    assert top["detail"]["body"]["name"] == name


@pytest.mark.asyncio
async def test_audit_log_pagination_advances(client: AsyncClient, platform_admin: OrgWithAdmin):
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    await _create_org(client)
    await _create_org(client)

    p1 = (await client.get("/admin/v1/audit-logs", params={"page": 1, "page_size": 1})).json()
    p2 = (await client.get("/admin/v1/audit-logs", params={"page": 2, "page_size": 1})).json()
    assert len(p1["items"]) == 1
    assert len(p2["items"]) == 1
    assert p1["items"][0]["id"] != p2["items"][0]["id"]


# --- each row names what it was done to -------------------------------------


@pytest.mark.asyncio
async def test_delete_org_names_the_org_it_deleted(
    client: AsyncClient, platform_admin: OrgWithAdmin
):
    """The org is gone after the handler; its name is read before it runs."""
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    name, created = await _create_org(client)
    org_id = created["org"]["id"]
    assert (await client.delete(f"/admin/v1/orgs/{org_id}")).status_code == 204

    body = (
        await client.get("/admin/v1/audit-logs", params={"action": "delete_org", "page_size": 200})
    ).json()
    mine = [i for i in body["items"] if i["path"] == f"/admin/v1/orgs/{org_id}"]
    assert [i["target"] for i in mine] == [name]


@pytest.mark.asyncio
async def test_a_user_action_names_the_user(
    client: AsyncClient, platform_admin: OrgWithAdmin, org_admin: OrgWithAdmin
):
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.patch(
        f"/admin/v1/users/{org_admin.admin_id}/platform_role",
        json={"platform_role": "alkera_support"},
    )
    assert resp.status_code == 200
    body = (
        await client.get(
            "/admin/v1/audit-logs", params={"action": "set_platform_role", "page_size": 200}
        )
    ).json()
    mine = [i for i in body["items"] if str(org_admin.admin_id) in i["path"]]
    assert mine and mine[0]["target"] == org_admin.admin_email


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("where", "expect"),
    [
        pytest.param("path_org", "org", id="org-in-path"),
        pytest.param("body_org", "org", id="org-in-body"),
        pytest.param("path_org_and_user", "org", id="org-wins-over-user"),
        pytest.param("path_user", "user", id="user-in-path"),
        pytest.param("unknown_org", "raw", id="unknown-id-kept-raw"),
        pytest.param("not_a_uuid", None, id="malformed-id-names-nothing"),
        pytest.param("nothing", None, id="no-identifier"),
        pytest.param("list_body", None, id="non-object-body"),
    ],
)
async def test_resolve_target(org_admin: OrgWithAdmin, where: str, expect: str | None) -> None:
    from backend.services.audit import audit_log as audit_log_service

    org, user = str(org_admin.org_id), str(org_admin.admin_id)
    stray = str(uuid4())
    path, body = {
        "path_org": ({"org_id": org}, None),
        "body_org": ({}, {"org_id": org, "seats": 2}),
        "path_org_and_user": ({"user_id": user, "org_id": org}, None),
        "path_user": ({"user_id": user}, None),
        "unknown_org": ({"org_id": stray}, None),
        "not_a_uuid": ({"org_id": "acme"}, None),
        "nothing": ({"model_id": org}, {"name": "x"}),
        "list_body": ({}, [{"org_id": org}]),
    }[where]
    async with AsyncSessionLocal() as session:
        got = await audit_log_service.resolve_target(session, path_params=path, body=body)
        org_name = (
            await session.execute(select(Team.name).where(Team.id == org_admin.org_id))
        ).scalar_one()
    wanted = {"org": org_name, "user": org_admin.admin_email, "raw": stray, None: None}[expect]
    assert got == wanted


@pytest.mark.asyncio
async def test_the_log_filters_by_action_actor_and_time(
    client: AsyncClient, platform_admin: OrgWithAdmin, platform_support: OrgWithAdmin
):
    await login(client, platform_support.admin_email, platform_support.admin_password)
    support_org, _ = await _create_org(client)
    await login(client, platform_admin.admin_email, platform_admin.admin_password)
    admin_org, created = await _create_org(client)
    assert (await client.delete(f"/admin/v1/orgs/{created['org']['id']}")).status_code == 204

    async def page(**params: str) -> dict:
        resp = await client.get("/admin/v1/audit-logs", params={"page_size": 200, **params})
        assert resp.status_code == 200, resp.text
        return resp.json()

    by_action = await page(action="create_org")
    assert {i["action"] for i in by_action["items"]} == {"create_org"}
    assert by_action["total"] == len(by_action["items"])
    assert {"create_org", "delete_org"} <= set(by_action["actions"])

    by_actor = await page(actor_email=platform_support.admin_email.upper())
    assert {i["actor_email"] for i in by_actor["items"]} == {platform_support.admin_email}
    assert support_org in {i["detail"]["body"].get("name") for i in by_actor["items"]}

    newest = (await page())["items"][0]["created_at"]
    after = await page(created_after=newest)
    assert all(i["created_at"] >= newest for i in after["items"])
    before = await page(created_before="2000-01-01T00:00:00")
    assert before["items"] == [] and before["total"] == 0
    assert admin_org  # the admin's create is outside the support actor's filter
    assert all(i["actor_email"] != platform_admin.admin_email for i in by_actor["items"])


# --- completeness: every admin route is audited ----------------------------


def test_all_admin_routes_use_audited_route():
    """A future /admin/v1 sub-router that forgets route_class fails here."""
    offenders = [
        route.path
        for route in fastapi_app.routes
        if getattr(route, "path", "").startswith("/admin/v1")
        and not isinstance(route, AuditedRoute)
    ]
    assert offenders == [], f"un-audited admin routes: {offenders}"
