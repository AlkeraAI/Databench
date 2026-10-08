"""The lockout must not be an account-existence oracle.

Before this, six wrong passwords made a REAL address answer
``429 account_locked`` while an unknown one answered ``401`` forever: six
requests per address told an attacker who has an Alkera account, on the one
route that already runs a decoy KDF so latency cannot leak the same fact.

These cases drive the real route and compare the two answer SEQUENCES — every
status and every error code, in order — rather than checking each side in
isolation, because "indistinguishable" is a statement about the pair. Time is
driven with freezegun so the window and the cool-off are crossed for real
rather than approximated with a constant.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import LoginLockout, User
from backend.services.identity import lockout as lockout_service
from freezegun import freeze_time
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from tests.conftest import OrgWithAdmin, _unique_email

#: What the caller observes for each outcome: the canonical envelope always
#: carries a code, so these are the full observable answers, not just statuses.
REFUSED = (401, "auth_required")
LOCKED = (429, "account_locked")
WRONG = "definitely-not-the-password"
START = "2026-07-01 12:00:00"


@pytest.fixture(autouse=True)
async def _no_inherited_lockouts() -> None:
    """Start every case with both halves' counters empty.

    Neither half is cleaned by anything else any more: the unknown half's
    retention moved off the login path onto a daily job, and the `users` half is
    never pruned at all. These cases also run under a frozen clock that starts
    at the same instant every time, so a lock another case left — a threshold-1
    case locks on its FIRST failure — is still in the future when the next one
    begins, and its opening answer is a 429 it never earned.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(delete(LoginLockout))
        await session.execute(
            update(User).values(failed_login_count=0, last_failed_login_at=None, locked_until=None)
        )
        await session.commit()


@pytest.fixture
async def real_account(real_session, org_admin: OrgWithAdmin) -> str:
    """An address with an account that no other case has signed in as.

    The real half has to be as fresh as the unknown half for the comparison to
    mean anything: a shared admin carries whatever the previous parametrisation
    did to it, and what these cases assert is a schedule from a standing start.
    """
    from tests.conftest import make_member

    user, _password = await make_member(real_session, org_id=org_admin.org_id, verified=True)
    return str(user.email)


#: Everything one attempt puts in front of the caller: the status, the error
#: code, the ``Retry-After`` header as the raw string a client reads off the
#: wire, and the same wait mirrored into the body.
Answer = tuple[int, str | None, str | None, int | None]


async def _answer(client: AsyncClient, email: str) -> Answer:
    """One sign-in attempt, as the whole of what the caller can observe.

    The header is kept unparsed because the bytes are what an attacker compares:
    a wait that agreed as an integer but was spelled differently on the two
    halves would still separate them.
    """
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": WRONG})
    body = resp.json()
    detail = body.get("error", body.get("detail"))
    envelope = detail if isinstance(detail, dict) else {}
    extra = envelope.get("details")
    mirrored = extra.get("retry_after") if isinstance(extra, dict) else None
    return resp.status_code, envelope.get("code"), resp.headers.get("retry-after"), mirrored


async def _attempt(client: AsyncClient, email: str) -> tuple[int, str | None]:
    """One sign-in attempt, narrowed to the status and code most cases compare."""
    status, code, _header, _mirrored = await _answer(client, email)
    return status, code


async def _spray(client: AsyncClient, email: str, times: int) -> list[tuple[int, str | None]]:
    return [await _attempt(client, email) for _ in range(times)]


async def _locked_until(email: str) -> object:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(select(User.locked_until).where(User.email == email))
        ).scalar_one()


async def _prune() -> int:
    """Run the retention pass the way the worker's daily job runs it."""
    async with AsyncSessionLocal() as session:
        removed = await lockout_service.prune_expired(session)
        await session.commit()
        return removed


def _at(seconds: int) -> str:
    """A wall-clock moment ``seconds`` after :data:`START`, for ``move_to``."""
    return (datetime.fromisoformat(START) + timedelta(seconds=seconds)).isoformat(sep=" ")


def _tune(monkeypatch: pytest.MonkeyPatch, *, threshold: int, window: int, duration: int) -> None:
    monkeypatch.setattr(settings, "auth_lockout_threshold", threshold)
    monkeypatch.setattr(settings, "auth_lockout_window_seconds", window)
    monkeypatch.setattr(settings, "auth_lockout_duration_seconds", duration)


#: The three knobs are independent settings, so the parity has to hold over
#: their COMBINATIONS, not just the shipped one. ``threshold=1`` is the boundary
#: where the upsert's INSERT half is the only write that ever runs, and
#: ``duration > window`` is where a row is past its streak while still inside
#: its lock. Both are ordinary hardening choices; both broke this property
#: before the fix, and each of these ids is one of those breaks.
_SHAPES = [
    pytest.param(60, 30, id="cooloff-inside-window"),
    pytest.param(900, 900, id="shipped-defaults"),
    pytest.param(60, 600, id="cooloff-outlives-window"),
]


@pytest.mark.usefixtures("wide_credential_window")
@pytest.mark.parametrize("threshold", [1, 3, 5])
@pytest.mark.parametrize(("window", "duration"), _SHAPES)
async def test_the_two_halves_answer_the_same_sequence(
    client: AsyncClient,
    real_account: str,
    monkeypatch: pytest.MonkeyPatch,
    threshold: int,
    window: int,
    duration: int,
) -> None:
    """A real address and an unknown one answer identically, attempt for attempt,
    across the streak, the lock, the daily prune and the release — for every
    legal combination of the three settings.

    Compared as SEQUENCES, because "indistinguishable" is a claim about the
    pair; and pinned against the schedule the settings describe, so a run in
    which neither side ever locked cannot pass either.
    """
    _tune(monkeypatch, threshold=threshold, window=window, duration=duration)
    ghost = _unique_email("ghost")
    # Two past the threshold: the attempt that REACHES it is still refused as an
    # ordinary wrong password (the lock is read before the password is checked),
    # and the ones after it are the first to be turned away.
    attempts = threshold + 2
    expected = [REFUSED] * threshold + [LOCKED] * (attempts - threshold)

    with freeze_time(START, real_asyncio=True) as frozen:
        real = await _spray(client, real_account, attempts)
        ghosts = await _spray(client, ghost, attempts)
        assert real == ghosts == expected, (real, ghosts)

        # The daily prune runs while both are still locked. It must not release
        # the half it can reach: where the cool-off outlives the window the
        # unknown row is past its streak but inside its lock, and dropping it
        # there is the oracle back again for the rest of the cool-off.
        frozen.move_to(_at(window + 1))
        await _prune()
        mid = (await _attempt(client, real_account), await _attempt(client, ghost))
        assert mid == ((LOCKED, LOCKED) if duration > window + 1 else (REFUSED, REFUSED)), mid

        # Where the first cool-off ends. Whatever the two answer there, they
        # answer it together — and an attempt that is NOT turned away is itself
        # a failure, which at threshold 1 arms the lock again for both.
        frozen.move_to(_at(window + duration))
        assert await _attempt(client, real_account) == await _attempt(client, ghost)

        # Past that second lock too: with nothing in force, the first attempt is
        # an ordinary refusal on both sides, and the prune has not let one of
        # them out early.
        frozen.move_to(_at(window + 2 * duration + 2))
        await _prune()
        released = (await _attempt(client, real_account), await _attempt(client, ghost))
        assert released == (REFUSED, REFUSED), released


@pytest.mark.usefixtures("wide_credential_window")
async def test_the_wait_is_told_the_same_way_to_both_halves_and_counts_down(
    client: AsyncClient,
    real_account: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``Retry-After`` header is part of the refusal, so it is part of the
    parity — and it reports the cool-off that is actually left.

    The other cases here compare statuses and codes. A header is the easiest
    place for the oracle to come back: a real account's lock is stamped on
    `users`, an unknown address's on `login_lockouts`, and nothing forces the
    two to derive the same number of seconds from their own row. An attacker who
    cannot tell the two statuses apart can still read a header that is present
    on one side and missing on the other, or that starts from a different
    duration. So the two halves are driven through one schedule and compared as
    whole answers — header included.

    The countdown is the other half of the claim. The number exists so the
    person who mistyped their own password can wait it out instead of knocking
    on the endpoint the lock protects; a constant would tell them the cool-off
    restarts on every attempt, which is both false and the worst advice
    available. Time moves with freezegun so the shrinking is the real one the
    expiry produces rather than an approximation.
    """
    threshold, cool_off = 3, 900
    _tune(monkeypatch, threshold=threshold, window=900, duration=cool_off)
    ghost = _unique_email("ghost")
    # Two moments well inside the cool-off that was stamped at the threshold, so
    # each later attempt has strictly less of it left than the one before.
    inside = (cool_off // 3, cool_off - 1)
    schedule = [0] * (threshold + 1) + list(inside)

    real: list[Answer] = []
    ghosts: list[Answer] = []
    with freeze_time(START, real_asyncio=True) as frozen:
        for offset in schedule:
            frozen.move_to(_at(offset))
            real.append(await _answer(client, real_account))
            ghosts.append(await _answer(client, ghost))

    # Indistinguishable is a claim about the pair, so it is asserted on the pair.
    assert real == ghosts, (real, ghosts)

    # ...and about a pair that really did lock: two runs that never locked would
    # agree on nothing but 401s.
    turned_away = len(schedule) - threshold
    assert [(status, code) for status, code, _, _ in real] == [REFUSED] * threshold + [
        LOCKED
    ] * turned_away
    # An attempt that is turned away does not re-arm the lock — it is refused
    # before the password is read — so what is left shrinks by the elapsed time.
    expected = [cool_off, *(cool_off - offset for offset in inside)]
    locked = real[threshold:]
    assert [header for _, _, header, _ in locked] == [str(wait) for wait in expected], locked
    # The body says the same thing, so a client that reads either one waits the
    # same amount.
    assert [mirrored for _, _, _, mirrored in locked] == expected, locked
    waits = [int(header or "0") for _, _, header, _ in locked]
    assert all(later < earlier for earlier, later in pairwise(waits)), waits


@pytest.mark.usefixtures("wide_credential_window")
async def test_the_prune_keeps_a_row_that_is_past_its_window_but_still_locked(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retention predicate, stated directly rather than through the route.

    A counter matters while EITHER its streak could still grow OR its lock is
    still in force. Judging it on the streak alone is the whole of the
    cool-off-outlives-window break, so it gets its own case.
    """
    _tune(monkeypatch, threshold=2, window=60, duration=3600)
    ghost = _unique_email("ghost")

    with freeze_time(START, real_asyncio=True) as frozen:
        await _spray(client, ghost, 2)
        # Past the window, far short of the cool-off.
        frozen.move_to(_at(120))
        assert await _prune() == 0
        assert await _attempt(client, ghost) == LOCKED
        # Past both.
        frozen.move_to(_at(3600 + 120 + 1))
        assert await _prune() == 1
        assert await _attempt(client, ghost) == REFUSED


@pytest.mark.usefixtures("wide_credential_window")
async def test_a_failure_past_the_window_restarts_both_streaks(
    client: AsyncClient, real_account: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paced guessing must never lock either side — the streak restarts when the
    window lapses, and it has to lapse identically for both."""
    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    monkeypatch.setattr(settings, "auth_lockout_window_seconds", 600)
    ghost = _unique_email("ghost")

    with freeze_time(START, real_asyncio=True) as frozen:
        for minute in (0, 11, 22, 33):
            frozen.move_to(f"2026-07-01 12:{minute:02d}:00")
            assert await _attempt(client, real_account) == REFUSED
            assert await _attempt(client, ghost) == REFUSED


@pytest.mark.usefixtures("wide_credential_window")
async def test_a_sprayed_address_is_not_pre_locked_once_it_has_an_account(
    client: AsyncClient, org_admin: OrgWithAdmin, real_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counter an address collects before anyone registers it must not
    follow it into a real account — otherwise anyone could lock a new customer
    out of their first sign-in by spraying the address first."""
    from tests.conftest import make_member

    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)
    future_email = _unique_email("future")

    with freeze_time(START, real_asyncio=True):
        assert (await _spray(client, future_email, 4))[-1] == LOCKED

    user, password = await make_member(
        real_session, org_id=org_admin.org_id, email=future_email, verified=True
    )
    with freeze_time(START, real_asyncio=True):
        # The new account has its own (empty) counter, so it signs in at once...
        ok = await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": password or ""}
        )
        assert ok.status_code == 200, ok.text
        # ...and the stale row is gone, so a later wrong password starts from zero.
        assert await _attempt(client, future_email) == REFUSED


@pytest.mark.usefixtures("wide_credential_window")
async def test_disabling_the_lockout_disables_both_halves(
    client: AsyncClient, real_account: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "auth_lockout_threshold", 0)
    ghost = _unique_email("ghost")

    with freeze_time(START, real_asyncio=True):
        assert await _spray(client, real_account, 6) == [REFUSED] * 6
        assert await _spray(client, ghost, 6) == [REFUSED] * 6

    async with AsyncSessionLocal() as session:
        assert (await session.execute(select(LoginLockout))).first() is None


@pytest.mark.usefixtures("wide_credential_window")
async def test_the_unknown_counter_never_touches_a_real_account_row(
    client: AsyncClient, real_account: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two counters are separate stores on purpose: an unknown-address spray
    must not be able to lock a real account that shares nothing but a digest
    space, and the admin-visible `users.locked_until` must stay truthful."""
    monkeypatch.setattr(settings, "auth_lockout_threshold", 3)

    with freeze_time(START, real_asyncio=True):
        await _spray(client, _unique_email("ghost"), 5)

    assert await _locked_until(real_account) is None
