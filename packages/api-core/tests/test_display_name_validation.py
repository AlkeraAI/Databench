"""The display-name policy: what a human-typed name may contain.

These names are embedded in outbound email prose and subject lines, where a mail
client turns URL-shaped text into a live link on a message carrying our own
sending domain. The write path refuses such a name (`validate_display_name`) and
the render path repairs one already stored (`scrub_display_name`) — so both
directions are pinned here, together with the false-positive cases the policy
must NOT touch.
"""

from __future__ import annotations

import pytest
from alkera_core.validation.display_name import (
    LINK_PLACEHOLDER,
    MAX_DISPLAY_NAME_LENGTH,
    DisplayNameError,
    normalize_display_name,
    scrub_display_name,
    validate_display_name,
)

# The literal payloads from the external assessment and the two emailed reports.
# They are the regression net: if any of these is ever accepted again, an
# attacker-controlled link ships inside an Alkera-branded invitation.
REPORTED_PAYLOADS = [
    pytest.param(
        "Security Compliance Portal (Please verify your account at "
        "https://malicious.evil/login before accepting)",
        id="pentest-01-invitation-phish",
    ),
    pytest.param("http://attacker.com/", id="reported-org-name-url"),
    pytest.param("strawberry://settings/team", id="reported-custom-scheme"),
    pytest.param("Team <h1>HTML_tags</h1>", id="pentest-07-markup"),
]


@pytest.mark.parametrize("payload", REPORTED_PAYLOADS)
def test_reported_payloads_are_refused(payload: str) -> None:
    with pytest.raises(DisplayNameError):
        validate_display_name(payload)


@pytest.mark.parametrize("payload", REPORTED_PAYLOADS)
def test_reported_payloads_carry_no_live_link_after_scrub(payload: str) -> None:
    """A row written BEFORE the policy existed still has to render — it just may
    not render anything a client will linkify."""
    scrubbed = scrub_display_name(payload)
    assert "://" not in scrubbed
    assert "<" not in scrubbed and ">" not in scrubbed
    for domain in ("malicious.evil", "attacker.com"):
        assert domain not in scrubbed


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("https://evil.example", id="https-scheme"),
        pytest.param("HTTP://EVIL.EXAMPLE", id="uppercase-scheme"),
        pytest.param("ftp://files.evil.example", id="ftp-scheme"),
        pytest.param("javascript://x", id="javascript-scheme"),
        pytest.param("www.evil.example", id="www-prefix"),
        pytest.param("Acme (www.evil.example)", id="www-mid-string"),
        pytest.param("evil.example/login", id="host-with-path"),
        pytest.param("billing@alkera-support.example", id="email-address"),
        pytest.param("<script>alert(1)</script>", id="script-tag"),
        pytest.param("Acme <b>Corp", id="stray-open-angle"),
    ],
)
def test_linkable_and_markup_shapes_are_refused(payload: str) -> None:
    with pytest.raises(DisplayNameError):
        validate_display_name(payload)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("Acme.io", id="bare-dotted-company-name"),
        pytest.param("Müller GmbH", id="non-ascii-letters"),
        pytest.param("Ben & Jerry's", id="ampersand-and-apostrophe"),
        pytest.param("Data Platform — Core", id="em-dash"),
        pytest.param("R&D (EMEA)", id="parentheses"),
        pytest.param("Team 42", id="digits"),
        pytest.param("北京数据团队", id="cjk"),
        pytest.param("x" * MAX_DISPLAY_NAME_LENGTH, id="exactly-at-the-length-cap"),
    ],
)
def test_ordinary_names_are_accepted(payload: str) -> None:
    """The asymmetric half: a policy that refuses real org names is a policy
    nobody ships. `Acme.io` in particular is a knowingly-accepted residual —
    banning bare dotted tokens would reject a large share of real startups."""
    assert validate_display_name(payload) == payload


def test_over_length_is_refused() -> None:
    with pytest.raises(DisplayNameError, match="longer than"):
        validate_display_name("x" * (MAX_DISPLAY_NAME_LENGTH + 1))


def test_link_reason_wins_over_length_reason() -> None:
    """A payload that is both over-long AND link-bearing must be told about the
    link — the reason it will still be refused after being shortened."""
    with pytest.raises(DisplayNameError, match="web address"):
        validate_display_name("https://evil.example/" + "x" * MAX_DISPLAY_NAME_LENGTH)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("  Acme   Corp  ", "Acme Corp", id="collapse-and-strip"),
        pytest.param("Acme\tCorp", "Acme Corp", id="tab-becomes-space"),
        pytest.param("Acme\nCorp", "Acme Corp", id="newline-becomes-space"),
        pytest.param("Acme​Corp", "AcmeCorp", id="zero-width-space-dropped"),
        pytest.param("Acme‮Corp", "AcmeCorp", id="bidi-override-dropped"),
        pytest.param("Acme\x00Corp", "AcmeCorp", id="nul-dropped"),
    ],
)
def test_normalization(raw: str, expected: str) -> None:
    assert normalize_display_name(raw) == expected


def test_normalization_is_applied_by_validate() -> None:
    """The NORMALIZED value is what a caller gets back, so a name is persisted
    exactly as it will later render."""
    assert validate_display_name("  Acme ​  Corp ") == "Acme Corp"


@pytest.mark.parametrize("blank", ["", "   ", "​", "\n\t"])
def test_blank_after_normalization_is_refused(blank: str) -> None:
    with pytest.raises(DisplayNameError, match="empty"):
        validate_display_name(blank)


def test_field_label_appears_in_the_message() -> None:
    """The 422 has to say WHICH box to fix."""
    with pytest.raises(DisplayNameError, match=r"^Organization name"):
        validate_display_name("http://x.example", field="Organization name")


class TestScrub:
    def test_clean_name_passes_through_unchanged(self) -> None:
        assert scrub_display_name("Acme.io") == "Acme.io"

    def test_none_and_empty_become_empty(self) -> None:
        assert scrub_display_name(None) == ""
        assert scrub_display_name("   ") == ""

    def test_url_becomes_a_visible_placeholder(self) -> None:
        """Visible, not silent: a phrase that simply vanished reads as a
        rendering bug, and the recipient loses the fact that something was
        removed."""
        assert scrub_display_name("Verify at https://evil.example/login now") == (
            f"Verify at {LINK_PLACEHOLDER} now"
        )

    def test_whole_tags_are_removed_not_just_their_brackets(self) -> None:
        """Dropping only the angle brackets would leave the debris
        `h1HTML_tags/h1` in the recipient's inbox."""
        assert scrub_display_name("Team <h1>HTML_tags</h1>") == "Team HTML_tags"

    def test_long_value_is_truncated(self) -> None:
        out = scrub_display_name("y" * 500)
        assert len(out) == MAX_DISPLAY_NAME_LENGTH
        assert out.endswith("…")

    def test_scrubbed_output_satisfies_the_write_policy(self) -> None:
        """The whole point of the render path: whatever it emits is something
        the write path would have accepted."""
        for payload in ("https://evil.example/x", "Team <h1>x</h1>", "www.evil.example"):
            assert validate_display_name(scrub_display_name(payload))
