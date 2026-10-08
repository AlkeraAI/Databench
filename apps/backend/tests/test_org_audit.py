"""Per-org audit trail: emit points fire on real actions, the read/export surface
is org-scoped, and secrets are redacted.

Actions are driven through the real HTTP routes so the audit *wiring* (not just
the service) is exercised end-to-end.
"""

from __future__ import annotations

import csv
import io
import uuid
from uuid import UUID

import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import IdentitySecurityEvent, OrgAuditEvent
from backend.services.audit import org_audit as org_audit_service
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import (
    OrgWithAdmin,
    login_via_route,
    make_member,
    make_org_enterprise,
    mint_cli_token,
)

pytestmark = pytest.mark.asyncio


async def _admin(client: AsyncClient, org: OrgWithAdmin) -> None:
    await login_via_route(client, org.admin_email, org.admin_password)


async def _member(org_id: UUID) -> tuple[UUID, str]:
    async with AsyncSessionLocal() as s:
        user, _pw = await make_member(s, org_id=org_id, verified=True)
        return user.id, user.email


async def _events(org_id: UUID) -> list[OrgAuditEvent]:
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_team_id == org_id))
        ).scalars()
        return list(rows)


# --------------------------------------------------------------------------- #
# emit points fire
# --------------------------------------------------------------------------- #


async def test_login_is_audited(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await _admin(client, org_admin)
    events = await _events(org_admin.org_id)
    assert any(e.action == "auth.login" and e.target == org_admin.admin_email for e in events)


async def test_budget_set_is_audited(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await _admin(client, org_admin)
    member_id, member_email = await _member(org_admin.org_id)
    resp = await client.put(
        f"/api/v1/org/billing/members/{member_id}/budget", json={"amount_usd": "50.00"}
    )
    assert resp.status_code == 200
    events = await _events(org_admin.org_id)
    budget_events = [e for e in events if e.action == "billing.budget_set"]
    assert len(budget_events) == 1
    assert budget_events[0].target == member_email
    assert budget_events[0].actor_email == org_admin.admin_email


async def test_sso_config_change_is_audited_without_the_secret(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await _admin(client, org_admin)
    await make_org_enterprise(org_admin.org_id)  # SSO config is Enterprise-gated on SaaS
    await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": "https://idp.acme.test",
            "oidc_client_id": "cid",
            "oidc_client_secret": "TOP-SECRET-VALUE",
            "enabled": True,
        },
    )
    events = await _events(org_admin.org_id)
    sso_events = [e for e in events if e.action == "sso.config_updated"]
    assert len(sso_events) == 1
    # The secret must NEVER reach the audit detail.
    assert "TOP-SECRET-VALUE" not in str(sso_events[0].detail)


async def test_membership_role_change_is_audited(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await _admin(client, org_admin)
    member_id, member_email = await _member(org_admin.org_id)
    resp = await client.patch(
        f"/api/v1/teams/{org_admin.org_id}/memberships/{member_id}", json={"role": "admin"}
    )
    assert resp.status_code == 200, resp.text
    events = await _events(org_admin.org_id)
    assert any(e.action == "membership.role_changed" and e.target == member_email for e in events)


async def test_invitation_is_audited(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch_email_send: list[dict]
) -> None:
    await _admin(client, org_admin)
    invited = f"newhire-{uuid.uuid4().hex[:8]}@external-co.example.com"
    resp = await client.post(
        f"/api/v1/teams/{org_admin.org_id}/invitations", json={"email": invited, "role": "member"}
    )
    assert resp.status_code == 201, resp.text
    events = await _events(org_admin.org_id)
    assert any(e.action == "invitation.created" and e.target == invited for e in events)


async def test_a_failed_login_is_the_identitys_event_not_the_orgs(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    # A brute-force against a real account: wrong password → recorded in the
    # identity's security log; no org's chain carries it.
    async with AsyncSessionLocal() as s:
        user, _pw = await make_member(s, org_id=org_admin.org_id, verified=True)
        email, user_id = user.email, user.id
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "definitely-wrong"}
    )
    assert resp.status_code == 401
    events = await _events(org_admin.org_id)
    assert not any(e.action == "auth.login_failed" and e.target == email for e in events)
    async with AsyncSessionLocal() as s:
        recorded = await s.scalar(
            select(IdentitySecurityEvent.event).where(IdentitySecurityEvent.user_id == user_id)
        )
    assert recorded == "auth.login_failed"


# --------------------------------------------------------------------------- #
# read / export surface
# --------------------------------------------------------------------------- #


async def test_list_is_paginated_and_newest_first(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await _admin(client, org_admin)  # 1 login event
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    body = (await client.get("/api/v1/org/audit-events?limit=10")).json()
    assert body["total"] >= 1
    assert body["limit"] == 10
    assert len(body["events"]) >= 1
    # Newest first.
    times = [e["created_at"] for e in body["events"]]
    assert times == sorted(times, reverse=True)


async def test_csv_export(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await _admin(client, org_admin)
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    resp = await client.get("/api/v1/org/audit-events/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "timestamp,actor_email,action,target,detail" in resp.text
    assert "auth.login" in resp.text


async def test_non_admin_cannot_read(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    # Enroll so the org-admin guard (not the Enterprise gate) is what denies the member
    # — this test exists to prove the admin check, not the plan check.
    await make_org_enterprise(org_admin.org_id)
    async with AsyncSessionLocal() as s:
        user, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
    await login_via_route(client, user.email, pw)
    assert (await client.get("/api/v1/org/audit-events")).status_code == 403


async def test_cross_org_isolation(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    # A second org with its own admin + an action.
    from backend.services.org import teams as team_service

    async with AsyncSessionLocal() as s:
        _org, admin_b = await team_service.create_org_with_admin(
            s,
            org_name=f"orgB-{uuid.uuid4().hex[:8]}",
            admin_email=f"adminB-{uuid.uuid4().hex[:8]}@alkera.dev",
            admin_first_name="B",
            admin_last_name="Admin",
            admin_password="b-pass-123456",
        )
        await s.commit()
        b_email, b_org = admin_b.email, admin_b.home_org_team_id
    await login_via_route(client, b_email, "b-pass-123456")  # logs B in (audits to org B)

    await _admin(client, org_admin)  # back to org A
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    body = (await client.get("/api/v1/org/audit-events")).json()
    actors = {e["actor_email"] for e in body["events"]}
    assert org_admin.admin_email in actors
    assert b_email not in actors  # org A never sees org B's events
    assert all(str(b_org) not in str(e) for e in body["events"])


# --------------------------------------------------------------------------- #
# list / export filters
# --------------------------------------------------------------------------- #


async def _seed_filterable_org(client: AsyncClient, org_admin: OrgWithAdmin) -> str:
    """Enroll, log a member in, ingest two agent events as them, then log the
    admin in. Returns the member's email. Org rows afterward: two ``auth.login``
    (member + admin) and two ``agent.*`` rows attributed to the member."""
    await make_org_enterprise(org_admin.org_id)
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
        member_email = member.email
    await login_via_route(client, member_email, pw)
    resp = await client.post(
        _AGENT_URL,
        json={
            "events": [
                _agent_event(action="agent.session_started", target="scratch-project"),
                _agent_event(),
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    await _admin(client, org_admin)
    return member_email


@pytest.mark.parametrize(
    ("params", "expected_actions"),
    [
        pytest.param(
            {"action": "agent."},
            {"agent.session_started", "agent.decision_denied"},
            id="prefix-selects-the-agent-family",
        ),
        pytest.param(
            {"action": "agent.decision_denied"},
            {"agent.decision_denied"},
            id="full-action-matches-itself",
        ),
        pytest.param({"action": "agent.%"}, set(), id="like-percent-is-literal"),
        pytest.param({"action": "agent_"}, set(), id="like-underscore-is-literal"),
        pytest.param({"action": "gent."}, set(), id="prefix-anchors-at-the-start"),
    ],
)
async def test_list_filters_by_action_prefix(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    params: dict[str, str],
    expected_actions: set[str],
) -> None:
    await _seed_filterable_org(client, org_admin)
    body = (await client.get("/api/v1/org/audit-events", params=params)).json()
    assert {e["action"] for e in body["events"]} == expected_actions
    assert body["total"] == len(body["events"])


async def test_list_filters_by_actor_email_exactly(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    member_email = await _seed_filterable_org(client, org_admin)
    body = (
        await client.get("/api/v1/org/audit-events", params={"actor_email": member_email})
    ).json()
    assert body["total"] == 3  # the member's login + both agent rows
    assert {e["actor_email"] for e in body["events"]} == {member_email}
    # A partial email is no match — the filter is exact, not a substring.
    partial = member_email.split("@")[0]
    body = (await client.get("/api/v1/org/audit-events", params={"actor_email": partial})).json()
    assert body["total"] == 0


async def test_list_filters_by_actor_email_ignores_casing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    # Emails are stored lowercased (user_service normalizes on write), and login
    # already resolves case-insensitively. A copied, display-cased address in the
    # actor filter must find the same rows — not silently match nothing.
    member_email = await _seed_filterable_org(client, org_admin)
    body = (
        await client.get("/api/v1/org/audit-events", params={"actor_email": member_email.upper()})
    ).json()
    assert body["total"] == 3
    assert {e["actor_email"] for e in body["events"]} == {member_email}


async def test_list_filters_by_time_range_inclusive(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await _seed_filterable_org(client, org_admin)
    all_events = (await client.get("/api/v1/org/audit-events")).json()["events"]
    times = [e["created_at"] for e in all_events]  # newest first, strictly monotonic
    newest, oldest = times[0], times[-1]

    body = (await client.get("/api/v1/org/audit-events", params={"created_after": newest})).json()
    assert [e["created_at"] for e in body["events"]] == [newest]  # bound is inclusive
    body = (await client.get("/api/v1/org/audit-events", params={"created_before": oldest})).json()
    assert [e["created_at"] for e in body["events"]] == [oldest]

    window = {"created_after": times[-1], "created_before": times[1]}
    body = (await client.get("/api/v1/org/audit-events", params=window)).json()
    assert [e["created_at"] for e in body["events"]] == times[1:]


async def test_offsetless_time_bound_reads_as_utc(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await _seed_filterable_org(client, org_admin)
    newest = (await client.get("/api/v1/org/audit-events")).json()["events"][0]["created_at"]
    naive = newest.replace("+00:00", "").replace("Z", "")
    assert naive != newest  # the bound really lost its offset
    aware = (await client.get("/api/v1/org/audit-events", params={"created_after": newest})).json()
    bare = (await client.get("/api/v1/org/audit-events", params={"created_after": naive})).json()
    assert [e["id"] for e in bare["events"]] == [e["id"] for e in aware["events"]]


async def test_filtered_total_spans_pages(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    # A filtered set larger than one page: the count and the page share one scope.
    await _seed_filterable_org(client, org_admin)
    params = {"action": "agent.", "limit": "1"}
    first = (await client.get("/api/v1/org/audit-events", params=params)).json()
    second = (await client.get("/api/v1/org/audit-events", params={**params, "offset": "1"})).json()
    assert first["total"] == second["total"] == 2
    assert len(first["events"]) == len(second["events"]) == 1
    assert {first["events"][0]["id"]} != {second["events"][0]["id"]}


async def test_list_filters_compose(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await _seed_filterable_org(client, org_admin)
    # The admin has a login row but no agent rows: composed filters intersect.
    params = {"action": "agent.", "actor_email": org_admin.admin_email}
    body = (await client.get("/api/v1/org/audit-events", params=params)).json()
    assert body["total"] == 0 and body["events"] == []


async def test_list_rejects_malformed_time_bound(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await _seed_filterable_org(client, org_admin)
    resp = await client.get("/api/v1/org/audit-events", params={"created_after": "yesterdayish"})
    assert resp.status_code == 422


async def test_csv_export_honors_filters(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await _seed_filterable_org(client, org_admin)
    resp = await client.get("/api/v1/org/audit-events/export.csv", params={"action": "agent."})
    assert resp.status_code == 200
    assert "agent.decision_denied" in resp.text
    assert "auth.login" not in resp.text


# --------------------------------------------------------------------------- #
# redaction (unit)
# --------------------------------------------------------------------------- #


async def test_record_redacts_sensitive_detail(org_admin: OrgWithAdmin) -> None:
    async with AsyncSessionLocal() as s:
        from backend.services.identity import users as user_service

        actor = await user_service.get_by_id(s, org_admin.admin_id)
        event = await org_audit_service.record(
            s,
            org_id=org_admin.org_id,
            actor=actor,
            action="test.redaction",
            detail={"client_secret": "leak-me", "amount_usd": "10.00", "nested": {"token": "t"}},
        )
        await s.commit()
        assert event.detail == {
            "client_secret": "***",
            "amount_usd": "10.00",
            "nested": {"token": "***"},
        }


# --------------------------------------------------------------------------- #
# agent-activity ingest (member write)
# --------------------------------------------------------------------------- #

_AGENT_URL = "/api/v1/org/audit-events/agent"


def _agent_event(**overrides: object) -> dict:
    event: dict = {
        "action": "agent.decision_denied",
        "session_id": "sess-0001",
        "occurred_at": "2026-07-19T10:00:00+00:00",
        "target": "sql.execute",
        "detail": {"capability": "sql", "decided_by": "rule"},
    }
    event.update(overrides)
    return event


async def test_agent_events_land_attributed_and_chained(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
        member_email = member.email
    await login_via_route(client, member_email, pw)

    resp = await client.post(
        _AGENT_URL,
        json={
            "events": [
                _agent_event(action="agent.session_started", target="scratch-project"),
                _agent_event(),
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json() == {"accepted": 2}

    events = await _events(org_admin.org_id)
    started = [e for e in events if e.action == "agent.session_started"]
    denied = [e for e in events if e.action == "agent.decision_denied"]
    assert len(started) == 1 and len(denied) == 1
    assert started[0].actor_email == member_email
    # session_id + the client clock land in detail; chain order is receipt time.
    assert denied[0].detail["session_id"] == "sess-0001"
    assert denied[0].detail["occurred_at"] == "2026-07-19T10:00:00+00:00"
    assert denied[0].detail["capability"] == "sql"

    # Agent rows interleave with auth.login rows without breaking the chain.
    await _admin(client, org_admin)
    verify = (await client.get("/api/v1/org/audit-events/verify")).json()
    assert verify["ok"] is True


async def test_agent_ingest_rejects_non_agent_action(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
    await login_via_route(client, member.email, pw)
    resp = await client.post(
        _AGENT_URL, json={"events": [_agent_event(action="billing.budget_set")]}
    )
    assert resp.status_code == 422
    events = await _events(org_admin.org_id)
    assert not any(e.action == "billing.budget_set" for e in events)


async def test_agent_actor_is_server_stamped(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    """A payload claiming someone else's identity still lands attributed to the
    authenticated caller."""
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
        member_email = member.email
    await login_via_route(client, member_email, pw)
    resp = await client.post(
        _AGENT_URL,
        json={
            "events": [
                _agent_event(
                    detail={"actor_email": org_admin.admin_email, "actor_id": "someone-else"}
                )
            ]
        },
    )
    assert resp.status_code == 201
    events = await _events(org_admin.org_id)
    rows = [e for e in events if e.action == "agent.decision_denied"]
    assert rows and all(e.actor_email == member_email for e in rows)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("stdout", "drwxr-xr-x 42 files", id="command-output"),
        pytest.param("file_content", "def secret(): ...", id="file-content"),
        pytest.param("sql_text", "select ssn from customers", id="sql-text"),
        pytest.param("rows", [["alice", "555-0100"]], id="sql-results"),
        pytest.param("prompt", "system: you are...", id="prompt"),
        pytest.param("raw", {"tool": "bash", "args": "cat /etc/passwd"}, id="raw-descriptor"),
    ],
)
async def test_agent_content_keys_never_reach_the_chain(
    client: AsyncClient, org_admin: OrgWithAdmin, key: str, value: object
) -> None:
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
    await login_via_route(client, member.email, pw)
    resp = await client.post(
        _AGENT_URL,
        json={"events": [_agent_event(detail={key: value, "nested": {key: value}, "kept": "x"})]},
    )
    assert resp.status_code == 201
    events = await _events(org_admin.org_id)
    row = next(e for e in events if e.action == "agent.decision_denied")
    assert row.detail[key] == "***"
    assert row.detail["nested"][key] == "***"
    assert row.detail["kept"] == "x"


async def test_agent_target_cannot_smuggle_trace_content(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    # detail is content-key redacted, but the free-form target bypasses that.
    # A newline/tab marks content (SQL, a prompt, a command), never a table or
    # path — it's dropped; a long target is clamped to a resource identifier.
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
    await login_via_route(client, member.email, pw)
    multiline = "SELECT ssn FROM customers\nWHERE id = 1"
    resp = await client.post(
        _AGENT_URL,
        json={
            "events": [
                _agent_event(action="agent.data_access", target=multiline),
                _agent_event(action="agent.session_started", target="t" * 320),
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    events = await _events(org_admin.org_id)
    access = next(e for e in events if e.action == "agent.data_access")
    started = next(e for e in events if e.action == "agent.session_started")
    assert access.target is None  # multiline content never stored
    assert started.target is not None and len(started.target) <= 200


async def test_agent_detail_over_cap_is_rejected(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
    await login_via_route(client, member.email, pw)
    resp = await client.post(
        _AGENT_URL, json={"events": [_agent_event(detail={"note": "x" * 5000})]}
    )
    assert resp.status_code == 422
    assert "exceeds" in resp.text


async def test_agent_batch_cap(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
    await login_via_route(client, member.email, pw)
    resp = await client.post(_AGENT_URL, json={"events": [_agent_event()] * 101})
    assert resp.status_code == 422


async def test_agent_ingest_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(_AGENT_URL, json={"events": [_agent_event()]})
    assert resp.status_code == 401


async def test_agent_ingest_accepts_bearer(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    """The daemon path: the long-lived CLI token authenticates the reporter."""
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    async with AsyncSessionLocal() as s:
        member, _pw = await make_member(s, org_id=org_admin.org_id, verified=True)
        member_id, member_email = member.id, member.email
    token = await mint_cli_token(
        user_id=member_id, email=member_email, org_team_id=org_admin.org_id
    )
    resp = await client.post(
        _AGENT_URL,
        json={"events": [_agent_event(action="agent.session_finished")]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    events = await _events(org_admin.org_id)
    rows = [e for e in events if e.action == "agent.session_finished"]
    assert rows and rows[0].actor_email == member_email


async def test_agent_ingest_is_recorded_without_enterprise_but_never_read(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Evidence is recorded for every organization; the plan decides who may
    READ it. A dropped agent event is evidence nobody can get back — and a plan
    gate on the ingest made every daemon in a plan-less org log a 403 for ever
    and throw its spool away, which is how the demo box's trail was lost.

    Deleting the read gate must fail this test, and so must putting one back on
    the write."""
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
    await login_via_route(client, member.email, pw)

    resp = await client.post(_AGENT_URL, json={"events": [_agent_event()]})
    assert resp.status_code == 201, resp.text
    events = await _events(org_admin.org_id)
    assert [e.action for e in events if e.action.startswith("agent.")] == ["agent.decision_denied"]

    # The same org's admin still cannot read what was recorded, and the refusal
    # names itself so the SPA's gate and a client can key off it.
    await _admin(client, org_admin)
    read = await client.get("/api/v1/org/audit-events")
    assert read.status_code == 403
    assert read.json()["error"]["code"] == "audit_not_entitled"


async def test_tampered_agent_row_breaks_the_chain(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
    await login_via_route(client, member.email, pw)
    assert (await client.post(_AGENT_URL, json={"events": [_agent_event()]})).status_code == 201

    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                select(OrgAuditEvent).where(
                    OrgAuditEvent.org_team_id == org_admin.org_id,
                    OrgAuditEvent.action == "agent.decision_denied",
                )
            )
        ).scalar_one()
        row.detail = {"capability": "sql", "decided_by": "forged"}
        await s.commit()

    await _admin(client, org_admin)
    verify = (await client.get("/api/v1/org/audit-events/verify")).json()
    assert verify["ok"] is False


# --------------------------------------------------------------------------- #
# CSV export: formula injection
# --------------------------------------------------------------------------- #


async def _export_rows(client: AsyncClient, org: OrgWithAdmin) -> list[list[str]]:
    await _admin(client, org)
    resp = await client.get("/api/v1/org/audit-events/export.csv")
    assert resp.status_code == 200
    return list(csv.reader(io.StringIO(resp.text)))


async def _ingest_target(client: AsyncClient, org: OrgWithAdmin, target: str) -> None:
    """Plant `target` through the MEMBER-level agent ingest — the real source
    an attacker controls (reads are admin-only, this one write is not)."""
    await make_org_enterprise(org.org_id)
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(s, org_id=org.org_id, verified=True)
        email = member.email
    await login_via_route(client, email, pw)
    resp = await client.post(_AGENT_URL, json={"events": [_agent_event(target=target)]})
    assert resp.status_code == 201, resp.text


async def _record_target(org: OrgWithAdmin, target: str) -> None:
    """Write an event straight through the audit service — the only way to land
    a tab/CR-prefixed target, which the agent ingest's scrubber drops."""
    async with AsyncSessionLocal() as s:
        await org_audit_service.record(
            s, org_id=org.org_id, actor=None, action="agent.data_access", target=target
        )
        await s.commit()


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param("=cmd|'/C calc'!A0", id="equals-dde"),
        pytest.param("+1+1", id="plus"),
        pytest.param("-1+1", id="minus"),
        pytest.param("@SUM(A1:A9)", id="at"),
    ],
)
async def test_csv_export_neutralizes_formula_targets(
    client: AsyncClient, org_admin: OrgWithAdmin, planted: str
) -> None:
    """A member-planted formula must reach the admin's spreadsheet as TEXT."""
    await _ingest_target(client, org_admin, planted)
    rows = await _export_rows(client, org_admin)
    targets = [r[3] for r in rows[1:]]
    assert "'" + planted in targets
    assert planted not in targets


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param("\tSUM(A1)", id="tab"),
        pytest.param("\rSUM(A1)", id="carriage-return"),
    ],
)
async def test_csv_export_neutralizes_control_prefixed_cells(
    client: AsyncClient, org_admin: OrgWithAdmin, planted: str
) -> None:
    """A leading tab/CR also triggers spreadsheet evaluation. The agent ingest
    scrubs those, so they can only arrive from another emit point — the export
    still has to neutralize them."""
    await make_org_enterprise(org_admin.org_id)
    await _record_target(org_admin, planted)
    rows = await _export_rows(client, org_admin)
    assert any(
        cell.startswith("'") and cell.endswith("SUM(A1)") for cell in (r[3] for r in rows[1:])
    )


async def test_csv_export_stores_the_target_verbatim(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The neutralization happens at EXPORT only: the persisted value feeds the
    tamper-evident hash chain, so it must stay byte-identical to what was sent
    (and the chain must still verify)."""
    planted = '=HYPERLINK("https://attacker.example")'
    await _ingest_target(client, org_admin, planted)

    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                select(OrgAuditEvent).where(
                    OrgAuditEvent.org_team_id == org_admin.org_id,
                    OrgAuditEvent.action == "agent.decision_denied",
                )
            )
        ).scalar_one()
        assert row.target == planted

    await _admin(client, org_admin)
    assert (await client.get("/api/v1/org/audit-events/verify")).json()["ok"] is True
    listed = (await client.get("/api/v1/org/audit-events")).json()["events"]
    assert any(e["target"] == planted for e in listed)  # JSON reads stay verbatim


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param("sql.execute", id="plain"),
        pytest.param("scratch-project", id="hyphen-inside"),
        pytest.param("a=b", id="equals-not-leading"),
        pytest.param("2026-07-27 rollout", id="digit-first"),
    ],
)
async def test_csv_export_leaves_ordinary_targets_untouched(
    client: AsyncClient, org_admin: OrgWithAdmin, planted: str
) -> None:
    """Asymmetric case: only a LEADING formula character is quoted — an
    over-eager escape would corrupt every normal target."""
    await _ingest_target(client, org_admin, planted)
    rows = await _export_rows(client, org_admin)
    assert planted in [r[3] for r in rows[1:]]


async def test_csv_export_is_served_as_a_nosniff_attachment(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await _admin(client, org_admin)
    await make_org_enterprise(org_admin.org_id)
    resp = await client.get("/api/v1/org/audit-events/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"] == "attachment; filename=org-audit-events.csv"
    assert resp.headers["x-content-type-options"] == "nosniff"


# --------------------------------------------------------------------------- #
# server-only actions never enter the chain from a client
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("action", "reportable"),
    [
        pytest.param("agent.decision_denied", True, id="agent-action"),
        pytest.param("agent.cost", True, id="agent-cost"),
        pytest.param("agent.", True, id="bare-agent-prefix-is-still-the-agent-namespace"),
        pytest.param("authz.decision", False, id="authz-decision"),
        pytest.param("authz.anything", False, id="authz-prefix"),
        pytest.param("billing.budget_set", False, id="server-emitted-action"),
        pytest.param("agent", False, id="agent-without-the-dot"),
        pytest.param("agentx.thing", False, id="near-miss-prefix"),
        pytest.param("Agent.cost", False, id="wrong-case"),
        pytest.param("", False, id="empty"),
    ],
)
async def test_client_reportable(action: str, reportable: bool) -> None:
    assert org_audit_service.client_reportable(action) is reportable


async def test_client_reportable_honours_a_widened_server_only_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Today every server-only prefix lies outside the agent namespace, so the
    prefix check alone would refuse them; the predicate still consults the
    server-only registry so that a decision type ever nested under ``agent.``
    stays unreportable without this guard being remembered."""
    from alkera_core.authz import decision as decision_module

    monkeypatch.setattr(
        decision_module, "SERVER_ONLY_EVENT_PREFIXES", ("authz.", "agent.decision_")
    )
    assert org_audit_service.client_reportable("agent.decision_denied") is False
    assert org_audit_service.client_reportable("agent.cost") is True


@pytest.mark.parametrize("action", ["authz.decision", "authz.anything"])
async def test_agent_ingest_rejects_server_only_authz_actions(
    client: AsyncClient, org_admin: OrgWithAdmin, action: str
) -> None:
    """An authorization decision is written by the server for every allow and
    deny; a client claiming one is forging the record. 422, nothing lands."""
    await make_org_enterprise(org_admin.org_id)  # audit log is Enterprise-gated on SaaS
    async with AsyncSessionLocal() as s:
        member, pw = await make_member(s, org_id=org_admin.org_id, verified=True)
    await login_via_route(client, member.email, pw)
    resp = await client.post(_AGENT_URL, json={"events": [_agent_event(action=action)]})
    assert resp.status_code == 422
    events = await _events(org_admin.org_id)
    assert not any(e.action.startswith("authz.") for e in events)


@pytest.mark.parametrize(
    "action",
    [
        pytest.param("authz.decision", id="authz-decision"),
        pytest.param("billing.budget_set", id="server-emitted-action"),
        pytest.param("agentx.cost", id="near-miss-prefix"),
    ],
)
async def test_record_agent_batch_refuses_a_non_agent_action(
    org_admin: OrgWithAdmin, action: str
) -> None:
    """Belt and braces below the schema: an event object that never passed
    Pydantic (a SimpleNamespace) is refused by the service itself, and a batch
    with one forged action writes nothing at all."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from alkera_core.models import User

    async with AsyncSessionLocal() as s:
        actor = (await s.execute(select(User).where(User.id == org_admin.admin_id))).scalar_one()
        good = SimpleNamespace(
            action="agent.cost",
            session_id="sess-1",
            occurred_at=datetime.now(UTC),
            target=None,
            detail={"usd": "0.01"},
        )
        forged = SimpleNamespace(
            action=action,
            session_id="sess-1",
            occurred_at=datetime.now(UTC),
            target=None,
            detail={},
        )
        with pytest.raises(ValueError, match="client-reported audit actions must be agent"):
            await org_audit_service.record_agent_batch(
                s,
                org_id=org_admin.org_id,
                actor=actor,
                events=[good, forged],  # type: ignore[list-item]
            )
        await s.rollback()
    events = await _events(org_admin.org_id)
    assert not any(e.action in (action, "agent.cost") for e in events)


# --------------------------------------------------------------------------- #
# record(acting=): the actor chain lands in detail
# --------------------------------------------------------------------------- #


async def test_record_with_an_acting_context_lands_the_actor_chain_in_detail(
    org_admin: OrgWithAdmin,
) -> None:
    from alkera_core.authz import ActingContext
    from alkera_core.models import User

    async with AsyncSessionLocal() as s:
        actor = (await s.execute(select(User).where(User.id == org_admin.admin_id))).scalar_one()
        ctx = ActingContext.for_agent(
            user_id=actor.id, org_id=actor.home_org_team_id, email=actor.email, session_id="sess-42"
        )
        row = await org_audit_service.record(
            s,
            org_id=org_admin.org_id,
            actor=actor,
            action="team_connection.credential_fetched",
            detail={"connection_id": "c-1", "password": "hunter2"},
            acting=ctx,
        )
        await s.commit()
        row_id = row.id

    async with AsyncSessionLocal() as s:
        stored = (
            await s.execute(select(OrgAuditEvent).where(OrgAuditEvent.id == row_id))
        ).scalar_one()
    assert stored.actor_id == org_admin.admin_id
    assert stored.actor_email == org_admin.admin_email
    assert stored.detail is not None
    assert stored.detail["connection_id"] == "c-1"
    assert stored.detail["password"] == "***"  # redaction still applies beside the actor
    assert stored.detail["actor"] == ctx.audit_dict()
    assert stored.detail["actor"]["acting"]["kind"] == "agent"
    assert [link["kind"] for link in stored.detail["actor"]["chain"]] == ["user", "agent"]
    # The stored document is exactly what was hashed: the chain still verifies.
    async with AsyncSessionLocal() as s:
        verification = await org_audit_service.verify_chain(s, org_id=org_admin.org_id)
    assert verification.ok is True


async def test_record_with_an_acting_context_and_no_detail_still_carries_the_actor(
    org_admin: OrgWithAdmin,
) -> None:
    from alkera_core.authz import ActingContext
    from alkera_core.models import User

    async with AsyncSessionLocal() as s:
        actor = (await s.execute(select(User).where(User.id == org_admin.admin_id))).scalar_one()
        ctx = ActingContext.for_user(
            user_id=actor.id, org_id=actor.home_org_team_id, email=actor.email
        )
        row = await org_audit_service.record(
            s, org_id=org_admin.org_id, actor=actor, action="billing.team_budget_set", acting=ctx
        )
        await s.commit()
        assert row.detail == {"actor": ctx.audit_dict()}


async def test_record_without_an_acting_context_leaves_detail_exactly_as_before(
    org_admin: OrgWithAdmin,
) -> None:
    from alkera_core.models import User

    async with AsyncSessionLocal() as s:
        actor = (await s.execute(select(User).where(User.id == org_admin.admin_id))).scalar_one()
        row = await org_audit_service.record(
            s, org_id=org_admin.org_id, actor=actor, action="teams.renamed", detail={"name": "x"}
        )
        await s.commit()
        assert row.detail == {"name": "x"}
        bare = await org_audit_service.record(
            s, org_id=org_admin.org_id, actor=actor, action="teams.renamed"
        )
        await s.commit()
        assert bare.detail is None
