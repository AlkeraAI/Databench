"""GitHub `_pick_email` — prefers a verified business address among several.

A GitHub account can expose multiple emails (`GET /user/emails`). These unit
tests pin the selection priority directly against the pure static method. The
personal/business split comes from the real vendored list via
`backend.auth.email_policy.is_personal_email`; the fixtures use canonical free
providers (gmail/yahoo) vs a clearly-business domain (acme.com).
"""

from __future__ import annotations

import pytest
from backend.auth.oauth.base import OAuthError
from backend.auth.oauth.github import GitHubProvider


def _email(addr: str, *, verified: bool, primary: bool = False) -> dict[str, object]:
    return {"email": addr, "verified": verified, "primary": primary}


def _pick(emails: object, *, fallback: str | None = None) -> tuple[str, bool]:
    return GitHubProvider._pick_email(emails, fallback=fallback)


def test_prefers_verified_business_over_verified_personal_primary() -> None:
    emails = [
        _email("me@gmail.com", verified=True, primary=True),
        _email("me@acme.com", verified=True),
    ]
    assert _pick(emails) == ("me@acme.com", True)


def test_verified_personal_beats_unverified_business() -> None:
    # Verified-first invariant wins: an unverified business email must NOT be
    # chosen over a verified personal one (it would block auto-link).
    emails = [
        _email("me@gmail.com", verified=True, primary=True),
        _email("me@acme.com", verified=False),
    ]
    assert _pick(emails) == ("me@gmail.com", True)


def test_business_first_among_unverified_when_none_verified() -> None:
    emails = [
        _email("me@gmail.com", verified=False, primary=True),
        _email("me@acme.com", verified=False),
    ]
    assert _pick(emails) == ("me@acme.com", False)


def test_noreply_never_preferred_over_a_real_email() -> None:
    emails = [
        _email("12345+me@users.noreply.github.com", verified=True, primary=True),
        _email("me@gmail.com", verified=True),
    ]
    # A real (even personal) verified address beats the synthetic noreply alias.
    assert _pick(emails) == ("me@gmail.com", True)


def test_noreply_used_only_as_last_resort() -> None:
    emails = [_email("12345+me@users.noreply.github.com", verified=True, primary=True)]
    assert _pick(emails) == ("12345+me@users.noreply.github.com", True)


def test_primary_verified_kept_when_all_personal() -> None:
    emails = [
        _email("secondary@yahoo.com", verified=True),
        _email("primary@gmail.com", verified=True, primary=True),
    ]
    assert _pick(emails) == ("primary@gmail.com", True)


def test_fallback_used_when_emails_empty() -> None:
    assert _pick([], fallback="x@acme.com") == ("x@acme.com", False)


def test_raises_when_nothing_usable() -> None:
    with pytest.raises(OAuthError):
        _pick([], fallback=None)
