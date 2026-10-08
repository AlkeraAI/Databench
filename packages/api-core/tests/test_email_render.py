"""Render tests + golden snapshots for the HTML email templates.

The snapshots under ``fixtures/email/`` pin MJML/MRML output so an engine bump or an
accidental template change is a visible, reviewed diff. Re-bless an intended change with
``make gen-email-snapshots`` (which sets ``UPDATE_EMAIL_SNAPSHOTS=1`` under the hood),
then commit the result. The pr-gate ``drift`` job regenerates + diffs these, so a stale
snapshot fails CI exactly like an un-regenerated SDK.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import alkera_core.email as email_mod
import pytest
from alkera_core.brand import DATABENCH
from alkera_core.email import tokens
from alkera_core.email.render import render_email
from jinja2 import TemplateNotFound, UndefinedError

FIXTURES = Path(__file__).parent / "fixtures" / "email"

# A configured header wordmark, pinned on settings for every test here so the header
# renders deterministically, independent of the ambient environment.
LOGO_URL = "https://app.example.com/email/wordmark.png"


@pytest.fixture(autouse=True)
def _pin_email_brand(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here reads the open product's name, whatever brand the test run's
    composition registered."""
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "brand_email_logo_url", LOGO_URL)
    monkeypatch.setattr(settings, "brand_product_name", DATABENCH.product_name)


# Deterministic contexts (no clock / randomness) so the snapshots are stable.
CASES: dict[str, dict[str, object]] = {
    "invitation": {
        "inviter": "Dana Lee",
        "org_name": "Acme Analytics",
        "team_name": "Data Platform",
        "accept_url": "https://app.example.com/signup?invite=INVITE_TOKEN",
        "expires": "Jul 1, 2026",
        "brand_suffix": " on Databench",
    },
    "verification": {
        "name": "Sam Rivera",
        "verify_url": "https://app.example.com/verify-email/VERIFY_TOKEN",
    },
    "password_reset": {
        "name": "Sam Rivera",
        "reset_url": "https://app.example.com/reset-password/RESET_TOKEN",
    },
    "welcome": {
        "name": "Sam Rivera",
        "app_url": "https://app.example.com",
    },
    "account_notice": {
        "heading": "Hi Sam Rivera,",
        "lines": [
            "Your Databench account is scheduled for deletion on Oct 21, 2026.",
            "Until then you can sign in and cancel from your account settings.",
        ],
        "cta_url": "https://app.example.com/settings/profile#delete-account",
        "cta_label": "Keep my account",
        "footnote": (
            "If you didn't ask for this, sign in, cancel the deletion and change your password."
        ),
    },
}

CASE_IDS = sorted(CASES)


@pytest.mark.parametrize("name", CASE_IDS)
def test_open_brand_template_matches_golden_snapshot(name: str) -> None:
    """The open build's emails: the Databench name and no logo image, since an
    open deployment sets no ``BRAND_EMAIL_LOGO_URL`` by default."""
    html = render_email(
        name, **{**CASES[name], "product_name": DATABENCH.product_name, "logo_url": ""}
    )
    fixture = FIXTURES / "databench" / f"{name}.html"

    if os.environ.get("UPDATE_EMAIL_SNAPSHOTS"):
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(html, encoding="utf-8")
        pytest.skip(f"snapshot updated: databench/{fixture.name}")

    assert fixture.exists(), f"missing snapshot {fixture}; run `make gen-email-snapshots`"
    assert html == fixture.read_text(encoding="utf-8"), (
        f"databench/{name}.html drifted from the golden snapshot. If the change is intended, "
        f"re-render with `make gen-email-snapshots` and commit the result."
    )
    assert "Databench" in html


@pytest.mark.parametrize("name", CASE_IDS)
def test_template_renders_deterministically(name: str) -> None:
    # MRML output must be stable across calls or the snapshot test would be flaky.
    assert render_email(name, **CASES[name]) == render_email(name, **CASES[name])


@pytest.mark.parametrize("name", CASE_IDS)
def test_template_carries_link_and_brand(name: str) -> None:
    html = render_email(name, **CASES[name])
    (url,) = [v for k, v in CASES[name].items() if k.endswith("_url")]
    assert url in html  # the actionable link
    assert tokens.CTA_BG in html  # olive CTA inlined as a literal hex
    # Both web-font families are referenced so a client that honors them upgrades.
    assert "Newsreader" in html
    assert "IBM Plex Sans" in html
    # ...but the email must read in the fallbacks too (the web font loads for few).
    assert "Georgia" in html
    assert "Arial" in html


def test_template_product_name_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    # The de-brand must be REAL, not cosmetic: a custom product name set on settings
    # flows through the injected render context into the output, and the default
    # name disappears entirely. (Guards against a swap that left a hardcoded literal.)
    # Opt out of the logo too (as a text-only white-label would) so the assertion
    # covers the header wordmark as well as the body — no other product's asset URL leaks.
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "brand_product_name", "Acme Data")
    monkeypatch.setattr(settings, "brand_email_logo_url", "")
    html = render_email("welcome", name="", app_url="https://app.example/")
    assert "Welcome to Acme Data" in html
    assert DATABENCH.product_name not in html
    assert "<img" not in html


def test_header_renders_configured_wordmark_image() -> None:
    # With a logo URL configured, the header is an <img> pointing at it, and the alt
    # text is the product name so image-blocking clients still show the brand.
    html = render_email(
        "verification", name="Sam", verify_url="https://app.example.com/v/T", product_name="Acme"
    )
    assert f'src="{LOGO_URL}"' in html
    assert 'alt="Acme"' in html


def test_header_falls_back_to_text_wordmark_without_logo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Self-hosted safety: no configured logo → NO image is referenced (the operator
    # is not hotlinking another deployment's asset), and the header degrades to the product-name
    # text wordmark.
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "brand_email_logo_url", "")
    html = render_email("verification", name="Sam", verify_url="https://app.example.com/v/T")
    assert LOGO_URL not in html
    assert "/email/wordmark.png" not in html
    assert "Databench" in html  # the text wordmark still names the product


@pytest.mark.parametrize("empty", ["", None])
def test_unset_logo_is_treated_as_no_image(
    monkeypatch: pytest.MonkeyPatch, empty: str | None
) -> None:
    # Both the empty-string opt-out and the unset default (None) must be falsy so
    # the template takes the text-wordmark branch — a self-hosted deployment that
    # never sets BRAND_EMAIL_LOGO_URL must not emit an <img> logo.
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "brand_email_logo_url", empty)
    html = render_email("welcome", name="Sam", app_url="https://app.example/")
    assert "wordmark.png" not in html
    assert "<img" not in html


def test_custom_self_hosted_logo_url_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    # A self-hosted operator can point the header at their OWN hosted logo.
    from alkera_core.config import settings

    monkeypatch.setattr(settings, "brand_email_logo_url", "https://cdn.acme.example/logo.png")
    html = render_email("welcome", name="Sam", app_url="https://app.example/")
    assert 'src="https://cdn.acme.example/logo.png"' in html
    assert LOGO_URL not in html


@pytest.mark.parametrize("name", CASE_IDS)
def test_template_is_well_under_gmail_clip(name: str) -> None:
    # Gmail clips messages over ~102KB of HTML bytes; stay comfortably under it.
    assert len(render_email(name, **CASES[name]).encode("utf-8")) < 102_000


def test_invitation_root_org_omits_team_phrase() -> None:
    html = render_email(
        "invitation",
        inviter="An admin",
        org_name="Acme Analytics",
        team_name=None,
        accept_url="https://app.example.com/signup?invite=t",
        expires="Jul 1, 2026",
        brand_suffix=" on Databench",
    )
    assert "organization" in html
    assert "team in" not in html


# --- The invitation's expiry and Subject --------------------------------------------
#
# Both surfaces the QA walk flagged: an expiry printed as a machine timestamp
# ("2026-09-25T23:52:24+00:00") where the portal prints "Sep 25, 2026", and a Subject
# that named the brand twice for an org called after the product. Driven through the
# real sender, because the Subject and the text/plain part never pass through Jinja and
# so are pinned by no snapshot.


async def _send_invitation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    org_name: str = "Acme Analytics",
    expires_at: datetime = datetime(2026, 9, 25, 23, 52, 24, tzinfo=UTC),
) -> EmailMessage:
    """Run the real invitation sender and return the message it handed to SMTP."""
    captured: list[EmailMessage] = []

    async def _capture(message: EmailMessage, **_kw: object) -> None:
        captured.append(message)

    monkeypatch.setattr(email_mod, "_send", _capture)
    await email_mod.send_invitation_email(
        SimpleNamespace(email="invitee@example.com", id=uuid4(), expires_at=expires_at),  # type: ignore[arg-type]
        token="INVITE_TOKEN",
        team=SimpleNamespace(name="Data Platform", is_root=False, id=uuid4()),  # type: ignore[arg-type]
        org_name=org_name,
        inviter_display_name="Dana Lee",
    )
    (message,) = captured
    return message


def _surfaces(message: EmailMessage) -> dict[str, str]:
    """The two bodies, whitespace-collapsed so a sentence MJML wrapped still matches."""
    parts = {}
    for kind in ("plain", "html"):
        body = message.get_body(preferencelist=(kind,))
        assert body is not None, f"the invitation has no text/{kind} part"
        parts[f"text/{kind}"] = " ".join(body.get_content().split())
    return parts


@pytest.mark.asyncio
async def test_invitation_expiry_reads_as_a_date_not_a_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = await _send_invitation(monkeypatch)
    for surface, body in _surfaces(message).items():
        assert "This invitation expires on Sep 25, 2026 at 11:52 PM UTC." in body, surface
        # The machine forms the reader was shown before: neither may survive anywhere.
        assert "2026-09-25T" not in body, surface
        assert "+00:00" not in body, surface
        assert "23:52" not in body, surface


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expires_at", "expected"),
    [
        pytest.param(
            datetime(2026, 9, 25, 23, 52, 24, tzinfo=UTC),
            "Sep 25, 2026 at 11:52 PM UTC",
            id="utc-instant",
        ),
        # "Sep 05" is what a %d format string would print; a person writes "Sep 5".
        pytest.param(
            datetime(2026, 9, 5, 0, 0, tzinfo=UTC), "Sep 5, 2026 at 12:00 AM UTC", id="day-unpadded"
        ),
        # 01:30 in Berlin is the PREVIOUS UTC day: the stored instant decides the
        # day, and the zone it is named in travels with it, so the date a reader
        # west of UTC sees in the portal is never contradicted by a bare day.
        pytest.param(
            datetime(2026, 9, 26, 1, 30, tzinfo=timezone(timedelta(hours=2))),
            "Sep 25, 2026 at 11:30 PM UTC",
            id="offset-zone-folds-to-the-utc-day",
        ),
        pytest.param(
            datetime(2027, 1, 1, 12, 0), "Jan 1, 2027 at 12:00 PM UTC", id="naive-is-read-as-utc"
        ),
    ],
)
async def test_invitation_expiry_names_the_stored_day(
    monkeypatch: pytest.MonkeyPatch, expires_at: datetime, expected: str
) -> None:
    message = await _send_invitation(monkeypatch, expires_at=expires_at)
    for surface, body in _surfaces(message).items():
        assert f"This invitation expires on {expected}." in body, surface


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("org_name", "expected_subject"),
    [
        pytest.param(
            "Acme Analytics",
            "You're invited to Acme Analytics on Databench",
            id="org-unrelated-to-the-brand-still-names-the-product",
        ),
        pytest.param(
            "Databench Dev", "You're invited to Databench Dev", id="org-already-carries-the-brand"
        ),
        pytest.param(
            "databench dev", "You're invited to databench dev", id="match-is-case-insensitive"
        ),
    ],
)
async def test_invitation_subject_names_the_brand_once(
    monkeypatch: pytest.MonkeyPatch, org_name: str, expected_subject: str
) -> None:
    message = await _send_invitation(monkeypatch, org_name=org_name)
    subject = str(message["Subject"])
    assert subject == expected_subject
    assert subject.casefold().count("databench") == 1
    # The preheader sits beside the Subject in the inbox, so it may not stutter either,
    # and the HTML document title is the same line.
    named = expected_subject.removeprefix("You're invited to ")
    html = _surfaces(message)["text/html"]
    assert f"<title>{expected_subject}</title>" in html
    assert f"Dana Lee invited you to join {named}." in html


def test_verification_greeting_degrades_without_name() -> None:
    html = render_email(
        "verification", name="", verify_url="https://app.example.com/verify-email/t"
    )
    assert "Welcome to Databench" in html
    assert "Welcome, " not in html  # no dangling "Welcome, ," for SSO/JIT users


def test_password_reset_greeting_degrades_without_name() -> None:
    html = render_email(
        "password_reset", name="", reset_url="https://app.example.com/reset-password/t"
    )
    assert "Reset your password" in html
    assert "Hi ," not in html


def test_welcome_greeting_degrades_without_name() -> None:
    html = render_email("welcome", name="", app_url="https://app.example.com")
    assert "Welcome to Databench" in html
    assert "Welcome, " not in html  # no dangling "Welcome, ," for SSO/JIT users


def test_render_escapes_user_controlled_content() -> None:
    html = render_email(
        "verification",
        name="<script>alert(1)</script>",
        verify_url="https://app.example.com/verify-email/t",
    )
    assert "<script>alert(1)</script>" not in html  # not injected raw
    assert "&lt;script&gt;" in html  # escaped form is present


def test_render_unknown_template_raises() -> None:
    # The sender's guard relies on this surfacing as an exception it can catch.
    with pytest.raises(TemplateNotFound):
        render_email("does_not_exist", x=1)


def test_render_missing_variable_raises() -> None:
    # StrictUndefined turns a typo'd/absent variable into a render-time error rather
    # than a silently-empty email.
    with pytest.raises(UndefinedError):
        render_email("verification")


async def _sent_text(monkeypatch: pytest.MonkeyPatch, send: object) -> str:
    """The text/plain part of the message a real sender handed to SMTP."""
    captured: list[EmailMessage] = []

    async def _capture(message: EmailMessage, **_kw: object) -> None:
        captured.append(message)

    monkeypatch.setattr(email_mod, "_send", _capture)
    await send  # type: ignore[misc]
    (message,) = captured
    part = message.get_body(preferencelist=("plain",))
    assert part is not None
    return str(part.get_content())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("display_name", "salutation"),
    [
        pytest.param("", "Hi,\n", id="no-name"),
        pytest.param("   ", "Hi,\n", id="blank-name"),
        pytest.param("Sam", "Hi Sam,\n", id="named"),
    ],
)
@pytest.mark.parametrize("sender", ["verification", "password_reset"])
async def test_text_body_greets_without_an_empty_name(
    monkeypatch: pytest.MonkeyPatch, sender: str, display_name: str, salutation: str
) -> None:
    user = SimpleNamespace(email="sam@example.com", display_name=display_name, id=uuid4())
    send = (
        email_mod.send_email_verification(user, token="T")  # type: ignore[arg-type]
        if sender == "verification"
        else email_mod.send_password_reset(user, token="T")  # type: ignore[arg-type]
    )
    text = await _sent_text(monkeypatch, send)
    assert text.startswith(salutation)
    assert "Hi ," not in text
