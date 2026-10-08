"""Password policy at every route a user can set a credential through.

Reported: an account set its password to its own registered email address. The
policy lives in `backend.auth.password_policy` and is enforced at the ROUTE layer
— so the coverage that matters is per-door, not per-function: signup, password
reset, self profile edit, admin member create, admin org bootstrap.
"""

from __future__ import annotations

import secrets

import pytest
from httpx import AsyncClient

GOOD = "correct-horse-battery-staple"


def _email(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}@alkera.dev"


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text


def _weak(resp) -> bool:  # type: ignore[no-untyped-def]
    """A policy rejection: the stable `weak_password` code, or a schema 422 for a
    value the field's own min_length already refuses."""
    if resp.status_code == 422:
        return True
    return resp.status_code == 400 and resp.json().get("error", {}).get("code") == "weak_password"


@pytest.mark.asyncio
async def test_signup_refuses_a_password_equal_to_the_email(
    client: AsyncClient, monkeypatch_verification_send: list[dict]
) -> None:
    """The reported bug, at the door it was reported through."""
    email = _email("selfpw")
    resp = await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": email, "org_name": "Acme"}
    )
    assert _weak(resp), resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "password",
    [
        pytest.param("short", id="too-short"),
        pytest.param("password1234", id="common-word-padded"),
        pytest.param("Welcome2024!", id="common-word-with-year-and-punctuation"),
        pytest.param("aaaaaaaaaaaa", id="single-repeated-char"),
        pytest.param("123456789012", id="digit-run"),
    ],
)
async def test_signup_refuses_weak_passwords(
    client: AsyncClient, password: str, monkeypatch_verification_send: list[dict]
) -> None:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": _email("weak"), "password": password, "org_name": "Acme"},
    )
    assert _weak(resp), resp.text


@pytest.mark.asyncio
async def test_signup_accepts_a_reasonable_password(
    client: AsyncClient, monkeypatch_verification_send: list[dict]
) -> None:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": _email("ok"), "password": GOOD, "org_name": "Acme"},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_signup_refuses_a_password_containing_the_supplied_name(
    client: AsyncClient, monkeypatch_verification_send: list[dict]
) -> None:
    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": _email("named"),
            "password": "bartholomew-secret-9",
            "first_name": "Bartholomew",
            "last_name": "Smith",
            "org_name": "Acme",
        },
    )
    assert _weak(resp), resp.text


@pytest.mark.asyncio
async def test_password_reset_refuses_a_password_equal_to_the_email(
    client: AsyncClient, org_admin, monkeypatch_password_reset_send: list[dict]
) -> None:
    """The reset link is the OTHER way to set a credential, and it is the one an
    account-recovery flow funnels users through — the identity check has to reach
    it even though the request body carries no email."""
    resp = await client.post(
        "/api/v1/auth/password-reset/request", json={"email": org_admin.admin_email}
    )
    assert resp.status_code == 200, resp.text
    token = monkeypatch_password_reset_send[-1]["token"]

    resp = await client.post(
        f"/api/v1/auth/password-reset/{token}", json={"password": org_admin.admin_email}
    )
    assert _weak(resp), resp.text


@pytest.mark.asyncio
async def test_a_refused_reset_leaves_the_token_spendable(
    client: AsyncClient, org_admin, monkeypatch_password_reset_send: list[dict]
) -> None:
    """A weak choice must not burn the user's one recovery link — they simply try
    a different password."""
    await client.post("/api/v1/auth/password-reset/request", json={"email": org_admin.admin_email})
    token = monkeypatch_password_reset_send[-1]["token"]

    refused = await client.post(
        f"/api/v1/auth/password-reset/{token}", json={"password": org_admin.admin_email}
    )
    assert _weak(refused), refused.text

    accepted = await client.post(f"/api/v1/auth/password-reset/{token}", json={"password": GOOD})
    assert accepted.status_code == 200, accepted.text


@pytest.mark.asyncio
async def test_invalid_token_is_refused_before_the_policy_runs(client: AsyncClient) -> None:
    """Ordering matters: the policy needs the account's identity, so it runs
    AFTER the token resolves. A caller holding no valid token must learn nothing
    about any address — including whether a password would have been accepted
    for it."""
    resp = await client.post(
        "/api/v1/auth/password-reset/not-a-real-token", json={"password": GOOD}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] != "weak_password"


@pytest.mark.asyncio
async def test_self_profile_edit_refuses_a_password_equal_to_the_email(
    client: AsyncClient, org_admin
) -> None:
    await _login(client, org_admin.admin_email, org_admin.admin_password)
    resp = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={
            "password": org_admin.admin_email,
            "current_password": org_admin.admin_password,
        },
    )
    assert _weak(resp), resp.text


@pytest.mark.asyncio
async def test_profile_edit_checks_against_the_new_email(client: AsyncClient, org_admin) -> None:
    """A request that changes address and password together must be judged
    against the identity the credential will protect AFTER the edit — otherwise
    "set my email to X and my password to X" walks straight through."""
    await _login(client, org_admin.admin_email, org_admin.admin_password)
    new_email = _email("moved")
    resp = await client.patch(
        f"/api/v1/users/{org_admin.admin_id}",
        json={
            "email": new_email,
            "password": new_email,
            "current_password": org_admin.admin_password,
        },
    )
    assert _weak(resp), resp.text


@pytest.mark.asyncio
async def test_the_deprecated_member_create_never_stores_the_admins_password(
    client: AsyncClient, org_admin, monkeypatch_password_reset_send: list[dict]
) -> None:
    """The policy question no longer arises: an admin's password, weak or
    strong, is never set on the account; the person chooses their own."""
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.models import User
    from sqlalchemy import select

    await _login(client, org_admin.admin_email, org_admin.admin_password)
    email = _email("member")
    resp = await client.post(
        "/api/v1/users",
        json={
            "org_team_id": str(org_admin.org_id),
            "email": email,
            "first_name": "New",
            "last_name": "Member",
            "password": email,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["notes"] == ["password_ignored"]
    async with AsyncSessionLocal() as s:
        created = await s.scalar(select(User).where(User.email == email))
    assert created is not None and created.password_hash is None


@pytest.mark.asyncio
async def test_admin_org_bootstrap_refuses_a_weak_password(
    client: AsyncClient, platform_admin
) -> None:
    await _login(client, platform_admin.admin_email, platform_admin.admin_password)
    email = _email("bootstrap")
    resp = await client.post(
        "/admin/v1/orgs",
        json={
            "name": f"Bootstrapped {secrets.token_hex(3)}",
            "admin_email": email,
            "admin_first_name": "New",
            "admin_last_name": "Admin",
            "admin_password": email,
        },
    )
    assert _weak(resp), resp.text


def _password_schemas() -> dict[str, dict[str, object]]:
    """Every request schema in the published contract that takes a password a
    person chooses, keyed by schema and field name."""
    from backend.app_factory import process_app

    app = process_app()

    found: dict[str, dict[str, object]] = {}
    for name, schema in app.openapi()["components"]["schemas"].items():
        for field in ("password", "admin_password"):
            prop = schema.get("properties", {}).get(field)
            if prop is None:
                continue
            # A nullable field nests the string shape under anyOf.
            shapes = [prop, *prop.get("anyOf", [])]
            string = next(shape for shape in shapes if shape.get("type") == "string")
            found[f"{name}.{field}"] = string
    return found


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param("SignupRequest.password", id="signup"),
        pytest.param("PasswordResetConfirm.password", id="password-reset"),
    ],
)
def test_the_published_password_length_is_the_one_the_server_enforces(schema: str) -> None:
    """A client built from the schema must not offer a password the server
    refuses: the schema said 8 while the policy refused anything under 12."""
    from alkera_core.config import settings

    assert _password_schemas()[schema]["minLength"] == settings.auth_password_min_length


@pytest.mark.asyncio
async def test_signup_refuses_a_password_one_below_the_policy(client: AsyncClient) -> None:
    """The other half of the contract: the length the schema publishes is the
    one the route holds, with the policy's own explanation."""
    from alkera_core.config import settings

    resp = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": _email("brief"),
            "password": "q7#Lm2@xZ9!vK4$w"[: settings.auth_password_min_length - 1],
            "org_name": "Brief Org",
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "weak_password"
