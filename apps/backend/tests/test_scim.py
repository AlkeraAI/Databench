"""SCIM 2.0 provisioning endpoints — bearer auth, Users CRUD, filter, and
deprovisioning (PATCH active=false → the membership deactivated + its credentials
ended; the identity itself is untouched)."""

from __future__ import annotations

import uuid

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import (
    IdentitySecurityEvent,
    MembershipStatus,
    OrgMembership,
    TeamRole,
    User,
)
from backend.services.identity import sso as sso_service
from backend.services.org import memberships as membership_service
from backend.services.org import teams as team_service
from httpx import AsyncClient
from sqlalchemy import func, select, update
from tests.conftest import OrgWithAdmin, app_client, hold_sso_domains, login

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _sso_feature_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """SCIM provisioning rides on SSO, an Enterprise feature; run this suite on a
    self-hosted deployment (where minting the SCIM token is allowed). The SaaS
    Enterprise gate is covered in test_enterprise_gating.py."""
    monkeypatch.setattr(settings, "self_hosted", True)


SCIM = "/api/v1/scim/v2"

#: Every user this suite provisions lives in this domain — the one the org's SSO
#: connection claims. SCIM refuses to mint or provision outside it.
DOMAIN = "acme.example"


async def _configure_sso(client: AsyncClient, org: OrgWithAdmin, *, domains: str = DOMAIN) -> None:
    """Declare the org's IdP + the domains it speaks for. SCIM provisioning is
    scoped to that list, so it must exist before a SCIM token can be minted."""
    await login(client, org.admin_email, org.admin_password)
    r = await client.put(
        "/api/v1/org/sso",
        json={
            "oidc_issuer": "https://idp.acme.example.com",
            "oidc_client_id": "cid",
            "oidc_client_secret": "s",
            "enabled": True,
        },
    )
    assert r.status_code == 200, r.text
    await hold_sso_domains(org.org_id, domains)


async def _mint_scim_token(client: AsyncClient, org: OrgWithAdmin) -> str:
    await _configure_sso(client, org)
    r = await client.post("/api/v1/org/sso/scim-token")
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _user_by_email(email: str) -> User | None:
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(select(User).where(func.lower(User.email) == email.lower()))
        ).scalar_one_or_none()


async def _membership_active(email: str) -> bool:
    """Whether the person's membership (in the org SCIM provisions) is active.
    The identity is never what SCIM deactivates."""
    async with AsyncSessionLocal() as s:
        user = (
            await s.execute(select(User).where(func.lower(User.email) == email.lower()))
        ).scalar_one()
        assert user.is_active is True, "SCIM never disables the identity"
        membership = (
            await s.execute(select(OrgMembership).where(OrgMembership.user_id == user.id))
        ).scalar_one()
        return membership.status is MembershipStatus.ACTIVE


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


async def test_scim_requires_a_valid_bearer_token(client: AsyncClient) -> None:
    # No token.
    r = await client.get(f"{SCIM}/Users")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/scim+json")
    body = r.json()
    assert body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert body["status"] == "401"
    # A bogus token.
    r = await client.get(f"{SCIM}/Users", headers=_auth("alk_scim_not-a-real-token"))
    assert r.status_code == 401


async def test_revoked_token_stops_working(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    token = await _mint_scim_token(client, org_admin)
    assert (await client.get(f"{SCIM}/Users", headers=_auth(token))).status_code == 200
    assert (await client.delete("/api/v1/org/sso/scim-token")).status_code == 200
    assert (await client.get(f"{SCIM}/Users", headers=_auth(token))).status_code == 401


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


async def test_service_provider_config(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    token = await _mint_scim_token(client, org_admin)
    r = await client.get(f"{SCIM}/ServiceProviderConfig", headers=_auth(token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/scim+json")
    body = r.json()
    assert body["patch"]["supported"] is True
    assert body["filter"]["supported"] is True


# --------------------------------------------------------------------------- #
# Users CRUD + lifecycle
# --------------------------------------------------------------------------- #


async def test_create_list_get_and_deprovision_a_user(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token = await _mint_scim_token(client, org_admin)
    email = f"scim-{uuid.uuid4().hex[:8]}@acme.example"
    body = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": email,
        "externalId": "ext-123",
        "name": {"givenName": "Sam", "familyName": "Carter"},
        "emails": [{"value": email, "primary": True}],
        "active": True,
    }

    # Create → 201 + Location + SCIM User.
    r = await client.post(f"{SCIM}/Users", json=body, headers=_auth(token))
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["userName"] == email
    assert created["active"] is True
    assert created["externalId"] == "ext-123"
    assert r.headers["location"].endswith(f"/Users/{created['id']}")
    user_id = created["id"]

    # Provisioned in the org, as a MEMBER.
    user = await _user_by_email(email)
    assert user is not None and str(user.home_org_team_id) == str(org_admin.org_id)
    async with AsyncSessionLocal() as s:
        m = await membership_service.get(s, team_id=org_admin.org_id, user_id=user.id)
        assert m is not None and m.role is TeamRole.MEMBER

    # Duplicate userName → 409 uniqueness.
    dup = await client.post(f"{SCIM}/Users", json=body, headers=_auth(token))
    assert dup.status_code == 409
    assert dup.json()["scimType"] == "uniqueness"

    # Filter by userName.
    r = await client.get(f'{SCIM}/Users?filter=userName eq "{email}"', headers=_auth(token))
    assert r.status_code == 200
    page = r.json()
    assert page["totalResults"] == 1
    assert page["Resources"][0]["id"] == user_id
    # Filter by externalId too.
    r = await client.get(f'{SCIM}/Users?filter=externalId eq "ext-123"', headers=_auth(token))
    assert r.json()["totalResults"] == 1

    # GET by id.
    r = await client.get(f"{SCIM}/Users/{user_id}", headers=_auth(token))
    assert r.status_code == 200 and r.json()["userName"] == email

    # Deprovision: PATCH active=false → membership deactivated + its credentials ended.
    patch = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "replace", "path": "active", "value": False}],
    }
    r = await client.patch(f"{SCIM}/Users/{user_id}", json=patch, headers=_auth(token))
    assert r.status_code == 200 and r.json()["active"] is False
    assert await _membership_active(email) is False


async def test_unsupported_filter_is_400(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    token = await _mint_scim_token(client, org_admin)
    r = await client.get(f'{SCIM}/Users?filter=userName co "acme"', headers=_auth(token))
    assert r.status_code == 400
    assert r.json()["scimType"] == "invalidFilter"


async def test_get_missing_user_is_scim_404(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    token = await _mint_scim_token(client, org_admin)
    r = await client.get(f"{SCIM}/Users/{uuid.uuid4()}", headers=_auth(token))
    assert r.status_code == 404
    assert r.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    # A non-UUID id is also a clean 404, not a 500.
    assert (await client.get(f"{SCIM}/Users/not-a-uuid", headers=_auth(token))).status_code == 404


async def test_delete_deactivates_the_user(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    token = await _mint_scim_token(client, org_admin)
    email = f"scim-{uuid.uuid4().hex[:8]}@acme.example"
    created = (
        await client.post(
            f"{SCIM}/Users",
            json={"userName": email, "emails": [{"value": email, "primary": True}]},
            headers=_auth(token),
        )
    ).json()
    r = await client.delete(f"{SCIM}/Users/{created['id']}", headers=_auth(token))
    assert r.status_code == 204
    assert await _membership_active(email) is False  # soft-delete (data kept)


async def test_one_orgs_token_cannot_see_another_orgs_users(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    # Provision a user in org A.
    token_a = await _mint_scim_token(client, org_admin)
    email = f"scim-{uuid.uuid4().hex[:8]}@acme.example"
    created = (
        await client.post(
            f"{SCIM}/Users",
            json={"userName": email, "emails": [{"value": email, "primary": True}]},
            headers=_auth(token_a),
        )
    ).json()
    # A SECOND org's token must not be able to read A's user (cross-tenant isolation).
    async with AsyncSessionLocal() as s:
        org_b, _admin_b = await team_service.create_org_with_admin(
            s,
            org_name=f"B-{uuid.uuid4().hex[:8]}",
            admin_email=f"b-{uuid.uuid4().hex[:8]}@b.example",
            admin_first_name="B",
            admin_last_name="Admin",
            admin_password="b-pass-123456",
        )
        _conn_b, raw_b = await sso_service.mint_scim_token(s, org_b.id)
        await s.commit()
    r = await client.get(f"{SCIM}/Users/{created['id']}", headers=_auth(raw_b))
    assert r.status_code == 404  # B's token can't reach A's user


async def test_scim_cannot_deactivate_the_last_org_admin(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token = await _mint_scim_token(client, org_admin)
    # Find the org's sole admin (the bootstrap/fixture admin) via SCIM.
    page = (
        await client.get(
            f'{SCIM}/Users?filter=userName eq "{org_admin.admin_email}"', headers=_auth(token)
        )
    ).json()
    assert page["totalResults"] == 1
    admin_id = page["Resources"][0]["id"]
    # An IdP must NOT be able to lock the org out by deactivating its last admin.
    r = await client.patch(
        f"{SCIM}/Users/{admin_id}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": False}],
        },
        headers=_auth(token),
    )
    assert r.status_code == 400 and r.json()["scimType"] == "mutability"
    # DELETE (also a deactivation) is refused for the same reason.
    assert (
        await client.delete(f"{SCIM}/Users/{admin_id}", headers=_auth(token))
    ).status_code == 400
    assert await _membership_active(org_admin.admin_email) is True  # still active


async def test_scim_rejects_duplicate_external_id(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token = await _mint_scim_token(client, org_admin)

    def _body(email: str, ext: str) -> dict:
        return {"userName": email, "externalId": ext, "emails": [{"value": email, "primary": True}]}

    a_email = f"a-{uuid.uuid4().hex[:8]}@acme.example"
    b_email = f"b-{uuid.uuid4().hex[:8]}@acme.example"
    a = await client.post(f"{SCIM}/Users", json=_body(a_email, "shared-ext"), headers=_auth(token))
    assert a.status_code == 201
    # A different user with the SAME externalId → 409 (dedup contract).
    dup = await client.post(
        f"{SCIM}/Users", json=_body(b_email, "shared-ext"), headers=_auth(token)
    )
    assert dup.status_code == 409 and dup.json()["scimType"] == "uniqueness"

    # And PUT can't move one user's externalId onto another's.
    b = await client.post(f"{SCIM}/Users", json=_body(b_email, "ext-b"), headers=_auth(token))
    assert b.status_code == 201
    collide = await client.put(
        f"{SCIM}/Users/{b.json()['id']}",
        json={"userName": b_email, "externalId": "shared-ext", "emails": [{"value": b_email}]},
        headers=_auth(token),
    )
    assert collide.status_code == 409 and collide.json()["scimType"] == "uniqueness"


async def test_scim_username_is_immutable(client: AsyncClient, org_admin: OrgWithAdmin) -> None:
    token = await _mint_scim_token(client, org_admin)
    email = f"u-{uuid.uuid4().hex[:8]}@acme.example"
    created = (
        await client.post(
            f"{SCIM}/Users",
            json={"userName": email, "emails": [{"value": email, "primary": True}]},
            headers=_auth(token),
        )
    ).json()
    # A PUT that tries to RENAME (different userName) is refused...
    r = await client.put(
        f"{SCIM}/Users/{created['id']}",
        json={"userName": f"renamed-{email}", "emails": [{"value": email}]},
        headers=_auth(token),
    )
    assert r.status_code == 400 and r.json()["scimType"] == "mutability"
    # ...but resending the SAME userName (a normal re-sync) is fine.
    ok = await client.put(
        f"{SCIM}/Users/{created['id']}",
        json={"userName": email, "name": {"givenName": "Renamed"}, "emails": [{"value": email}]},
        headers=_auth(token),
    )
    # Names an IdP pushes are the org's name for the person, never the identity's.
    assert ok.status_code == 200 and ok.json()["displayName"] == "Renamed"


# --------------------------------------------------------------------------- #
# The authority boundary: a token provisions only inside its own allowed domains
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "email",
    [
        pytest.param("ceo@victim.example", id="another-companys-domain"),
        pytest.param("ceo@notacme.example", id="substring-lookalike"),
        pytest.param("ceo@acme.example.evil.test", id="suffix-lookalike"),
        pytest.param("ceo@sub.acme.example", id="subdomain-not-listed"),
        pytest.param("ceo@gmail.com", id="free-mail"),
    ],
)
async def test_scim_cannot_provision_outside_the_connections_allowed_domains(
    client: AsyncClient, org_admin: OrgWithAdmin, email: str
) -> None:
    """`allowed_domains` is what an org's IdP is authoritative for — the SSO login
    path already refuses a profile outside it. Provisioning is the same authority
    through the same connection, so a token must not be able to plant a row at
    another company's address (which would block the real owner's signup and
    silently absorb them into this org when they later sign in)."""
    token = await _mint_scim_token(client, org_admin)
    r = await client.post(
        f"{SCIM}/Users",
        json={"userName": email, "emails": [{"value": email, "primary": True}]},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.text
    assert r.json()["scimType"] == "invalidValue"
    assert await _user_by_email(email) is None  # nothing planted

    # The emails[] array is the other way in — same refusal.
    other = await client.post(
        f"{SCIM}/Users",
        json={
            "userName": f"ok-{uuid.uuid4().hex[:8]}@{DOMAIN}",
            "emails": [{"value": email, "primary": True}],
        },
        headers=_auth(token),
    )
    assert other.status_code == 400
    assert await _user_by_email(email) is None


async def test_an_allowed_domain_is_matched_case_insensitively(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The asymmetric case: the guard binds the domain, not the spelling."""
    token = await _mint_scim_token(client, org_admin)
    email = f"Mixed-{uuid.uuid4().hex[:8]}@ACME.Example"
    r = await client.post(
        f"{SCIM}/Users",
        json={"userName": email, "emails": [{"value": email, "primary": True}]},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text


async def test_a_token_minted_before_the_domains_existed_still_provisions_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """Fail closed on DATA, not only on input: a token minted the old way (no
    connection, no domains) is refused at use, not honored."""
    async with AsyncSessionLocal() as s:
        _conn, raw = await sso_service.mint_scim_token(s, org_admin.org_id)
        await s.commit()
    email = f"x-{uuid.uuid4().hex[:8]}@{DOMAIN}"
    r = await client.post(
        f"{SCIM}/Users",
        json={"userName": email, "emails": [{"value": email, "primary": True}]},
        headers=_auth(raw),
    )
    assert r.status_code == 400
    assert await _user_by_email(email) is None


# --------------------------------------------------------------------------- #
# Deprovisioning must never be answered with a silent 200
# --------------------------------------------------------------------------- #

_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


async def _provision(client: AsyncClient, token: str) -> tuple[str, str]:
    email = f"dep-{uuid.uuid4().hex[:8]}@{DOMAIN}"
    r = await client.post(
        f"{SCIM}/Users",
        json={"userName": email, "emails": [{"value": email, "primary": True}]},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"], email


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {
                "schemas": [_PATCH_SCHEMA],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
            id="canonical-boolean",
        ),
        # Microsoft Entra ID sends the value as a JSON STRING.
        pytest.param(
            {"Operations": [{"op": "Replace", "path": "active", "value": "False"}]},
            id="entra-string-value",
        ),
        pytest.param(
            {
                "Operations": [
                    {
                        "op": "replace",
                        "path": "urn:ietf:params:scim:schemas:core:2.0:User:active",
                        "value": False,
                    }
                ]
            },
            id="urn-qualified-path",
        ),
        pytest.param({"Operations": [{"op": "remove", "path": "active"}]}, id="remove-op"),
        # RFC 7643 §2.1: SCIM attribute names are case-insensitive.
        pytest.param(
            {"operations": [{"op": "replace", "path": "active", "value": False}]},
            id="lowercase-operations-key",
        ),
        pytest.param(
            {"Operations": [{"op": "replace", "path": "ACTIVE", "value": 0}]},
            id="uppercase-path-int-value",
        ),
        pytest.param(
            {"Operations": [{"op": "replace", "value": {"active": "false"}}]},
            id="pathless-object-string-value",
        ),
        pytest.param(
            {"Operations": [{"op": "add", "path": "active", "value": [{"value": False}]}]},
            id="multi-valued-form",
        ),
    ],
)
async def test_every_deprovisioning_shape_actually_deactivates(
    client: AsyncClient, org_admin: OrgWithAdmin, body: dict
) -> None:
    """An IdP that says "this person is gone" gets a user who IS gone. Before this,
    four RFC-legal / real-IdP shapes fell through the handler and returned 200 with
    the account still live — and no session revoked."""
    token = await _mint_scim_token(client, org_admin)
    user_id, email = await _provision(client, token)
    r = await client.patch(f"{SCIM}/Users/{user_id}", json=body, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False
    assert await _membership_active(email) is False


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {"Operations": [{"op": "replace", "path": "active", "value": "maybe"}]},
            id="unreadable-value",
        ),
        pytest.param(
            {"Operations": [{"op": "replace", "path": "active", "value": None}]},
            id="null-value",
        ),
        pytest.param({"Operations": [{"op": "delete", "path": "active"}]}, id="unsupported-op"),
        pytest.param({"Operations": [{"op": "replace"}]}, id="pathless-without-object"),
        pytest.param({"Operations": "replace-everything"}, id="operations-not-an-array"),
        pytest.param({"schemas": [_PATCH_SCHEMA]}, id="no-operations-at-all"),
        pytest.param({"Operations": ["active=false"]}, id="operation-not-an-object"),
    ],
)
async def test_an_uninterpretable_patch_is_a_400_never_a_silent_200(
    client: AsyncClient, org_admin: OrgWithAdmin, body: dict
) -> None:
    token = await _mint_scim_token(client, org_admin)
    user_id, email = await _provision(client, token)
    r = await client.patch(f"{SCIM}/Users/{user_id}", json=body, headers=_auth(token))
    assert r.status_code == 400, r.text
    assert await _membership_active(email) is True  # unchanged, and the IdP knows


async def test_an_unmodelled_attribute_sync_is_still_tolerated(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The asymmetry that keeps this usable: attributes this resource doesn't model
    (displayName, department, …) are ignored, not rejected — only `active` and a
    malformed envelope are hard failures."""
    token = await _mint_scim_token(client, org_admin)
    user_id, email = await _provision(client, token)
    r = await client.patch(
        f"{SCIM}/Users/{user_id}",
        json={
            "Operations": [
                {"op": "replace", "path": "displayName", "value": "Sam Carter"},
                {"op": "replace", "path": "name.givenName", "value": "Sam"},
            ]
        },
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["displayName"] == "Sam"
    assert await _membership_active(email) is True


async def test_reactivation_reads_the_same_tolerant_shapes(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    token = await _mint_scim_token(client, org_admin)
    user_id, email = await _provision(client, token)
    await client.patch(
        f"{SCIM}/Users/{user_id}",
        json={"Operations": [{"op": "replace", "path": "active", "value": "false"}]},
        headers=_auth(token),
    )
    r = await client.patch(
        f"{SCIM}/Users/{user_id}",
        json={"Operations": [{"op": "replace", "path": "active", "value": "True"}]},
        headers=_auth(token),
    )
    assert r.status_code == 200 and r.json()["active"] is True
    assert await _membership_active(email) is True


async def test_put_reads_a_string_active_and_refuses_an_unreadable_one(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """PUT carries the same `active` gate as PATCH — Entra sends the string form
    on both."""
    token = await _mint_scim_token(client, org_admin)
    user_id, email = await _provision(client, token)
    ok = await client.put(
        f"{SCIM}/Users/{user_id}",
        json={"userName": email, "emails": [{"value": email}], "active": "False"},
        headers=_auth(token),
    )
    assert ok.status_code == 200 and ok.json()["active"] is False

    bad = await client.put(
        f"{SCIM}/Users/{user_id}",
        json={"userName": email, "emails": [{"value": email}], "active": "banana"},
        headers=_auth(token),
    )
    assert bad.status_code == 400 and bad.json()["scimType"] == "invalidValue"


# --------------------------------------------------------------------------- #
# The credential cannot outlive the entitlement that authorized it
# --------------------------------------------------------------------------- #


async def _offboard_the_old_way(user_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """What the upgrade leaves for a person an org offboarded before
    deactivation moved onto the membership: the identity inactive and its home
    membership deactivated, at a moved epoch."""
    async with AsyncSessionLocal() as s:
        await s.execute(update(User).where(User.id == user_id).values(is_active=False))
        await s.execute(
            update(OrgMembership)
            .where(OrgMembership.user_id == user_id, OrgMembership.org_team_id == org_id)
            .values(
                status=MembershipStatus.DEACTIVATED,
                credential_epoch=OrgMembership.credential_epoch + 1,
            )
        )
        await s.commit()


async def _fresh_cli_token(user_id: uuid.UUID, org_id: uuid.UUID) -> str:
    from alkera_core.auth import register_token
    from alkera_core.models import TokenType
    from backend.auth.membership_tokens import mint_for_membership

    async with AsyncSessionLocal() as s:
        user = await s.get(User, user_id)
        assert user is not None
        token, claims = await mint_for_membership(s, user, org_id, kind="cli")
        await register_token(s, claims=claims, token_type=TokenType.CLI)
        await s.commit()
    return token


@pytest.mark.parametrize(
    ("platform_disabled", "can_act"),
    [
        pytest.param(False, True, id="org-offboarding-is-lifted"),
        pytest.param(True, False, id="platform-disable-stands"),
    ],
)
async def test_scim_reactivating_a_person_offboarded_before_this_release(
    client: AsyncClient, org_admin: OrgWithAdmin, platform_disabled: bool, can_act: bool
) -> None:
    token = await _mint_scim_token(client, org_admin)
    email = f"scim-{uuid.uuid4().hex[:8]}@{DOMAIN}"
    created = (
        await client.post(
            f"{SCIM}/Users",
            json={"userName": email, "emails": [{"value": email, "primary": True}]},
            headers=_auth(token),
        )
    ).json()
    user_id = uuid.UUID(created["id"])
    await _offboard_the_old_way(user_id, org_admin.org_id)
    if platform_disabled:
        async with AsyncSessionLocal() as s:
            s.add(IdentitySecurityEvent(user_id=user_id, event="platform.user_disabled"))
            await s.commit()

    patch = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "replace", "path": "active", "value": True}],
    }
    r = await client.patch(f"{SCIM}/Users/{user_id}", json=patch, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True

    cli = await _fresh_cli_token(user_id, org_admin.org_id)
    async with app_client() as person:
        me = await person.get("/api/v1/auth/me", headers=_auth(cli))
    assert (me.status_code == 200) is can_act, me.text
    user = await _user_by_email(email)
    assert user is not None and user.is_active is can_act
