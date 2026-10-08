"""Every write path that can put a name into an outbound email refuses a link.

The external assessment set a TEAM name to phishing prose with an attacker URL
and let the product mail it to a prospect from our own domain; a separate report
did the same through the ORG name at signup. Both are one class of bug with many
doors, so this suite walks the doors: signup, complete-profile, team create, team
rename, profile edit, and admin org bootstrap.
"""

from __future__ import annotations

import secrets

import pytest
from httpx import AsyncClient

# The literal payloads from the reports. A 422 (schema) or 400 is fine; a 2xx is
# the bug.
PHISH_URL = (
    "Security Compliance Portal (Please verify your account at "
    "https://malicious.evil/login before accepting)"
)
ORG_URL = "http://attacker.com/"
SCHEME = "strawberry://settings/team"
MARKUP = "Team <h1>HTML_tags</h1>"

PAYLOADS = [
    pytest.param(PHISH_URL, id="invitation-phish"),
    pytest.param(ORG_URL, id="org-name-url"),
    pytest.param(SCHEME, id="custom-scheme"),
    pytest.param(MARKUP, id="markup"),
]


def _email(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}@alkera.dev"


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text


def _rejected(resp) -> bool:  # type: ignore[no-untyped-def]
    return resp.status_code in (400, 422)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", PAYLOADS)
async def test_signup_refuses_a_link_bearing_org_name(
    client: AsyncClient, payload: str, monkeypatch_verification_send: list[dict]
) -> None:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": _email("signup"),
            "password": "correct-horse-battery-staple",
            "org_name": payload,
        },
    )
    assert _rejected(resp), resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", PAYLOADS)
async def test_signup_refuses_a_link_bearing_person_name(
    client: AsyncClient, payload: str, monkeypatch_verification_send: list[dict]
) -> None:
    """The inviter's display name is rendered beside the org name in the same
    invitation, so it is the same vector."""
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": _email("signup"),
            "password": "correct-horse-battery-staple",
            "first_name": payload,
            "last_name": "Smith",
            "org_name": "Acme",
        },
    )
    assert _rejected(resp), resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", PAYLOADS)
async def test_team_create_refuses_a_link_bearing_name(
    client: AsyncClient, org_admin, payload: str
) -> None:
    await _login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post(
        "/api/v1/teams", json={"name": payload, "parent_team_id": str(org_admin.org_id)}
    )
    assert _rejected(resp), resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", PAYLOADS)
async def test_team_rename_refuses_a_link_bearing_name(
    client: AsyncClient, org_admin, payload: str
) -> None:
    """The reported flow exactly: a clean team is created, THEN renamed to the
    payload — so validating only on create would leave the hole wide open."""
    await _login(client, org_admin.admin_email, org_admin.admin_password)
    created = await client.post(
        "/api/v1/teams",
        json={"name": f"Clean {secrets.token_hex(3)}", "parent_team_id": str(org_admin.org_id)},
    )
    assert created.status_code == 201, created.text
    team_id = created.json()["id"]

    resp = await client.patch(f"/api/v1/teams/{team_id}", json={"name": payload})
    assert _rejected(resp), resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", PAYLOADS)
async def test_profile_edit_refuses_a_link_bearing_name(
    client: AsyncClient, org_admin, payload: str
) -> None:
    await _login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.patch(f"/api/v1/users/{org_admin.admin_id}", json={"first_name": payload})
    assert _rejected(resp), resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", PAYLOADS)
async def test_admin_org_bootstrap_refuses_a_link_bearing_org_name(
    client: AsyncClient, platform_admin, payload: str
) -> None:
    await _login(client, platform_admin.admin_email, platform_admin.admin_password)
    resp = await client.post(
        "/admin/v1/orgs",
        json={
            "name": payload,
            "admin_email": _email("neworg"),
            "admin_first_name": "New",
            "admin_last_name": "Admin",
            "admin_password": "correct-horse-battery-staple",
        },
    )
    assert _rejected(resp), resp.text


@pytest.mark.asyncio
async def test_a_clean_team_name_still_works(client: AsyncClient, org_admin) -> None:
    """The other half of the policy: it must not break ordinary names. `Acme.io`
    is the deliberate residual — a bare dotted token is what most startups are
    called, so it is accepted knowingly."""
    await _login(client, org_admin.admin_email, org_admin.admin_password)
    for name in (f"Acme.io {secrets.token_hex(3)}", f"R&D (EMEA) {secrets.token_hex(3)}"):
        resp = await client.post(
            "/api/v1/teams", json={"name": name, "parent_team_id": str(org_admin.org_id)}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["name"] == name


@pytest.mark.asyncio
async def test_a_name_is_stored_normalized(client: AsyncClient, org_admin) -> None:
    """Whitespace and invisible formatting characters are settled at the door, so
    what is persisted is exactly what an email will render."""
    await _login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.post(
        "/api/v1/teams",
        json={
            "name": f"  Data​   Platform {secrets.token_hex(3)}  ",
            "parent_team_id": str(org_admin.org_id),
        },
    )
    assert resp.status_code == 201, resp.text
    stored = resp.json()["name"]
    assert stored.startswith("Data Platform ")
    assert "​" not in stored
    assert "  " not in stored
