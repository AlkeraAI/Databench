"""Email-verification grace-period policy (`alkera_core.verification`).

Pure functions over a User-shaped object — exercised with a lightweight stand-in
so no DB is needed. The grace window is read from settings so these stay correct
if the default is retuned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from alkera_core import verification
from alkera_core.config import settings


def _user(
    *,
    verified: bool = False,
    created_days_ago: int = 0,
    platform_role: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        email_verified_at=datetime.now(UTC) if verified else None,
        created_at=datetime.now(UTC) - timedelta(days=created_days_ago),
        platform_role=platform_role,
    )


def test_verified_user_is_never_gated() -> None:
    u = _user(verified=True, created_days_ago=999)
    assert verification.requires_verification(u) is False
    assert verification.deadline(u) is None
    assert verification.state(u) == "verified"
    assert verification.is_blocked(u) is False


def test_platform_staff_is_exempt() -> None:
    u = _user(verified=False, created_days_ago=999, platform_role="alkera_admin")
    assert verification.is_exempt(u) is True
    assert verification.requires_verification(u) is False
    assert verification.deadline(u) is None
    assert verification.state(u) == "verified"
    assert verification.is_blocked(u) is False


def test_fresh_unverified_user_is_in_grace() -> None:
    u = _user(verified=False, created_days_ago=0)
    assert verification.requires_verification(u) is True
    assert verification.deadline(u) is not None
    assert verification.state(u) == "grace"
    assert verification.is_blocked(u) is False


def test_unverified_past_grace_is_blocked() -> None:
    u = _user(verified=False, created_days_ago=settings.email_verification_grace_period_days + 1)
    assert verification.requires_verification(u) is True
    assert verification.state(u) == "blocked"
    assert verification.is_blocked(u) is True


def test_no_email_mode_treats_everyone_as_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    # With EMAIL_ENABLED=false a verification link can't be delivered, so the gate is
    # off deployment-wide: even a long-unverified, non-staff account is never blocked
    # and the durable-mutation gate (is_verified) lets it through.
    monkeypatch.setattr(settings, "email_enabled", False)
    u = _user(verified=False, created_days_ago=999)
    assert verification.requires_verification(u) is False
    assert verification.is_verified(u) is True
    assert verification.deadline(u) is None
    assert verification.state(u) == "verified"
    assert verification.is_blocked(u) is False


def test_deadline_is_created_at_plus_grace() -> None:
    u = _user(verified=False, created_days_ago=0)
    expected = u.created_at + timedelta(days=settings.email_verification_grace_period_days)
    assert verification.deadline(u) == expected


def test_deadline_boundary_is_inclusive_of_grace() -> None:
    days = settings.email_verification_grace_period_days
    now = datetime(2026, 6, 1, tzinfo=UTC)
    user = SimpleNamespace(
        email_verified_at=None,
        created_at=now - timedelta(days=days),
        platform_role=None,
    )
    # Exactly at the deadline → blocked; one second earlier → still in grace.
    assert verification.state(user, now=now) == "blocked"
    assert verification.is_blocked(user, now=now) is True
    assert verification.state(user, now=now - timedelta(seconds=1)) == "grace"
    assert verification.is_blocked(user, now=now - timedelta(seconds=1)) is False
