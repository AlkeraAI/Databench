"""Who may sign up and found an org.

On a self-hosted deployment every org runs on the operator's provider keys and
reaches the operator's network (SSH machines, data connections), so a stranger
who could sign up on a public install would get both. Self-hosted is therefore
invite-only unless the operator opens it; an invited person still signs up.
"""

from __future__ import annotations

import secrets

import pytest
from alkera_core.config import Settings, settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import OrgWithAdmin, login


def _stranger() -> dict[str, str]:
    suffix = secrets.token_hex(4)
    return {
        "email": f"stranger-{suffix}@alkera.dev",
        "first_name": "Stranger",
        "last_name": "Danger",
        "password": "a-good-password-1",
        "org_name": f"Stranger Co {suffix}",
    }


async def _user_exists(email: str) -> bool:
    async with AsyncSessionLocal() as db:
        found = await db.execute(select(User.id).where(User.email == email))
        return found.scalar_one_or_none() is not None


@pytest.mark.parametrize(
    ("self_hosted", "mode", "open_"),
    [
        pytest.param(True, None, False, id="self-hosted-defaults-to-invite-only"),
        pytest.param(True, "open", True, id="self-hosted-operator-opens-it"),
        pytest.param(False, None, True, id="hosted-defaults-to-open"),
        pytest.param(False, "invite_only", False, id="hosted-operator-closes-it"),
        pytest.param(None, None, True, id="inferred-deployment-stays-open"),
    ],
)
def test_the_mode_resolves_from_the_deployment(
    self_hosted: bool | None, mode: str | None, open_: bool
) -> None:
    resolved = Settings(  # type: ignore[call-arg]
        _env_file=None, app_env="local", self_hosted=self_hosted, signup_mode=mode
    )
    assert resolved.public_signup_open is open_


@pytest.mark.asyncio
async def test_a_stranger_cannot_sign_up_on_an_invite_only_install(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, monkeypatch_verification_send
) -> None:
    monkeypatch.setattr(settings, "self_hosted", True)
    monkeypatch.setattr(settings, "signup_mode", None)
    body = _stranger()
    resp = await client.post("/api/v1/auth/signup", json=body)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "signup_closed"
    assert not await _user_exists(body["email"])
    assert monkeypatch_verification_send == []


@pytest.mark.asyncio
async def test_a_stranger_signs_up_when_the_operator_opens_it(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    monkeypatch_verification_send,
    monkeypatch_welcome_send,
) -> None:
    monkeypatch.setattr(settings, "self_hosted", True)
    monkeypatch.setattr(settings, "signup_mode", "open")
    body = _stranger()
    resp = await client.post("/api/v1/auth/signup", json=body)
    assert resp.status_code == 201, resp.text
    assert await _user_exists(body["email"])


@pytest.mark.asyncio
async def test_an_invited_person_still_signs_up_on_an_invite_only_install(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    monkeypatch_email_send,
) -> None:
    monkeypatch.setattr(settings, "self_hosted", True)
    monkeypatch.setattr(settings, "signup_mode", None)
    await login(client, org_admin.admin_email, org_admin.admin_password)
    team = await client.post("/api/v1/teams", json={"name": f"Invited {secrets.token_hex(3)}"})
    assert team.status_code == 201, team.text
    email = f"invitee-{secrets.token_hex(4)}@alkera.dev"
    invite = await client.post(
        f"/api/v1/teams/{team.json()['id']}/invitations",
        json={"email": email, "role": "member"},
    )
    assert invite.status_code == 201, invite.text
    token = monkeypatch_email_send[-1]["invitation_token"]
    await client.post("/api/v1/auth/logout")

    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "a-good-password-1", "invite_token": token},
    )
    assert resp.status_code == 201, resp.text
