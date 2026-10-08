"""Login hardening: DB-backed brute-force lockout + TOTP MFA (enroll → confirm →
challenge on login → backup codes → disable)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import User
from httpx import AsyncClient
from sqlalchemy import select
from tests.conftest import OrgWithAdmin, TotpClock, login, make_member

pytestmark = pytest.mark.asyncio


async def _lockout_state(email: str) -> tuple[int, bool]:
    """`(failed_login_count, is_locked)` straight from the row."""
    async with AsyncSessionLocal() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        return user.failed_login_count, user.locked_until is not None


async def _member(org_id: uuid.UUID, *, pw: str = "member-pass-123") -> tuple[str, str]:
    async with AsyncSessionLocal() as s:
        m, p = await make_member(
            s,
            org_id=org_id,
            email=f"u-{uuid.uuid4().hex[:8]}@m.example",
            password=pw,
            verified=True,
        )
        email = m.email
        await s.commit()
    assert p is not None
    return email, p


# --------------------------------------------------------------------------- #
# Brute-force lockout
# --------------------------------------------------------------------------- #


async def test_account_locks_after_threshold_failures(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    email, pw = await _member(org_admin.org_id)

    for _ in range(3):
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})
        assert r.status_code == 401

    # Locked: even the CORRECT password is refused with 429 until the cool-off.
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "account_locked"


async def test_a_success_resets_the_failure_counter(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    email, pw = await _member(org_admin.org_id)

    for _ in range(2):  # below the threshold
        await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})
    assert (
        await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    ).status_code == 200  # resets the counter

    for _ in range(2):  # would have locked if the counter hadn't reset
        await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})
    assert (
        await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    ).status_code == 200


async def test_concurrent_failures_are_all_counted(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """The counter must track ATTEMPTS, not rounds of attempts.

    A read-modify-write loses every guess that lands between another request's
    read and its commit, so an attacker multiplies their budget by whatever
    concurrency they choose — the exact defeat of the only server-side
    brute-force control this product has.

    Exactly the threshold's worth of attempts, in flight together: no request
    can see the lock (it takes every one of them to reach the threshold), so
    each must be a 401 — and afterwards the count must equal the attempts and
    the lock must be set, which is what a lost update cannot produce. One
    attempt more would let a late arrival meet a lock the others already
    committed and answer 429 by design, not by defect."""
    email, _pw = await _member(org_admin.org_id)
    attempts = settings.auth_lockout_threshold

    results = await asyncio.gather(
        *(
            client.post("/api/v1/auth/login", json={"email": email, "password": f"wrong-{i}"})
            for i in range(attempts)
        )
    )
    assert [r.status_code for r in results] == [401] * attempts

    count, locked = await _lockout_state(email)
    assert count == attempts
    assert locked  # the threshold reached: every concurrent failure was counted


async def test_a_failure_outside_the_window_restarts_the_streak(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window is a streak, not a lifetime total: a failure long after the
    previous one starts over at 1 rather than accumulating forever."""
    monkeypatch.setattr(settings, "auth_lockout_window_seconds", 0)
    email, _pw = await _member(org_admin.org_id)

    for _ in range(3):
        await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})

    count, locked = await _lockout_state(email)
    assert count == 1
    assert not locked


async def test_login_pays_the_kdf_for_an_unknown_address(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constant work per attempt: the password hasher runs exactly once whether
    or not the address resolves to an account. Skipping it for unknown addresses
    makes response latency an account-existence oracle."""
    from backend.api.routes.identity import auth as auth_routes

    real = auth_routes.verify_password
    hashes: list[str | None] = []

    def _spy(plain: str, hashed: str | None) -> bool:
        hashes.append(hashed)
        return real(plain, hashed)

    monkeypatch.setattr(auth_routes, "verify_password", _spy)

    unknown = f"nobody-{uuid.uuid4().hex[:8]}@alkera.dev"
    assert (
        await client.post("/api/v1/auth/login", json={"email": unknown, "password": "whatever"})
    ).status_code == 401
    assert len(hashes) == 1
    assert hashes[0] is not None  # a real argon2 verify ran, against the decoy

    hashes.clear()
    email, pw = await _member(org_admin.org_id)
    assert (
        await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    ).status_code == 200
    assert len(hashes) == 1


# --------------------------------------------------------------------------- #
# TOTP MFA
# --------------------------------------------------------------------------- #


async def _enable_mfa(
    client: AsyncClient, email: str, pw: str, clock: TotpClock
) -> tuple[str, list[str]]:
    """Log in, enroll + confirm MFA; return (secret, backup_codes).

    Confirmation SPENDS the code it accepts, so every later step-up in the same
    flow has to come from a later step -- `clock.next_code`.
    """
    await login(client, email, pw)
    enroll = (await client.post("/api/v1/auth/mfa/enroll")).json()
    secret = enroll["secret"]
    confirm = await client.post("/api/v1/auth/mfa/confirm", json={"code": clock.code(secret)})
    assert confirm.status_code == 200, confirm.text
    return secret, confirm.json()["backup_codes"]


async def test_mfa_challenge_on_login_with_totp_and_backup_codes(
    client: AsyncClient, org_admin: OrgWithAdmin, totp_clock: TotpClock
) -> None:
    email, pw = await _member(org_admin.org_id)
    secret, backup = await _enable_mfa(client, email, pw, totp_clock)
    assert len(backup) == 10

    # A password-only login is now refused with mfa_required.
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "mfa_required"

    # Wrong code → mfa_invalid.
    r = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": pw, "mfa_code": "000000"}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "mfa_invalid"

    # Correct TOTP → in.
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": pw, "mfa_code": totp_clock.next_code(secret)},
    )
    assert r.status_code == 200

    # A backup code works ONCE, then is consumed.
    r = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": pw, "mfa_code": backup[0]}
    )
    assert r.status_code == 200
    r = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": pw, "mfa_code": backup[0]}
    )
    assert r.status_code == 401  # already used


async def test_mfa_disable_requires_a_code_then_restores_password_login(
    client: AsyncClient, org_admin: OrgWithAdmin, totp_clock: TotpClock
) -> None:
    email, pw = await _member(org_admin.org_id)
    secret, _backup = await _enable_mfa(client, email, pw, totp_clock)

    # Disable needs a valid code (re-login to get a fresh session under MFA first).
    await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": pw, "mfa_code": totp_clock.next_code(secret)},
    )
    bad = await client.post("/api/v1/auth/mfa/disable", json={"code": "000000"})
    assert bad.status_code == 400
    ok = await client.post("/api/v1/auth/mfa/disable", json={"code": totp_clock.next_code(secret)})
    assert ok.status_code == 200

    # MFA off → password-only login works again.
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200


async def test_mfa_disable_is_throttled_by_the_same_lockout(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    totp_clock: TotpClock,
) -> None:
    """Six digits is a grindable space, so the disable route must consume the
    same failure budget as login — otherwise a hijacked session can brute-force
    MFA off at line rate and turn a stolen cookie into durable access."""
    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    email, pw = await _member(org_admin.org_id)
    secret, _backup = await _enable_mfa(client, email, pw, totp_clock)
    await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": pw, "mfa_code": totp_clock.next_code(secret)},
    )

    for _ in range(3):
        bad = await client.post("/api/v1/auth/mfa/disable", json={"code": "000000"})
        assert bad.status_code == 400

    count, locked = await _lockout_state(email)
    assert count == 3
    assert locked

    # Past the threshold the route stops evaluating codes at all — including a
    # VALID one, so the attacker can't slip through by guessing during the lock.
    blocked = await client.post(
        "/api/v1/auth/mfa/disable", json={"code": totp_clock.next_code(secret)}
    )
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "account_locked"

    # MFA is still on: the grind never landed.
    assert (await client.get("/api/v1/auth/mfa/status")).json()["enabled"] is True


async def test_mfa_confirm_bad_codes_count_toward_the_lockout(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enrollment confirmation checks the same 6-digit code, so it shares the
    budget too."""
    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    email, pw = await _member(org_admin.org_id)
    await login(client, email, pw)
    await client.post("/api/v1/auth/mfa/enroll")

    for _ in range(3):
        assert (
            await client.post("/api/v1/auth/mfa/confirm", json={"code": "000000"})
        ).status_code == 400

    count, locked = await _lockout_state(email)
    assert count == 3
    assert locked
    assert (
        await client.post("/api/v1/auth/mfa/confirm", json={"code": "000000"})
    ).status_code == 429


@pytest.mark.parametrize(
    ("state", "route"),
    [
        pytest.param("not-enrolled", "/api/v1/auth/mfa/confirm", id="confirm-before-enrolling"),
        pytest.param("already-enabled", "/api/v1/auth/mfa/confirm", id="confirm-when-already-on"),
        pytest.param("not-enabled", "/api/v1/auth/mfa/disable", id="disable-when-mfa-is-off"),
    ],
)
async def test_an_mfa_state_error_is_not_charged_to_the_lockout_budget(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    totp_clock: TotpClock,
    state: str,
    route: str,
) -> None:
    """The other asymmetric case: only a rejected CODE is a guess.

    These routes also refuse on state ("MFA is already enabled", "Start
    enrollment first", "MFA is not enabled") — a double-submitted button, not an
    attempt at the 6-digit space. Counting those would let a few benign clicks
    (or a handful of requests from a hijacked session) lock the owner out of
    login entirely, turning the brute-force control into a DoS lever."""
    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    email, pw = await _member(org_admin.org_id)
    if state == "already-enabled":
        await _enable_mfa(client, email, pw, totp_clock)
    else:
        await login(client, email, pw)

    for _ in range(settings.auth_lockout_threshold + 2):
        r = await client.post(route, json={"code": "000000"})
        # Still the state error, never the throttle's 429 — the budget is untouched.
        assert r.status_code == 400, r.text

    count, locked = await _lockout_state(email)
    assert count == 0
    assert not locked


async def test_a_correct_mfa_code_never_counts_as_a_failure(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    monkeypatch: pytest.MonkeyPatch,
    totp_clock: TotpClock,
) -> None:
    """The asymmetric case: the throttle must not fire on success, or ordinary
    use of MFA would lock people out of their own accounts."""
    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    email, pw = await _member(org_admin.org_id)
    secret, _backup = await _enable_mfa(client, email, pw, totp_clock)
    await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": pw, "mfa_code": totp_clock.next_code(secret)},
    )

    assert (
        await client.post("/api/v1/auth/mfa/disable", json={"code": totp_clock.next_code(secret)})
    ).status_code == 200
    count, locked = await _lockout_state(email)
    assert count == 0
    assert not locked


async def test_mfa_status_reflects_state(
    client: AsyncClient, org_admin: OrgWithAdmin, totp_clock: TotpClock
) -> None:
    email, pw = await _member(org_admin.org_id)
    await login(client, email, pw)
    assert (await client.get("/api/v1/auth/mfa/status")).json()["enabled"] is False
    await _enable_mfa(client, email, pw, totp_clock)
    status = (await client.get("/api/v1/auth/mfa/status")).json()
    assert status["enabled"] is True
    assert status["backup_codes_remaining"] == 10
