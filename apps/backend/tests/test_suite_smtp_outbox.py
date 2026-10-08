"""The suite never dials an SMTP relay: every send lands in ``smtp_outbox``.

``alkera_core.email._send`` swallows a refused connect, so a test cannot see the
difference between a relay that accepted and none at all; the box's time can.
The root conftest replaces ``aiosmtplib.send`` for every test, and this pins
that the replacement is in place, records what a real sender produces, and
that a test's own fake of the same seam still wins over it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from alkera_core import email

pytestmark = pytest.mark.asyncio


def _member() -> Any:
    return SimpleNamespace(display_name="Member", email="member@alkera.dev", id=uuid4())


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Any attempt to open a connection is recorded and refused."""
    opened: list[tuple[Any, ...]] = []

    async def _refuse(*args: Any, **kwargs: Any) -> Any:
        opened.append(args)
        raise ConnectionRefusedError("no relay in the suite")

    monkeypatch.setattr(asyncio, "open_connection", _refuse)
    return opened


async def test_a_sender_lands_in_the_outbox_and_opens_no_socket(
    smtp_outbox: list[tuple[Any, dict[str, Any]]], no_sockets: list[tuple[Any, ...]]
) -> None:
    await email.send_password_reset(_member(), token="RESET_TOKEN")

    assert no_sockets == []
    assert len(smtp_outbox) == 1
    message, kwargs = smtp_outbox[0]
    assert message["To"] == "member@alkera.dev"
    assert "RESET_TOKEN" in message.as_string()
    assert kwargs["hostname"] == email.settings.smtp_host


async def test_a_test_s_own_fake_of_the_seam_wins(
    smtp_outbox: list[tuple[Any, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    mine: list[str] = []

    async def fake_send(message: Any, **kwargs: Any) -> None:
        mine.append(str(message["To"]))

    monkeypatch.setattr(email.aiosmtplib, "send", fake_send)
    await email.send_password_reset(_member(), token="RESET_TOKEN")

    assert mine == ["member@alkera.dev"]
    assert smtp_outbox == []


async def test_no_email_mode_sends_nothing_anywhere(
    smtp_outbox: list[tuple[Any, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(email.settings, "email_enabled", False)
    await email.send_password_reset(_member(), token="RESET_TOKEN")
    assert smtp_outbox == []
