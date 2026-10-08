"""An accepted TOTP cannot be spent twice.

Clock-skew tolerance accepts a code across several 30-second steps, so a code
observed once -- over a shoulder, in a screenshot, in a phishing relay that is
racing the real login -- stays valid for the rest of that band. RFC 6238 section
5.2 says a validated code must be refused on a second presentation, and skew
tolerance alone cannot do that: the verifier has to remember what it accepted.

`totp.matching_counter` reports the step a code matched; these pin that the
service actually RECORDS it on the account and enforces it afterwards, which is
the half a unit test of the primitive cannot see. The primitive's own cases live
in `packages/api-core/tests/test_totp_single_use.py`.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from alkera_core.auth import totp
from alkera_core.auth.secret_box import encrypt_secret
from alkera_core.auth.token_hash import hash_lookup_token
from alkera_core.models import User
from backend.services.identity import mfa as mfa_service
from httpx import ASGITransport, AsyncClient
from tests._suite_app import app as fastapi_app
from tests.conftest import OrgWithAdmin, TotpClock, app_client, login, make_member


def _user_with_mfa(secret: str, *, backup: list[str] | None = None) -> User:
    """A User carrying an active factor. Detached from any session on purpose --
    these assert the service's effect on the instance, which is what the routes
    then commit."""
    user = User(email="mfa@example.com", password_hash="x")
    user.mfa_secret_encrypted = encrypt_secret(secret)
    user.mfa_enabled = True
    user.mfa_backup_codes = json.dumps([hash_lookup_token(c) for c in (backup or [])])
    return user


def _code(secret: str, *, step: int) -> str:
    return totp._hotp(secret, step)


def _now_step() -> int:
    return int(time.time() // 30)


def test_a_code_accepted_once_is_refused_on_the_second_presentation() -> None:
    secret = totp.generate_secret()
    user = _user_with_mfa(secret)
    code = _code(secret, step=_now_step())

    assert mfa_service.spend_code_unlocked(user, code) is True
    assert mfa_service.spend_code_unlocked(user, code) is False, "the same code was spent twice"


def test_the_accepted_step_is_recorded_on_the_account() -> None:
    """The record is the whole mechanism: without it persisted, the next request
    -- served by another task, or after a restart -- knows nothing."""
    secret = totp.generate_secret()
    user = _user_with_mfa(secret)
    assert user.mfa_last_used_counter is None

    step = _now_step()
    assert mfa_service.spend_code_unlocked(user, _code(secret, step=step)) is True
    assert user.mfa_last_used_counter == step


@pytest.mark.parametrize(
    "drift",
    [pytest.param(-1, id="the-previous-step"), pytest.param(0, id="the-step-just-spent")],
)
def test_no_earlier_code_in_the_skew_band_survives_a_later_one(drift: int) -> None:
    """Spending step N must retire the whole band up to N, not only the exact
    digits presented -- the previous step's code is equally observable and would
    otherwise still be live."""
    secret = totp.generate_secret()
    user = _user_with_mfa(secret)
    now = _now_step()

    assert mfa_service.spend_code_unlocked(user, _code(secret, step=now)) is True
    assert mfa_service.spend_code_unlocked(user, _code(secret, step=now + drift)) is False


def test_the_next_step_is_still_accepted() -> None:
    """The asymmetric case: single-use must not become one-code-per-account. The
    authenticator's next code has to work."""
    secret = totp.generate_secret()
    user = _user_with_mfa(secret)
    now = _now_step()

    assert mfa_service.spend_code_unlocked(user, _code(secret, step=now)) is True
    assert mfa_service.spend_code_unlocked(user, _code(secret, step=now + 1)) is True
    assert user.mfa_last_used_counter == now + 1


def test_a_backup_code_is_unaffected_by_the_totp_record() -> None:
    """Backup codes carry their own single-use rule (the digest is removed). A
    spent TOTP step must not make them unusable, and using one must not stamp a
    step that would retire live TOTP codes."""
    secret = totp.generate_secret()
    user = _user_with_mfa(secret, backup=["aaaaa-bbbbb"])
    assert mfa_service.spend_code_unlocked(user, _code(secret, step=_now_step())) is True
    spent_step = user.mfa_last_used_counter

    assert mfa_service.spend_code_unlocked(user, "aaaaa-bbbbb") is True
    assert user.mfa_last_used_counter == spent_step
    assert mfa_service.spend_code_unlocked(user, "aaaaa-bbbbb") is False  # still single-use


def test_the_enrolling_code_is_spent_by_confirmation() -> None:
    """The code typed into the setup form is as observable as any other. If
    confirmation does not spend it, it still satisfies the very next step-up."""
    secret = totp.generate_secret()
    user = User(email="enrolling@example.com", password_hash="x")
    user.mfa_secret_encrypted = encrypt_secret(secret)
    code = _code(secret, step=_now_step())

    codes = mfa_service.confirm_enrollment(user, code)
    assert len(codes) == 10
    assert user.mfa_enabled is True
    assert user.mfa_last_used_counter == _now_step()
    assert mfa_service.spend_code_unlocked(user, code) is False


def test_disabling_clears_the_record_so_a_later_enrollment_is_not_blocked() -> None:
    """A new enrollment mints a new secret, so a step spent against the old one
    describes nothing about it and would only refuse the user's real codes."""
    secret = totp.generate_secret()
    user = _user_with_mfa(secret)
    now = _now_step()
    assert mfa_service.spend_code_unlocked(user, _code(secret, step=now)) is True

    mfa_service.disable(user, _code(secret, step=now + 1))
    assert user.mfa_last_used_counter is None

    fresh = totp.generate_secret()
    user.mfa_secret_encrypted = encrypt_secret(fresh)
    mfa_service.confirm_enrollment(user, _code(fresh, step=now))
    assert user.mfa_enabled is True


class TestConcurrentSpend:
    """Two requests presenting the same code at the same moment.

    This is not a hypothetical: a relay phishing kit forwards the victim's code
    the instant they type it, so the attacker's request and the real one are in
    flight together. If both read the account's factor state before either writes
    it, both accept the same code and recording the step buys almost nothing.
    """

    @pytest.mark.asyncio
    async def test_only_one_of_two_concurrent_logins_spends_the_code(
        self, client: AsyncClient, org_admin: OrgWithAdmin, real_session, totp_clock: TotpClock
    ) -> None:
        member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
        await real_session.commit()
        assert password is not None

        await login(client, member.email, password)
        secret = (await client.post("/api/v1/auth/mfa/enroll")).json()["secret"]
        assert (
            await client.post("/api/v1/auth/mfa/confirm", json={"code": totp_clock.code(secret)})
        ).status_code == 200

        code = totp_clock.next_code(secret)
        body = {"email": member.email, "password": password, "mfa_code": code}

        # Separate clients so neither answers from the other's cookie jar, and a
        # real gather so the two requests genuinely overlap in the event loop.
        async def _attempt() -> int:
            transport = ASGITransport(app=fastapi_app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                return (await c.post("/api/v1/auth/login", json=body)).status_code

        first, second = await asyncio.gather(_attempt(), _attempt())

        assert sorted([first, second]) == [200, 401], (
            f"the same code was accepted by both concurrent logins ({first}, {second})"
        )

    @pytest.mark.asyncio
    async def test_a_double_submitted_confirmation_is_a_state_error_not_a_guess(
        self, client: AsyncClient, org_admin: OrgWithAdmin, real_session, totp_clock: TotpClock
    ) -> None:
        """The loser of a confirm race must not be charged to the lockout budget.

        Both requests read `mfa_enabled` to decide whether a refusal was a guess
        against the 6-digit space or a state error. Read before the row lock, the
        loser sees the pre-race state, calls its "already enabled" refusal a bad
        code, and spends the owner's failure budget on their own double-click.
        """
        member, password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
        await real_session.commit()
        assert password is not None
        await login(client, member.email, password)

        secret = (await client.post("/api/v1/auth/mfa/enroll")).json()["secret"]
        cookie = dict(client.cookies)
        code = totp_clock.code(secret)

        async def _confirm() -> int:
            async with app_client(cookies=cookie) as c:
                return (await c.post("/api/v1/auth/mfa/confirm", json={"code": code})).status_code

        first, second = await asyncio.gather(_confirm(), _confirm())
        assert sorted([first, second]) == [200, 400], (first, second)

        await real_session.refresh(member)
        assert member.failed_login_count == 0, (
            "a benign double-submit spent the account's lockout budget"
        )
