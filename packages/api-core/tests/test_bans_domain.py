"""The one spelling a domain ban is stored and matched under."""

from __future__ import annotations

import pytest
from alkera_core.bans import InvalidDomainError, normalize_domain


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("acme.com", "acme.com", id="plain"),
        pytest.param("ACME.COM", "acme.com", id="lower-cased"),
        pytest.param("  acme.com  ", "acme.com", id="whitespace-stripped"),
        pytest.param("@acme.com", "acme.com", id="leading-at-dropped"),
        pytest.param(" @ ACME.com ", "acme.com", id="at-then-whitespace"),
        pytest.param("mail.sub.acme.co.uk", "mail.sub.acme.co.uk", id="deep"),
        pytest.param("xn--bcher-kva.example", "xn--bcher-kva.example", id="punycode"),
        pytest.param("a-b.c-d.io", "a-b.c-d.io", id="inner-hyphens"),
    ],
)
def test_normalize_domain(raw: str, expected: str) -> None:
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("@", id="at-only"),
        pytest.param("acme", id="lone-label-no-dot"),
        pytest.param("alice@acme.com", id="a-full-address"),
        pytest.param("@@acme.com", id="two-ats"),
        pytest.param("https://acme.com", id="a-url"),
        pytest.param("acme.com/path", id="a-path"),
        pytest.param("acme .com", id="inner-space"),
        pytest.param("-acme.com", id="leading-hyphen"),
        pytest.param("acme-.com", id="trailing-hyphen-label"),
        pytest.param("acme..com", id="empty-label"),
        pytest.param(".acme.com", id="leading-dot"),
        pytest.param("acme.com.", id="trailing-dot"),
        pytest.param("acme_corp.com", id="underscore"),
        pytest.param("a" * 64 + ".com", id="label-over-63"),
        pytest.param(".".join(["a" * 60] * 5), id="over-253-chars"),
    ],
)
def test_normalize_domain_refuses_what_is_not_a_domain(raw: str) -> None:
    with pytest.raises(InvalidDomainError):
        normalize_domain(raw)
