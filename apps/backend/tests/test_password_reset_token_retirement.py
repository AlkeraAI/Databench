"""An outstanding password-reset link dies with the credential it was minted for.

A reset link stays spendable for its whole TTL, and spending it does two things
at once: it sets a password the account holder did not choose, and it revokes
every session — including the one they just established by changing their own
credentials. So a link that is still live after a self-service password or email
change is a delayed takeover for anyone who later reads that mailbox: a shared or
monitored inbox, a forwarding rule left by a departed colleague, a mail backup.

`password_reset_service.consume_token` and `user_service.clear_password` already
retire the pending token. These pin that the third writer of a credential —
`update_profile`, behind `PATCH /api/v1/users/{id}` — does too.
"""

from __future__ import annotations

import uuid

import pytest
from backend.services.identity import password_reset as password_reset_service
from backend.services.identity import users as user_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin, login, make_member

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "change",
    [
        pytest.param("password", id="password-change"),
        pytest.param("email", id="email-change"),
    ],
)
async def test_a_credential_change_retires_the_pending_reset_token(
    real_session: AsyncSession, org_admin: OrgWithAdmin, change: str
) -> None:
    """Both credential-grade edits retire it. The email case matters as much as
    the password one: the link was minted against the OLD address, and the
    account has since moved away from it."""
    member, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    raw_token = await password_reset_service.issue_token(real_session, member)
    await real_session.commit()
    assert member.password_reset_token is not None

    if change == "password":
        await user_service.update_profile(
            real_session, member, password=f"retired-link-{uuid.uuid4().hex[:8]}"
        )
    else:
        await user_service.update_profile(
            real_session, member, email=f"moved-{uuid.uuid4().hex[:8]}@acme-reset.example.com"
        )
    await real_session.commit()

    assert member.password_reset_token is None
    assert member.password_reset_expires_at is None
    # And the raw link no longer resolves to anyone at all.
    assert await password_reset_service.get_by_token(real_session, raw_token) is None


async def test_a_name_only_edit_leaves_the_pending_link_alone(
    real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """The asymmetry that keeps the retirement honest: renaming yourself is not a
    credential change, so a reset link you asked for a moment ago must survive it
    — otherwise finishing your profile silently breaks the email in your inbox."""
    member, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    raw_token = await password_reset_service.issue_token(real_session, member)
    await real_session.commit()

    await user_service.update_profile(real_session, member, first_name="Renamed")
    await real_session.commit()

    found = await password_reset_service.get_by_token(real_session, raw_token)
    assert found is not None
    assert found.id == member.id


async def test_the_emailed_link_is_dead_after_changing_the_password_in_the_ui(
    client: AsyncClient, real_session: AsyncSession, org_admin: OrgWithAdmin
) -> None:
    """End to end, as the victim experiences it: they request a reset link, decide
    to change the password in the profile UI instead, and the link that is still
    sitting in their mailbox can no longer be spent against their account."""
    member, member_password = await make_member(
        real_session, org_id=org_admin.org_id, verified=True
    )
    assert member_password is not None
    raw_token = await password_reset_service.issue_token(real_session, member)
    await real_session.commit()

    await login(client, member.email, member_password)
    patched = await client.patch(
        f"/api/v1/users/{member.id}",
        json={
            "password": f"chosen-in-the-ui-{uuid.uuid4().hex[:8]}",
            "current_password": member_password,
        },
    )
    assert patched.status_code == 200, patched.text

    spent = await client.post(
        f"/api/v1/auth/password-reset/{raw_token}",
        json={"password": f"attacker-picked-{uuid.uuid4().hex[:8]}"},
    )
    assert spent.status_code == 400, spent.text
