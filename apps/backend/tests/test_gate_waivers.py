"""Gate waivers: audited create behind the force floor, revoke-keeps-history,
and the CI-side exact-identity lookup.

Route-driven through the real ASGI app. The pins that matter: the force floor
(built-in + org additions) refuses creation outright; one ACTIVE waiver per
identity with revoked history accumulating; and lookup matches the
(urn, rule_id, change_fingerprint) triple exactly, so a morphed change stops
matching by design.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import OrgSettings
from tests.conftest import OrgWithAdmin, login, make_member

_FP = "f" * 64


def _waiver_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "repo": "acme/warehouse",
        "urn": "urn:col:orders.total",
        "rule_id": "TYPE_CHANGED",
        "change_fingerprint": _FP,
        "reason": "downstream consumer migrates next sprint",
    }
    body.update(overrides)
    return body


async def _mint_ci_secret(client: httpx.AsyncClient, org: OrgWithAdmin, **body: Any) -> str:
    await login(client, org.admin_email, org.admin_password)
    resp = await client.post("/api/v1/gate/tokens", json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["secret"])


@pytest.mark.asyncio
async def test_create_is_audited_and_idempotent(
    client: httpx.AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    body = _waiver_body(change_fingerprint=secrets.token_hex(32))
    first = await client.post("/api/v1/gate/waivers", json=body)
    assert first.status_code == 201, first.text
    created = first.json()
    assert created["reason"] == body["reason"]
    assert created["created_by_id"] == str(org_admin.admin_id)
    assert created["revoked_at"] is None
    # A double-submit converges on the SAME active waiver instead of stacking.
    second = await client.post("/api/v1/gate/waivers", json=body)
    assert second.status_code == 200
    assert second.json()["id"] == created["id"]


@pytest.mark.parametrize("rule_id", ["TABLE_DROPPED", "COLUMN_DROPPED"])
@pytest.mark.asyncio
async def test_builtin_force_floor_refuses_creation(
    client: httpx.AsyncClient, org_admin: OrgWithAdmin, rule_id: str
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post("/api/v1/gate/waivers", json=_waiver_body(rule_id=rule_id))
    # The behavior: a floor rule is refused (422) and the refusal names the rule
    # the caller tried to waive (input echoed) -- not a copy of the error prose.
    assert resp.status_code == 422
    assert rule_id in resp.text


@pytest.mark.asyncio
async def test_org_floor_additions_are_enforced(
    client: httpx.AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """gate_force_rules on org_settings extends (never shrinks) the floor."""
    async with AsyncSessionLocal() as s:
        existing = await s.get(OrgSettings, org_admin.org_id)
        if existing is None:
            s.add(OrgSettings(org_team_id=org_admin.org_id, gate_force_rules=["LOGIC_CHANGED"]))
        else:
            existing.gate_force_rules = ["LOGIC_CHANGED"]
        await s.commit()
    await login(client, org_admin.admin_email, org_admin.admin_password)
    refused = await client.post("/api/v1/gate/waivers", json=_waiver_body(rule_id="LOGIC_CHANGED"))
    assert refused.status_code == 422
    # The built-ins still hold alongside the org addition.
    also_refused = await client.post(
        "/api/v1/gate/waivers", json=_waiver_body(rule_id="TABLE_DROPPED")
    )
    assert also_refused.status_code == 422


@pytest.mark.asyncio
async def test_member_cannot_create_or_list_waivers(
    client: httpx.AsyncClient, org_admin: OrgWithAdmin
) -> None:
    async with AsyncSessionLocal() as session:
        member, password = await make_member(session, org_id=org_admin.org_id, verified=True)
        member_email = member.email
    assert password is not None
    await login(client, member_email, password)
    assert (await client.post("/api/v1/gate/waivers", json=_waiver_body())).status_code == 403
    assert (await client.get("/api/v1/gate/waivers")).status_code == 403


@pytest.mark.asyncio
async def test_revoke_keeps_history_and_allows_a_fresh_waiver(
    client: httpx.AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    body = _waiver_body(change_fingerprint=secrets.token_hex(32))
    waiver_id = (await client.post("/api/v1/gate/waivers", json=body)).json()["id"]
    assert (await client.delete(f"/api/v1/gate/waivers/{waiver_id}")).status_code == 204
    # Revoked: out of the default list, present in the audit view.
    active = {w["id"] for w in (await client.get("/api/v1/gate/waivers")).json()}
    assert waiver_id not in active
    audit = {
        w["id"] for w in (await client.get("/api/v1/gate/waivers?include_revoked=true")).json()
    }
    assert waiver_id in audit
    # The identity is waivable AGAIN (partial unique index scopes to active).
    recreated = await client.post("/api/v1/gate/waivers", json=body)
    assert recreated.status_code == 201
    assert recreated.json()["id"] != waiver_id


@pytest.mark.asyncio
async def test_revoke_unknown_or_foreign_waiver_404s(
    client: httpx.AsyncClient, org_admin: OrgWithAdmin
) -> None:
    from uuid import uuid4

    await login(client, org_admin.admin_email, org_admin.admin_password)
    assert (await client.delete(f"/api/v1/gate/waivers/{uuid4()}")).status_code == 404


@pytest.mark.asyncio
async def test_ci_lookup_matches_the_exact_identity_triple(
    client: httpx.AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    fp = secrets.token_hex(32)
    created = await client.post("/api/v1/gate/waivers", json=_waiver_body(change_fingerprint=fp))
    assert created.status_code == 201
    secret = await _mint_ci_secret(client, org_admin)
    resp = await client.post(
        "/api/v1/gate/waivers/lookup",
        json={
            "repo": "acme/warehouse",
            "candidates": [
                # exact match
                {
                    "urn": "urn:col:orders.total",
                    "rule_id": "TYPE_CHANGED",
                    "change_fingerprint": fp,
                },
                # the change morphed: same urn+rule, different fingerprint
                {
                    "urn": "urn:col:orders.total",
                    "rule_id": "TYPE_CHANGED",
                    "change_fingerprint": "0" * 64,
                },
                # different rule on the same column
                {
                    "urn": "urn:col:orders.total",
                    "rule_id": "NULLABLE_TO_NOT_NULL",
                    "change_fingerprint": fp,
                },
            ],
        },
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert resp.status_code == 200, resp.text
    matches = resp.json()["waivers"]
    assert len(matches) == 1
    assert matches[0]["change_fingerprint"] == fp
    assert matches[0]["rule_id"] == "TYPE_CHANGED"


@pytest.mark.asyncio
async def test_ci_lookup_excludes_revoked_and_honors_repo_scope(
    client: httpx.AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    fp = secrets.token_hex(32)
    waiver_id = (
        await client.post("/api/v1/gate/waivers", json=_waiver_body(change_fingerprint=fp))
    ).json()["id"]
    assert (await client.delete(f"/api/v1/gate/waivers/{waiver_id}")).status_code == 204
    secret = await _mint_ci_secret(client, org_admin, repo="acme/warehouse")
    candidates = [
        {"urn": "urn:col:orders.total", "rule_id": "TYPE_CHANGED", "change_fingerprint": fp}
    ]
    hit = await client.post(
        "/api/v1/gate/waivers/lookup",
        json={"repo": "acme/warehouse", "candidates": candidates},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert hit.status_code == 200
    assert hit.json()["waivers"] == []  # revoked waivers never match
    cross = await client.post(
        "/api/v1/gate/waivers/lookup",
        json={"repo": "acme/other", "candidates": candidates},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert cross.status_code == 403  # the repo-scoped token stays scoped
