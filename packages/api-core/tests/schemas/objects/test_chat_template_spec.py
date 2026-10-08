"""A chat template's persisted body: what a reader saved a chat AS.

A template is a starting point, not a record of a run — its brief is prose its
author wrote for whoever starts the next chat, and the model and the stance are
a suggestion the starter narrows. So the interesting properties here are the
ones a starter depends on: the defaults are the safe ones, the brief is bounded,
and a template written by a newer server still loads.
"""

from __future__ import annotations

import pytest
from alkera_core.schemas.objects import ChatModelPin, ChatTemplateSpec
from alkera_core.schemas.objects.specs import MAX_TEMPLATE_BRIEF_LENGTH
from pydantic import ValidationError


def test_a_template_saved_with_nothing_chosen_reads_as_the_safe_defaults() -> None:
    """Every field defaults, so a partial or older row still loads.

    ``read_only`` is the floor: a template that named no stance must not hand
    the chat it starts a wider one than the reader would have got themselves.
    """
    spec = ChatTemplateSpec()
    assert spec.brief == ""
    assert spec.model is None
    assert spec.permission_mode == "read_only"
    assert spec.source_chat_id is None
    assert spec.saved_from_seq == 0
    assert spec.schema_version == ChatTemplateSpec.SCHEMA_VERSION


def test_a_template_carries_the_pin_and_the_transcript_point_it_was_saved_at() -> None:
    pin = ChatModelPin(
        id="claude-opus-4.5",
        display_name="Claude Opus 4.5",
        wire="anthropic",
        efforts=["low", "high"],
        effort="high",
    )
    spec = ChatTemplateSpec(
        brief="Ask which region, then run the weekly rollup.",
        model=pin,
        permission_mode="plan",
        source_chat_id="5f1f0a3c-2f0e-4a2b-9d5c-71a3c9f0b111",
        saved_from_seq=42,
    )
    assert spec.model is not None
    assert spec.model.id == "claude-opus-4.5"
    assert spec.saved_from_seq == 42
    round_tripped = ChatTemplateSpec.model_validate(spec.model_dump(mode="json"))
    assert round_tripped == spec


def test_a_brief_longer_than_the_cap_is_refused() -> None:
    """The brief rides the box's per-turn channel, so it is bounded at the row."""
    ChatTemplateSpec(brief="x" * MAX_TEMPLATE_BRIEF_LENGTH)
    with pytest.raises(ValidationError) as raised:
        ChatTemplateSpec(brief="x" * (MAX_TEMPLATE_BRIEF_LENGTH + 1))
    assert "brief" in str(raised.value)


def test_a_negative_saved_from_seq_is_refused() -> None:
    """A transcript seq counts up from zero; a negative one names no message."""
    with pytest.raises(ValidationError):
        ChatTemplateSpec(saved_from_seq=-1)


def test_a_permission_mode_outside_the_vocabulary_is_refused() -> None:
    with pytest.raises(ValidationError):
        ChatTemplateSpec(permission_mode="root")


def test_a_field_a_newer_writer_added_survives_a_round_trip() -> None:
    """Forward compatibility: an older reader must not drop what it cannot name."""
    written = {
        "schema_version": ChatTemplateSpec.SCHEMA_VERSION,
        "brief": "Weekly rollup",
        "starting_folder": "scratch",
    }
    spec = ChatTemplateSpec.model_validate(written)
    assert spec.model_dump(mode="json")["starting_folder"] == "scratch"


def test_the_brief_cap_is_the_one_the_schema_declares() -> None:
    """A pin, so widening the cap is a deliberate edit rather than a drift."""
    assert MAX_TEMPLATE_BRIEF_LENGTH == 32_000
