"""A turn that ends with nothing on screen says why.

The walkthrough's model finished on ``content-filter`` with 0 output tokens: the
turn went idle and the transcript showed nothing. These drive a real session over
a scripted FakeAdapter, so the sentence is proven to reach what a caller reads
(the turn's final text) and the persisted transcript, and they pin the cases the
watch must leave alone: an answer the model did give, a turn that ran a tool, a
continuation, and an error with its own notice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from _adapter_factory import fake_runtime
from alkera_cli.chat.headless import run_headless
from alkera_cli.harness._fake import FakeAdapter, ScriptedMessage, ScriptedTurn
from alkera_cli.harness.empty_answer import (
    CUT_OFF,
    DECLINED,
    EmptyAnswerWatch,
    empty_answer_sentence,
)
from alkera_core.schemas.chat import (
    MessageCompleted,
    MessageCreated,
    PartCreated,
    TextPart,
    ToolCall,
)

_T = datetime(2026, 9, 24, tzinfo=UTC)


def _persisted(root: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.jsonl"))


def _turn(*messages: ScriptedMessage) -> ScriptedTurn:
    return ScriptedTurn(messages=messages)


@pytest.mark.parametrize(
    ("finish", "sentence"),
    [
        pytest.param("content-filter", DECLINED, id="content-filter"),
        pytest.param("length", CUT_OFF, id="length"),
        pytest.param("other", "The model returned no answer (other).", id="other"),
    ],
)
async def test_a_blank_turn_leaves_one_sentence_naming_why(
    tmp_path: Path, finish: str, sentence: str
) -> None:
    runtime, _factory = fake_runtime(
        tmp_path,
        lambda: FakeAdapter(scripted_turns=[_turn(ScriptedMessage(text="", finish_reason=finish))]),
    )
    result = await run_headless(tmp_path, ["tell me"], runtime=runtime)
    assert result.final_text == sentence
    assert sentence in _persisted(tmp_path), "the sentence is on the persisted transcript"


async def test_an_answer_the_model_gave_is_left_alone(tmp_path: Path) -> None:
    runtime, _factory = fake_runtime(
        tmp_path,
        lambda: FakeAdapter(
            scripted_turns=[_turn(ScriptedMessage(text="half an answer", finish_reason="length"))]
        ),
    )
    result = await run_headless(tmp_path, ["tell me"], runtime=runtime)
    assert result.final_text == "half an answer"


def _events(*, text: str = "", finish: str = "stop", tool: bool = False) -> list[object]:
    out: list[object] = [
        MessageCreated(event_id="u", time=_T, session_id="s", message_id="mu", role="user"),
        MessageCreated(event_id="c", time=_T, session_id="s", message_id="m1", role="assistant"),
    ]
    if tool:
        out.append(
            ToolCall(
                event_id="t",
                time=_T,
                session_id="s",
                tool_call_id="call-1",
                message_id="m1",
                tool_name="bash",
                input={"command": "ls"},
            )
        )
    if text:
        out.append(
            PartCreated(
                event_id="p",
                time=_T,
                session_id="s",
                part=TextPart(part_id="p1", message_id="m1", text=text),
            )
        )
    out.append(
        MessageCompleted(
            event_id="d", time=_T, session_id="s", message_id="m1", finish_reason=finish
        )
    )
    return out


def _added(events: list[object]) -> list[str]:
    watch = EmptyAnswerWatch()
    said: list[str] = []
    for event in events:
        for added in watch.precede(event):  # type: ignore[arg-type]
            assert isinstance(added, PartCreated) and isinstance(added.part, TextPart)
            assert added.part.message_id == "m1", "the sentence is the assistant's message"
            said.append(added.part.text)
    return said


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param({"finish": "content-filter"}, [DECLINED], id="blank-filter"),
        pytest.param({"finish": "content-filter", "tool": True}, [], id="ran-a-tool"),
        pytest.param({"finish": "stop", "text": "done"}, [], id="answered"),
        pytest.param({"finish": "tool-calls"}, [], id="continuation"),
        pytest.param({"finish": "error"}, [], id="error-has-its-own-notice"),
        pytest.param({"finish": ""}, [], id="no-reason-reported"),
        pytest.param(
            {"finish": "stop", "text": "   "},
            ["The model returned no answer (stop)."],
            id="whitespace",
        ),
    ],
)
def test_the_watch_speaks_only_for_a_blank_final_message(
    kwargs: dict[str, object], expected: list[str]
) -> None:
    assert _added(_events(**kwargs)) == expected  # type: ignore[arg-type]


def test_a_user_message_opens_a_new_turn_for_the_watch() -> None:
    watch = EmptyAnswerWatch()
    first = _events(finish="stop", text="done")
    second = _events(finish="content-filter")
    said = [a for e in [*first, *second] for a in watch.precede(e)]  # type: ignore[arg-type]
    assert [a.part.text for a in said] == [DECLINED]  # type: ignore[attr-defined]


@pytest.mark.parametrize("reason", ["content_filter", "refusal", "CONTENT-FILTER"])
def test_every_spelling_of_a_decline_reads_the_same(reason: str) -> None:
    assert empty_answer_sentence(reason) == DECLINED
