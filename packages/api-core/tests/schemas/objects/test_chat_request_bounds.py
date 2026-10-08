"""The bounds on a chat request body that a deployment owns.

The schema publishes an absolute ceiling a generated client can see. Beside it
sits the one an operator sets, read at validation time, which only ever tightens
below the published figure. These cases pin that the configured number — not the
constant — is what a request is judged against.
"""

from __future__ import annotations

import pytest
from alkera_core.config import settings
from alkera_core.schemas.objects.api import (
    MAX_ANSWER_CHOICES,
    MAX_MESSAGE_ATTACHMENTS,
    MAX_REFUSAL_REASON_LENGTH,
    ChatInterruptAnswer,
    ChatMessageCreate,
    ChatPublisherStateUpdate,
)
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("configured", "named", "accepted"),
    [
        pytest.param(3, 3, True, id="exactly-the-configured-ceiling"),
        pytest.param(3, 4, False, id="one-over-the-configured-ceiling"),
        pytest.param(20, 12, True, id="default-admits-a-normal-send"),
        pytest.param(None, MAX_MESSAGE_ATTACHMENTS, True, id="unset-falls-back-to-the-published"),
    ],
)
def test_how_many_files_one_message_may_name_is_the_configured_number(
    monkeypatch: pytest.MonkeyPatch, configured: int | None, named: int, accepted: bool
) -> None:
    """Naming a file that is already linked is a relay, not a transfer, so the
    cost this bounds is authorization decisions per request — a figure a
    deployment sizes against its own directory, not one baked into a release."""
    monkeypatch.setattr(settings, "chat_message_max_attachments", configured)
    body = {"text": "hello", "client_id": "c", "attachments": [f"n{i}" for i in range(named)]}
    if accepted:
        assert len(ChatMessageCreate(**body).attachments) == named
        return
    with pytest.raises(ValidationError, match=f"at most {configured} attachments"):
        ChatMessageCreate(**body)


@pytest.mark.parametrize(
    ("configured", "chosen", "accepted"),
    [
        pytest.param(2, 2, True, id="exactly-the-configured-ceiling"),
        pytest.param(2, 3, False, id="one-over-the-configured-ceiling"),
        pytest.param(32, 10, True, id="default-admits-a-real-answer"),
        # A setting above the published absolute may not widen it: the schema is
        # what a generated client was built against.
        pytest.param(10_000, MAX_ANSWER_CHOICES + 1, False, id="cannot-exceed-the-published"),
    ],
)
def test_how_many_choices_one_prompt_may_carry_is_the_configured_number(
    monkeypatch: pytest.MonkeyPatch, configured: int, chosen: int, accepted: bool
) -> None:
    monkeypatch.setattr(settings, "chat_answer_max_choices", configured)
    body = {"interrupt_id": "i", "answers": [[f"a{i}" for i in range(chosen)]]}
    if accepted:
        assert ChatInterruptAnswer(**body).answers == [[f"a{i}" for i in range(chosen)]]
        return
    with pytest.raises(ValidationError, match="answers"):
        ChatInterruptAnswer(**body)


@pytest.mark.parametrize(
    ("configured", "written", "kept"),
    [
        pytest.param(40, 40, 40, id="exactly-the-configured-ceiling"),
        pytest.param(40, 4000, 40, id="a-long-reason-keeps-its-head"),
        pytest.param(500, 900, 500, id="default-keeps-five-hundred"),
        # The setting cannot widen past what the schema publishes.
        pytest.param(10_000, 4000, MAX_REFUSAL_REASON_LENGTH, id="clipped-to-the-published"),
    ],
)
def test_a_long_refusal_reason_is_cut_rather_than_rejected(
    monkeypatch: pytest.MonkeyPatch, configured: int, written: int, kept: int
) -> None:
    """A refusal is the only thing that explains an empty chat. Refusing the
    report because its reason ran long left the banner saying nothing at all,
    which is strictly worse than saying the first part of why."""
    monkeypatch.setattr(settings, "chat_refusal_reason_max_chars", configured)
    update = ChatPublisherStateUpdate(state="refused", reason="x" * written)
    assert update.reason == "x" * min(written, kept)
