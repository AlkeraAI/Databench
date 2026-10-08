"""Header-level invariants for the email message builder.

These pin the founders From + Reply-To on the welcome email — the whole point of
the feature — at the message-construction layer, no SMTP required.
"""

from __future__ import annotations

from email.message import EmailMessage

import alkera_core.email as email_module
import pytest
from alkera_core import brand
from alkera_core.config import Settings, settings
from alkera_core.email import _build_message, send_welcome_email
from alkera_core.extensions import ExtensionPoint
from alkera_core.models import User


def test_build_message_defaults_to_branded_no_reply_from() -> None:
    msg = _build_message(to="x@example.com", subject="s", text_body="b")
    assert settings.smtp_from in msg["From"]
    # Default senders set no Reply-To (replies go to the From / no-reply box).
    assert "Reply-To" not in msg


def test_build_message_founders_override_sets_from_and_reply_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pinned per-test (like test_email.py) so ambient env can't leak into the
    # identity under assertion.
    monkeypatch.setattr(settings, "email_founders_from", "founders@example.com")
    monkeypatch.setattr(settings, "email_founders_from_name", "Alkera AI")
    msg = _build_message(
        to="x@example.com",
        subject="Welcome",
        text_body="b",
        from_addr=settings.email_founders_from,
        from_name=settings.email_founders_from_name,
        reply_to=settings.email_founders_from,
    )
    assert settings.email_founders_from in msg["From"]
    assert settings.email_founders_from_name in msg["From"]
    assert msg["Reply-To"] == settings.email_founders_from


def test_founders_address_has_no_built_in_default() -> None:
    # A deployment that sets nothing must not send its welcome email from a domain
    # it does not own.
    assert Settings.model_fields["email_founders_from"].default is None


async def _sent_welcome(monkeypatch: pytest.MonkeyPatch) -> EmailMessage:
    sent: list[EmailMessage] = []

    async def capture(message: EmailMessage, **_: object) -> bool:
        sent.append(message)
        return True

    monkeypatch.setattr(email_module, "_send", capture)
    # As the open build sees it: no product brand registered.
    monkeypatch.setattr(brand, "BRAND", ExtensionPoint("alkera.brand"))
    await send_welcome_email(User(email="new@example.com", first_name="Ada", last_name="L"))
    assert len(sent) == 1
    return sent[0]


@pytest.mark.parametrize(
    "configured", [pytest.param(None, id="unset"), pytest.param("", id="empty")]
)
async def test_welcome_without_a_founders_address_uses_the_deployment_sender(
    monkeypatch: pytest.MonkeyPatch, configured: str | None
) -> None:
    monkeypatch.setattr(settings, "email_founders_from", configured)
    monkeypatch.setattr(settings, "smtp_from", "mail@self-hosted.example")
    monkeypatch.setattr(settings, "brand_support_email", "help@self-hosted.example")
    message = await _sent_welcome(monkeypatch)
    assert "<mail@self-hosted.example>" in message["From"]
    assert message["Reply-To"] == "help@self-hosted.example"


async def test_welcome_with_a_founders_address_sends_and_replies_from_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "email_founders_from", "hello@self-hosted.example")
    monkeypatch.setattr(settings, "smtp_from", "mail@self-hosted.example")
    message = await _sent_welcome(monkeypatch)
    assert "<hello@self-hosted.example>" in message["From"]
    assert message["Reply-To"] == "hello@self-hosted.example"
