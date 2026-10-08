"""Unit tests for the email_domain helper."""

from __future__ import annotations

import pytest
from alkera_core.utils import email_domain


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("user@example.com", "example.com"),
        ("User.Name+tag@Example.COM", "example.com"),  # lowercased, plus-addressing
        ("a@sub.domain.co.uk", "sub.domain.co.uk"),  # subdomains preserved
        ('"weird@local"@example.com', "example.com"),  # quoted local part — last @ wins
        ("  spaced@example.com  ", "example.com"),  # surrounding whitespace tolerated
        ("no-at-sign", ""),  # defensive: no domain
        ("", ""),
    ],
)
def test_email_domain(email: str, expected: str) -> None:
    assert email_domain(email) == expected
