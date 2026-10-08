"""user_service domain helpers."""

from __future__ import annotations

import pytest
from backend.services.identity import users as user_service
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import OrgWithAdmin


@pytest.mark.asyncio
async def test_get_by_email_normalizes_case_and_whitespace(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """Lookups normalize identically to storage (lower + strip), so a presented
    address with stray case/whitespace still resolves the row. Without the
    `.strip()`, the surrounding spaces below would miss the stored email."""
    messy = f"  {org_admin.admin_email.upper()}  "
    found = await user_service.get_by_email(real_session, messy)
    assert found is not None
    assert found.id == org_admin.admin_id


# --------------------------------------------------------------------------- #
# `User.email` write chokepoint + the credential-change security event
#
# These live at the SERVICE, not the route: `update_profile` is the second write
# path to `User.email` (the first is `create_user`), and any future third caller
# inherits whatever is enforced here. Testing only through the route would leave
# the invariant hostage to one handler.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reserved",
    [
        pytest.param("m@svc.alkera.proxy", id="rename-target"),
        pytest.param("M@SVC.ALKERA.PROXY", id="uppercase"),
    ],
)
async def test_update_profile_refuses_the_reserved_machine_domain(
    real_session: AsyncSession, org_admin: OrgWithAdmin, reserved: str
):
    """`create_user` has always refused this domain; `update_profile` is the other
    write path to `User.email` and must refuse it identically. A human holding one
    is read by the model gateway as the org's machine principal — trusted to
    forward its own usage meter, and funded under the pool rule that drops the
    per-user postpaid cap."""
    user = await user_service.get_by_id(real_session, org_admin.admin_id)
    assert user is not None
    with pytest.raises(user_service.UserConflictError):
        await user_service.update_profile(real_session, user, email=reserved)
    await real_session.rollback()


@pytest.mark.asyncio
async def test_update_profile_refuses_it_on_creation_too(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """The same chokepoint, still enforced on the creation path."""
    with pytest.raises(user_service.UserConflictError):
        await user_service.create_user(
            real_session,
            org_team_id=org_admin.org_id,
            email="fresh@svc.alkera.proxy",
            first_name="F",
            last_name="M",
            password="whatever-pass-1",
        )
    await real_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "bumps_epoch"),
    [
        pytest.param("password", True, id="password-change"),
        pytest.param("email", True, id="email-change"),
        pytest.param("first_name", False, id="name-change"),
    ],
)
async def test_only_a_credential_change_bumps_the_revocation_epoch(
    real_session: AsyncSession, org_admin: OrgWithAdmin, field: str, bumps_epoch: bool
):
    """`token_epoch` is the revoke-all lever every live token is checked against.
    A credential change must move it (so an intruder's session dies with the
    credential they stole), and a cosmetic edit must not (so a name fix doesn't
    sign the user out of every device)."""
    import secrets

    user = await user_service.get_by_id(real_session, org_admin.admin_id)
    assert user is not None
    before = user.token_epoch

    values = {
        "password": "service-level-pass-1",
        "email": f"svc-{secrets.token_hex(6)}@alkera.dev",
        "first_name": "Renamed",
    }
    await user_service.update_profile(real_session, user, **{field: values[field]})
    await real_session.commit()
    await real_session.refresh(user)
    assert (user.token_epoch > before) is bumps_epoch


@pytest.mark.asyncio
async def test_re_sending_the_same_email_is_not_a_credential_change(
    real_session: AsyncSession, org_admin: OrgWithAdmin
):
    """The asymmetric case: the SPA echoes the unchanged address on every profile
    save, so an identical value must not count as a rename — it would un-verify
    the account and revoke every session on a name edit."""
    user = await user_service.get_by_id(real_session, org_admin.admin_id)
    assert user is not None
    before, verified_before = user.token_epoch, user.email_verified_at
    await user_service.update_profile(
        real_session, user, email=f"  {org_admin.admin_email.upper()}  "
    )
    await real_session.commit()
    await real_session.refresh(user)
    assert user.token_epoch == before
    assert user.email_verified_at == verified_before
