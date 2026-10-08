"""The auth-token prune against the real local Postgres.

Drives the async core directly on the repo's single session-scoped event
loop (the shared async engine pool is bound to it); the Temporal activity and
workflow around it are covered in ``test_auth_workflow.py``. The catalog pin
here is the one line that ties this job to its daily schedule.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alkera_core.config import settings
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.models import AuthToken, LoginLockout, Team, TokenType, User
from alkera_core.temporal import TaskQueue, WorkflowType
from freezegun import freeze_time
from sqlalchemy import select
from worker.activities.auth import prune_expired_tokens, prune_login_lockouts
from worker.schedules import SCHEDULES
from worker.tasks.auth import run_prune_expired_tokens, run_prune_login_lockouts
from worker.temporal.queues import activity_type_name, served_workflow_types


async def _seed() -> tuple[str, str]:
    """Create a user with one expired + one live token. Returns their jtis."""
    async with AsyncSessionLocal() as s:
        team = Team(name=f"prune-org-{secrets.token_hex(4)}", is_root=True)
        s.add(team)
        await s.flush()
        user = User(
            home_org_team_id=team.id,
            email=f"prune-{secrets.token_hex(6)}@alkera.dev",
            first_name="Prune",
            last_name="Test",
        )
        s.add(user)
        await s.flush()

        now = datetime.now(UTC)
        expired_jti, live_jti = uuid4().hex, uuid4().hex
        s.add(
            AuthToken(
                jti=expired_jti,
                user_id=user.id,
                token_type=TokenType.CLI,
                issued_at=now - timedelta(days=2),
                expires_at=now - timedelta(days=1),
            )
        )
        s.add(
            AuthToken(
                jti=live_jti,
                user_id=user.id,
                token_type=TokenType.CLI,
                issued_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
        await s.commit()
        return expired_jti, live_jti


async def _jti_exists(jti: str) -> bool:
    async with AsyncSessionLocal() as s:
        row = await s.execute(select(AuthToken.jti).where(AuthToken.jti == jti))
        return row.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_prune_removes_expired_keeps_live() -> None:
    expired_jti, live_jti = await _seed()

    removed = await run_prune_expired_tokens()

    # Asserted by identity, not by the count: the pass deletes by predicate across
    # the whole table, and the workflow suite and `test_auth_revocation.py` prune
    # the same one on the same per-worker database. What this test means is the two
    # rows it seeded.
    assert isinstance(removed, int)
    assert await _jti_exists(expired_jti) is False
    assert await _jti_exists(live_jti) is True


def test_the_token_prune_is_served_and_scheduled_daily() -> None:
    """The activity keeps the historical task name, some worker serves its
    workflow, and the catalog runs it at 03:00 UTC on the default queue."""
    assert activity_type_name(prune_expired_tokens) == "auth.prune_expired_tokens"
    assert WorkflowType.PRUNE_EXPIRED_TOKENS.value in served_workflow_types()
    entry = next(e for e in SCHEDULES if e.id == "prune-expired-tokens")
    assert entry.workflow is WorkflowType.PRUNE_EXPIRED_TOKENS
    assert entry.spec_text == "0 3 * * *"
    assert entry.queue is TaskQueue.DEFAULT


# --- the lockout counters kept for addresses with no account ---------------------


async def _seed_lockouts(now: datetime) -> tuple[str, str, str]:
    """Three counters stamped at ``now``: one whose lock ends inside the window,
    one locked far past it, and one that never reached the threshold.

    Returns their digests. The digest is opaque to this table — it is an HMAC of
    an address the prune never sees — so the test writes its own.
    """
    spent, still_locked, never_locked = (secrets.token_hex(32) for _ in range(3))
    async with AsyncSessionLocal() as s:
        s.add(
            LoginLockout(
                identifier_digest=spent,
                failed_count=3,
                last_failed_at=now,
                locked_until=now + timedelta(seconds=30),
            )
        )
        s.add(
            LoginLockout(
                identifier_digest=still_locked,
                failed_count=3,
                last_failed_at=now,
                locked_until=now + timedelta(hours=1),
            )
        )
        s.add(
            LoginLockout(
                identifier_digest=never_locked,
                failed_count=1,
                last_failed_at=now,
                locked_until=None,
            )
        )
        await s.commit()
    return spent, still_locked, never_locked


async def _digest_exists(digest: str) -> bool:
    async with AsyncSessionLocal() as s:
        row = await s.execute(
            select(LoginLockout.identifier_digest).where(LoginLockout.identifier_digest == digest)
        )
        return row.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_the_lockout_prune_keeps_a_counter_until_both_its_clocks_have_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A counter is spent only once it is past its failure window AND past its
    cool-off, and the pass is driven across both boundaries rather than reading
    one arrangement of rows.

    This is the retention half of the lockout's existence-oracle fix: the
    ``users`` counter is never pruned, so releasing one of these early makes an
    address nobody has answer 401 while an address somebody has still answers
    429 — the fact the whole table exists to hide.
    """
    monkeypatch.setattr(settings, "auth_lockout_window_seconds", 60)
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    spent, still_locked, never_locked = await _seed_lockouts(start)

    # Asserted by identity rather than by the number the pass returns: this
    # table is shared with everything else the suite signs in as, so a count is
    # a statement about other tests' rows too.
    with freeze_time(start, real_asyncio=True) as frozen:
        # A second inside the window: nothing of ours is spent yet, not even
        # the row whose own lock lapsed thirty seconds ago.
        frozen.move_to(start + timedelta(seconds=59))
        await run_prune_login_lockouts()
        assert await _digest_exists(spent) is True
        assert await _digest_exists(never_locked) is True
        assert await _digest_exists(still_locked) is True

        # A second past it: the two whose locks have run go; the one locked for
        # another hour stays, which is the case a window-only prune got wrong.
        frozen.move_to(start + timedelta(seconds=61))
        await run_prune_login_lockouts()
        assert await _digest_exists(spent) is False
        assert await _digest_exists(never_locked) is False
        assert await _digest_exists(still_locked) is True

        # And past the cool-off, the last one goes too.
        frozen.move_to(start + timedelta(hours=1, seconds=1))
        await run_prune_login_lockouts()
        assert await _digest_exists(still_locked) is False


def test_the_lockout_prune_is_served_and_scheduled_daily() -> None:
    """Named, served, and on the default queue at 03:30 UTC — after the token
    and device-code prunes rather than alongside them, so three predicate
    deletes do not land on the same connection at the same minute."""
    assert activity_type_name(prune_login_lockouts) == "auth.prune_login_lockouts"
    assert WorkflowType.PRUNE_LOGIN_LOCKOUTS.value in served_workflow_types()
    entry = next(e for e in SCHEDULES if e.id == "prune-login-lockouts")
    assert entry.workflow is WorkflowType.PRUNE_LOGIN_LOCKOUTS
    assert entry.spec_text == "30 3 * * *"
    assert entry.queue is TaskQueue.DEFAULT
