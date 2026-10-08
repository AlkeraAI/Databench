"""Tests for the backend personal/business email-domain detector + vendored list."""

from __future__ import annotations

import pytest
from backend.auth.email_policy import DEFAULT_PERSONAL_EMAIL_DOMAINS, is_personal_email


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("alice@gmail.com", True),
        ("alice@googlemail.com", True),
        ("bob@yahoo.com", True),
        ("carol@hotmail.com", True),
        ("dan@outlook.com", True),
        ("erin@icloud.com", True),
        # Modern providers folded in via _EXTRA_PERSONAL_DOMAINS (source predates them).
        ("frank@proton.me", True),
        ("grace@pm.me", True),
        # Case-insensitive (domain is lowercased before lookup).
        ("HEIDI@Gmail.COM", True),
        # Real business / custom domains are not flagged.
        ("ivan@alkera.dev", False),
        ("judy@example.com", False),
        ("ken@acme.io", False),
        # Defensive: a malformed address with no domain is not "personal".
        ("not-an-email", False),
    ],
)
def test_is_personal_email(email: str, expected: bool) -> None:
    assert is_personal_email(email) is expected


def test_is_personal_email_honors_custom_domain_set() -> None:
    # A caller-supplied set overrides the vendored default entirely.
    assert is_personal_email("a@acme.io", personal_domains={"acme.io"}) is True
    # gmail is no longer "personal" under a custom set that omits it.
    assert is_personal_email("a@gmail.com", personal_domains={"acme.io"}) is False


def test_default_personal_email_domains_is_loaded_and_sane() -> None:
    # The vendored snapshot loaded (it's ~3.8k providers); a tiny set means the
    # package data didn't ship / parse.
    assert len(DEFAULT_PERSONAL_EMAIL_DOMAINS) > 1000
    # Headline providers + the code-side modern additions are present.
    for domain in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "proton.me", "pm.me"):
        assert domain in DEFAULT_PERSONAL_EMAIL_DOMAINS
    # Entries are normalized (lowercase) and don't leak comment lines.
    assert all(d == d.lower() for d in DEFAULT_PERSONAL_EMAIL_DOMAINS)
    assert not any(d.startswith("#") for d in DEFAULT_PERSONAL_EMAIL_DOMAINS)
    # A business domain must never be in the default set.
    assert "alkera.dev" not in DEFAULT_PERSONAL_EMAIL_DOMAINS


def test_configured_extras_union_into_the_default_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """A self-hosted PERSONAL_EMAIL_DOMAINS extends the default set (the union
    that used to live in Settings.personal_email_domains_set now lives here)."""
    from alkera_core.config import settings

    # A normally-business domain isn't personal by default...
    assert is_personal_email("a@acme.io") is False
    # ...but becomes personal when configured as an extra (case/space-normalized).
    monkeypatch.setattr(settings, "personal_email_domains", "Acme.IO, partner.example")
    assert is_personal_email("a@acme.io") is True
    assert is_personal_email("b@partner.example") is True
    # The vendored defaults still apply alongside the extras.
    assert is_personal_email("c@gmail.com") is True
