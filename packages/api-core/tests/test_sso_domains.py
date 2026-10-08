"""The pure half of the SSO domain owner: what counts as a domain. The
database half (one org per domain, assignment, release) is driven through the
routes in ``apps/backend/tests/test_sso_domain_claims.py``."""

from __future__ import annotations

import pytest
from alkera_core.auth import sso_domains


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        pytest.param(["acme.com"], ("acme.com",), id="one"),
        pytest.param([" Acme.COM ", "acme.io "], ("acme.com", "acme.io"), id="trimmed-lowercased"),
        pytest.param(["acme.com.", "acme.com"], ("acme.com",), id="trailing-dot-and-repeat"),
        pytest.param(["b.example", "a.example"], ("b.example", "a.example"), id="order-kept"),
        pytest.param(["mail.eu.acme.co.uk"], ("mail.eu.acme.co.uk",), id="deep-subdomain"),
        pytest.param(["xn--bcher-kva.example"], ("xn--bcher-kva.example",), id="punycode"),
        pytest.param([], (), id="none"),
        pytest.param(["", "  "], (), id="only-blanks"),
    ],
)
def test_parse_domains(values: list[str], expected: tuple[str, ...]) -> None:
    assert sso_domains.parse_domains(values) == expected


@pytest.mark.parametrize(
    ("values", "bad"),
    [
        pytest.param(["localhost"], "localhost", id="one-label"),
        pytest.param(["acme.com", "*.acme.com"], "*.acme.com", id="wildcard"),
        pytest.param(["@acme.com"], "@acme.com", id="at-sign"),
        pytest.param(["ac me.com"], "ac me.com", id="space"),
        pytest.param(["acme.com,acme.io"], "acme.com,acme.io", id="a-comma-list-is-one-bad-value"),
        pytest.param(["-acme.com"], "-acme.com", id="leading-hyphen"),
        pytest.param(["acme-.com"], "acme-.com", id="trailing-hyphen"),
        pytest.param(["acme..com"], "acme..com", id="empty-label"),
        pytest.param(["acme.c"], "acme.c", id="one-letter-tld"),
        pytest.param(["https://acme.com"], "https://acme.com", id="url"),
        pytest.param(["a" * 64 + ".com"], "a" * 64 + ".com", id="label-over-63"),
        pytest.param([".".join(["a" * 60] * 5)], ".".join(["a" * 60] * 5), id="name-over-253"),
    ],
)
def test_parse_domains_refuses_what_is_not_a_domain(values: list[str], bad: str) -> None:
    with pytest.raises(sso_domains.InvalidDomainError) as exc:
        sso_domains.parse_domains(values)
    assert exc.value.value == bad
