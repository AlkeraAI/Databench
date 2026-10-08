"""From-header branding for outbound email.

The three senders monkeypatched to no-ops elsewhere never exercise the From
header, so these tests pin it directly: every message must carry the display
name (`Alkera AI <no-reply@…>`) and degrade to a bare address when the name is
empty. Both `smtp_from` and `smtp_from_name` are pinned per-test so a dev's
local `.env` (which may override `SMTP_FROM`) can't leak in.
"""

from __future__ import annotations

from email.message import EmailMessage
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alkera_core import email
from alkera_core.config import settings
from alkera_core.email import _build_message, send_password_reset


@pytest.fixture(autouse=True)
def _pin_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "smtp_from", "no-reply@example.com")
    monkeypatch.setattr(settings, "smtp_from_name", "Alkera AI")


def test_build_message_sets_branded_from():
    message = _build_message(to="user@example.com", subject="Hi", text_body="Hello\n")

    assert message["From"] == "Alkera AI <no-reply@example.com>"
    assert message["To"] == "user@example.com"
    assert message["Subject"] == "Hi"
    assert message.get_content() == "Hello\n"


def test_build_message_falls_back_to_bare_address(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "smtp_from_name", "")

    message = _build_message(to="user@example.com", subject="Hi", text_body="Hi\n")

    assert message["From"] == "no-reply@example.com"


def test_build_message_quotes_names_with_special_chars(monkeypatch: pytest.MonkeyPatch):
    # formataddr quotes a display name containing RFC 5322 specials (here a comma).
    monkeypatch.setattr(settings, "smtp_from_name", "Alkera, AI")

    message = _build_message(to="user@example.com", subject="Hi", text_body="Hi\n")

    assert message["From"] == '"Alkera, AI" <no-reply@example.com>'


@pytest.mark.asyncio
async def test_send_password_reset_uses_branded_from(monkeypatch: pytest.MonkeyPatch):
    captured: list[EmailMessage] = []

    async def fake_send(message: EmailMessage, **_kwargs: object) -> None:
        captured.append(message)

    monkeypatch.setattr(email.aiosmtplib, "send", fake_send)

    user = SimpleNamespace(id=uuid4(), email="reset@example.com", display_name="Reset User")
    await send_password_reset(user, token="tok-123")  # type: ignore[arg-type]

    assert len(captured) == 1
    assert captured[0]["From"] == "Alkera AI <no-reply@example.com>"
    assert captured[0]["To"] == "reset@example.com"


def test_build_message_html_body_makes_multipart_alternative():
    message = _build_message(
        to="user@example.com", subject="Hi", text_body="plain\n", html_body="<p>rich</p>"
    )

    assert message.is_multipart()
    assert message.get_content_type() == "multipart/alternative"
    parts = message.get_payload()
    assert isinstance(parts, list) and len(parts) == 2
    # MIME orders alternatives least-to-most preferred: text first, then HTML.
    assert parts[0].get_content_type() == "text/plain"
    assert parts[1].get_content_type() == "text/html"
    assert parts[0].get_content() == "plain\n"
    assert "<p>rich</p>" in parts[1].get_content()
    # utf-8 (not base64) keeps the part small — bytes matter for Gmail's ~102KB clip.
    assert parts[1].get_content_charset() == "utf-8"


@pytest.mark.asyncio
async def test_no_email_mode_skips_the_send(monkeypatch: pytest.MonkeyPatch):
    # EMAIL_ENABLED=false → the send path is a logged no-op and SMTP is never touched
    # (a no-email / air-gapped deployment runs with no relay).
    monkeypatch.setattr(settings, "email_enabled", False)
    called = False

    async def fake_send(message: EmailMessage, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(email.aiosmtplib, "send", fake_send)

    user = SimpleNamespace(id=uuid4(), email="reset@example.com", display_name="Reset User")
    await send_password_reset(user, token="tok-123")  # type: ignore[arg-type]

    assert called is False


@pytest.mark.asyncio
async def test_anonymous_relay_passes_no_auth(monkeypatch: pytest.MonkeyPatch):
    # An EMPTY username/password (what compose's `${SMTP_USERNAME:-}` default yields for
    # an internal no-auth relay) must reach aiosmtplib as None, not "" — otherwise it
    # ATTEMPTS auth and fails "AUTH not supported" on an anonymous relay.
    monkeypatch.setattr(settings, "smtp_username", "")
    monkeypatch.setattr(settings, "smtp_password", "")
    captured: dict[str, object] = {}

    async def fake_send(message: EmailMessage, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(email.aiosmtplib, "send", fake_send)
    user = SimpleNamespace(id=uuid4(), email="reset@example.com", display_name="Reset User")
    await send_password_reset(user, token="tok-123")  # type: ignore[arg-type]

    assert captured["username"] is None
    assert captured["password"] is None


def test_build_message_without_html_stays_single_text_part():
    message = _build_message(to="user@example.com", subject="Hi", text_body="plain\n")

    assert not message.is_multipart()
    assert message.get_content_type() == "text/plain"
    assert message.get_content() == "plain\n"


@pytest.mark.asyncio
async def test_send_password_reset_attaches_html_with_reset_link(monkeypatch: pytest.MonkeyPatch):
    captured: list[EmailMessage] = []

    async def fake_send(message: EmailMessage, **_kwargs: object) -> None:
        captured.append(message)

    monkeypatch.setattr(email.aiosmtplib, "send", fake_send)

    user = SimpleNamespace(id=uuid4(), email="reset@example.com", display_name="Reset User")
    await send_password_reset(user, token="tok-123")  # type: ignore[arg-type]

    message = captured[0]
    assert message.is_multipart()
    text_part, html_part = message.get_payload()
    html = html_part.get_content()
    # The link is the whole point — it must survive into BOTH parts.
    assert "reset-password/tok-123" in html
    assert "reset-password/tok-123" in text_part.get_content()
    assert "Hi Reset User" in html


@pytest.mark.asyncio
async def test_sender_degrades_to_text_only_when_render_fails(monkeypatch: pytest.MonkeyPatch):
    # A render failure must NEVER raise out of the sender: the public password-reset
    # route returns a constant response on purpose so it can't be used to probe which
    # emails have accounts. So a broken template degrades to a text-only email.
    def boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("template exploded")

    monkeypatch.setattr(email, "render_email", boom)

    captured: list[EmailMessage] = []

    async def fake_send(message: EmailMessage, **_kwargs: object) -> None:
        captured.append(message)

    monkeypatch.setattr(email.aiosmtplib, "send", fake_send)

    user = SimpleNamespace(id=uuid4(), email="reset@example.com", display_name="Reset User")
    await send_password_reset(user, token="tok-123")  # type: ignore[arg-type]

    message = captured[0]
    assert not message.is_multipart()  # degraded to text-only, no HTML part
    assert "reset-password/tok-123" in message.get_content()
