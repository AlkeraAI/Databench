"""The digest a saved chat's brief starts from.

The prompt cases are driven from the persisted transcript fixtures rather than
from strings invented here, so a change to what a publisher actually writes
shows up as a digest that no longer quotes it. The reply cases are built with
the real event and part models for the same reason: the digest reads a shape
those models define, and a hand-rolled dict would agree with the reader by
construction instead of with the writer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alkera_core.schemas.chat.events_transcript import PartCreated
from alkera_core.schemas.chat.parts import TextPart
from alkera_core.schemas.objects.template_brief import (
    ANSWERED_LABEL,
    ASKED_LABEL,
    ELLIPSIS,
    REPLY_MAX_CHARS,
    brief_from_transcript,
    heading_for,
    truncation_note,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "objects"
TITLE = "Weekly prompt movement"


@dataclass
class Row:
    """A transcript row, structurally what ``chat_messages`` hands a route."""

    seq: int
    role: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


def _prompt_fixtures() -> list[pytest.param]:
    """Every persisted prompt record in the corpus, newest and oldest alike."""
    found = sorted(FIXTURES.glob("v*/transcript/prompt_record.json"))
    assert found, "the transcript fixture corpus is missing its prompt records"
    return [
        pytest.param(json.loads(path.read_text()), id=f"{path.parent.parent.name}")
        for path in found
    ]


def _prompt_row(payload: dict[str, Any], *, seq: int = 1) -> Row:
    return Row(seq=seq, role="user", kind="prompt", payload=payload)


def _reply_row(text: str, *, seq: int = 2, **part_fields: Any) -> Row:
    part = TextPart(part_id=f"p{seq}", message_id=f"m{seq}", text=text, **part_fields)
    event = PartCreated(
        event_id=f"e{seq}",
        session_id="sess-1",
        time=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
        part=part,
    )
    return Row(
        seq=seq, role="assistant", kind="part.created", payload=event.model_dump(mode="json")
    )


def test_an_empty_transcript_is_the_heading_alone() -> None:
    """A template with no brief beats a brief that describes the wrong thing."""
    assert brief_from_transcript([], title=TITLE) == heading_for(TITLE)


def test_the_heading_names_the_chat_it_came_from() -> None:
    assert TITLE in brief_from_transcript([], title=TITLE)


def test_a_blank_title_still_yields_a_heading() -> None:
    assert brief_from_transcript([], title="   ").startswith("# ")


@pytest.mark.parametrize("payload", _prompt_fixtures())
def test_a_persisted_prompt_is_quoted_verbatim(payload: dict[str, Any]) -> None:
    """Verbatim is the contract: a paraphrased request starts the wrong chat."""
    brief = brief_from_transcript([_prompt_row(payload)], title=TITLE)
    assert payload["text"] in brief
    assert ASKED_LABEL in brief


def test_a_prompt_keeps_its_own_newlines_and_markdown() -> None:
    text = "compare:\n\n- **March**\n- April\n\n…and say which moved."
    brief = brief_from_transcript([_prompt_row({"text": text})], title=TITLE)
    assert text in brief


def test_a_reply_under_the_ceiling_is_kept_whole() -> None:
    text = "x" * (REPLY_MAX_CHARS - 1)
    brief = brief_from_transcript([_reply_row(text)], title=TITLE)
    assert text in brief
    assert ELLIPSIS not in brief
    assert ANSWERED_LABEL in brief


def test_a_reply_over_the_ceiling_is_cut_and_marked() -> None:
    text = "y" * (REPLY_MAX_CHARS * 3)
    brief = brief_from_transcript([_reply_row(text)], title=TITLE)
    body = brief.split(ANSWERED_LABEL, 1)[1].strip()
    assert len(body) == REPLY_MAX_CHARS
    assert body.endswith(ELLIPSIS)
    assert text not in brief


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(
            Row(seq=3, role="tool", kind="tool.call", payload={"text": "SECRET"}), id="tool-role"
        ),
        pytest.param(
            Row(seq=3, role="assistant", kind="tool.call", payload={"text": "SECRET"}),
            id="tool-call-kind",
        ),
        pytest.param(
            Row(seq=3, role="assistant", kind="tool.call_update", payload={"text": "SECRET"}),
            id="tool-update-kind",
        ),
        pytest.param(
            Row(seq=3, role="system", kind="prompt", payload={"text": "SECRET"}), id="system-role"
        ),
        pytest.param(
            Row(seq=3, role="user", kind="part.created", payload={"text": "SECRET"}),
            id="a-user-row-that-is-not-a-prompt",
        ),
    ],
)
def test_a_row_the_digest_does_not_speak_for_never_appears(row: Row) -> None:
    """Tool traffic is the half of a transcript most likely to carry something
    private and the half that says least about what the chat was for."""
    assert "SECRET" not in brief_from_transcript([row], title=TITLE)


@pytest.mark.parametrize(
    "part_fields",
    [
        pytest.param({"synthetic": True}, id="synthetic"),
        pytest.param({"ignored": True}, id="ignored"),
    ],
)
def test_a_part_the_harness_inserted_for_itself_is_not_the_assistant_speaking(
    part_fields: dict[str, Any],
) -> None:
    assert "SECRET" not in brief_from_transcript([_reply_row("SECRET", **part_fields)], title=TITLE)


def test_an_empty_prompt_or_reply_contributes_no_block() -> None:
    rows = [
        _prompt_row({"text": "   "}),
        _reply_row("", seq=2),
        Row(seq=3, role="user", kind="prompt"),
    ]
    assert brief_from_transcript(rows, title=TITLE) == heading_for(TITLE)


def test_the_blocks_run_in_seq_order_whatever_order_they_arrive_in() -> None:
    rows = [
        _reply_row("second", seq=2),
        _prompt_row({"text": "first"}, seq=1),
        _reply_row("fourth", seq=4),
        _prompt_row({"text": "third"}, seq=3),
    ]
    brief = brief_from_transcript(rows, title=TITLE)
    positions = [brief.index(word) for word in ("first", "second", "third", "fourth")]
    assert positions == sorted(positions)


def test_the_cut_names_the_seq_the_digest_stopped_before() -> None:
    rows = [_prompt_row({"text": f"question {n}"}, seq=n) for n in range(1, 21)]
    brief = brief_from_transcript(rows, title=TITLE, max_chars=200)
    assert brief.endswith(truncation_note(6)), brief
    assert "question 5" in brief
    assert "question 6" not in brief


def test_a_cut_digest_still_fits_the_ceiling_it_was_given() -> None:
    """The note is inside the budget, not an overflow past it: a caller who
    sized the ceiling against a column gets something that column holds."""
    rows = [_prompt_row({"text": f"question {n}"}, seq=n) for n in range(1, 400)]
    for ceiling in (120, 200, 512, 1000):
        assert len(brief_from_transcript(rows, title=TITLE, max_chars=ceiling)) <= ceiling


def test_a_transcript_that_fits_carries_no_cut_line() -> None:
    rows = [_prompt_row({"text": "short"}, seq=1), _reply_row("also short", seq=2)]
    brief = brief_from_transcript(rows, title=TITLE)
    assert "(digest truncated" not in brief
    assert "short" in brief and "also short" in brief


def test_the_digest_is_pure() -> None:
    """Same rows in, same text out — the routes that save a template and the
    tests that pin the shape must not be able to disagree."""
    rows = [_prompt_row({"text": "ask"}, seq=1), _reply_row("answer", seq=2)]
    assert brief_from_transcript(rows, title=TITLE) == brief_from_transcript(rows, title=TITLE)
