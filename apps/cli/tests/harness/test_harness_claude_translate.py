"""Unit tests for the Claude Agent SDK → IR translator.

Mirrors ``test_harness_opencode_translate.py``: construct SDK message objects (+
raw Anthropic ``StreamEvent`` frames), feed them through a bare translator, and
assert the IR output + cross-event state transitions. No subprocess, no SDK
client — the translator is pure over the typed messages.

Wire shapes pinned against ``claude-agent-sdk`` ``types.py`` + the Anthropic
Messages streaming events. When upstream drifts, this file breaks first.
"""

from __future__ import annotations

from typing import Any

import pytest
from alkera_cli.harness.adapters.claude_translate import (
    ClaudeEventTranslator,
    _ClaudeTranslatorContext,
    _OpenQuery,
)
from alkera_core.schemas.chat import (
    AgentMessageChunk,
    AgentThoughtChunk,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PartStarted,
    RawEvent,
    ReasoningPart,
    SessionStatusChanged,
    TextPart,
    ToolCall,
    ToolCallUpdate,
    TurnFinished,
)
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(*sent: str) -> _ClaudeTranslatorContext:
    """A context with one open query per id in ``sent``, what the adapter's
    ``send_prompt`` appends before each write, oldest first."""
    ctx = _ClaudeTranslatorContext(session_id="our-sid")
    ctx.open_queries.extend(_OpenQuery(turn_id) for turn_id in sent)
    return ctx


def _tr(ctx: _ClaudeTranslatorContext | None = None) -> ClaudeEventTranslator:
    return ClaudeEventTranslator(ctx or _ctx())


def _se(event: dict[str, Any]) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="cc-sid", event=event)


def _result(**kw: Any) -> ResultMessage:
    base: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 1,
        "session_id": "cc-sid",
    }
    base.update(kw)
    return ResultMessage(**base)


def _stream_text(
    tr: ClaudeEventTranslator, *, message_id: str = "msg1", text: str = "Hello"
) -> list[Any]:
    out: list[Any] = []
    out += tr.translate(
        _se({"type": "message_start", "message": {"id": message_id, "usage": {"input_tokens": 5}}})
    )
    out += tr.translate(
        _se(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        )
    )
    for ch in text:
        out += tr.translate(
            _se(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": ch},
                }
            )
        )
    out += tr.translate(_se({"type": "content_block_stop", "index": 0}))
    out += tr.translate(
        _se(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 3},
            }
        )
    )
    out += tr.translate(_se({"type": "message_stop"}))
    return out


# ---------------------------------------------------------------------------
# Streaming: message + text part lifecycle
# ---------------------------------------------------------------------------


def test_message_start_emits_message_created() -> None:
    tr = _tr()
    out = tr.translate(_se({"type": "message_start", "message": {"id": "msg1"}}))
    assert len(out) == 1
    assert isinstance(out[0], MessageCreated)
    assert out[0].message_id == "msg1"
    assert out[0].role == "assistant"


def test_message_start_without_id_drops() -> None:
    assert _tr().translate(_se({"type": "message_start", "message": {}})) == []


def test_duplicate_message_start_does_not_re_announce() -> None:
    tr = _tr()
    tr.translate(_se({"type": "message_start", "message": {"id": "msg1"}}))
    out = tr.translate(_se({"type": "message_start", "message": {"id": "msg1"}}))
    assert out == []


def test_text_block_start_emits_part_started() -> None:
    ctx = _ctx()
    tr = _tr(ctx)
    tr.translate(_se({"type": "message_start", "message": {"id": "msg1"}}))
    out = tr.translate(
        _se(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        )
    )
    assert len(out) == 1
    started = out[0]
    assert isinstance(started, PartStarted)
    assert started.part_type == "text"
    assert started.part_id == "msg1:0"
    assert started.message_id == "msg1"
    assert "msg1:0" in ctx.open_parts


def test_text_delta_emits_message_chunk_with_sequence() -> None:
    ctx = _ctx()
    tr = _tr(ctx)
    tr.translate(_se({"type": "message_start", "message": {"id": "msg1"}}))
    tr.translate(
        _se({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
    )
    out1 = tr.translate(
        _se(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "He"},
            }
        )
    )
    out2 = tr.translate(
        _se(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "llo"},
            }
        )
    )
    assert isinstance(out1[0], AgentMessageChunk)
    assert out1[0].sequence == 0
    assert out1[0].text == "He"
    assert out2[0].sequence == 1
    assert out2[0].text == "llo"


def test_text_block_stop_finalizes_with_concatenated_text() -> None:
    ctx = _ctx()
    tr = _tr(ctx)
    out = _stream_text(tr, text="Hello")
    created = [e for e in out if isinstance(e, PartCreated)]
    assert len(created) == 1
    part = created[0].part
    assert isinstance(part, TextPart)
    assert part.text == "Hello"
    assert part.part_id == "msg1:0"
    assert ctx.open_parts == {}  # closed


def test_full_text_stream_event_order() -> None:
    tr = _tr()
    out = _stream_text(tr, text="Hi")
    types = [type(e).__name__ for e in out]
    # MessageCreated, PartStarted, chunk, chunk, PartCreated
    assert types == [
        "MessageCreated",
        "PartStarted",
        "AgentMessageChunk",
        "AgentMessageChunk",
        "PartCreated",
    ]


# ---------------------------------------------------------------------------
# Streaming: reasoning / thinking
# ---------------------------------------------------------------------------


def test_thinking_block_emits_reasoning_part_and_thought_chunks() -> None:
    ctx = _ctx()
    tr = _tr(ctx)
    tr.translate(_se({"type": "message_start", "message": {"id": "m"}}))
    started = tr.translate(
        _se({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}})
    )
    assert isinstance(started[0], PartStarted)
    assert started[0].part_type == "reasoning"
    chunk = tr.translate(
        _se(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "ponder"},
            }
        )
    )
    assert isinstance(chunk[0], AgentThoughtChunk)
    assert chunk[0].text == "ponder"
    # signature accumulates onto the open part, emits nothing
    sig = tr.translate(
        _se(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig123"},
            }
        )
    )
    assert sig == []
    out = tr.translate(_se({"type": "content_block_stop", "index": 0}))
    part = out[0].part
    assert isinstance(part, ReasoningPart)
    assert part.text == "ponder"
    assert part.signature == "sig123"


# ---------------------------------------------------------------------------
# Streaming: tool use
# ---------------------------------------------------------------------------


def test_tool_use_block_start_emits_tool_call_pending() -> None:
    tr = _tr()
    tr.translate(_se({"type": "message_start", "message": {"id": "m"}}))
    out = tr.translate(
        _se(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}},
            }
        )
    )
    assert isinstance(out[0], ToolCall)
    assert out[0].tool_call_id == "toolu_1"
    assert out[0].tool_name == "Bash"
    assert out[0].status == "pending"


def test_tool_use_input_json_delta_accumulates_and_finalizes() -> None:
    tr = _tr()
    tr.translate(_se({"type": "message_start", "message": {"id": "m"}}))
    tr.translate(
        _se(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash"},
            }
        )
    )
    # input streamed in two json fragments
    assert (
        tr.translate(
            _se(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"command":'},
                }
            )
        )
        == []
    )
    assert (
        tr.translate(
            _se(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '"ls"}'},
                }
            )
        )
        == []
    )
    out = tr.translate(_se({"type": "content_block_stop", "index": 0}))
    assert isinstance(out[0], ToolCallUpdate)
    assert out[0].tool_call_id == "toolu_1"
    assert out[0].status == "running"
    assert out[0].input == {"command": "ls"}


def test_tool_use_unparseable_json_yields_empty_input() -> None:
    tr = _tr()
    tr.translate(_se({"type": "message_start", "message": {"id": "m"}}))
    tr.translate(
        _se(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "t", "name": "Bash"},
            }
        )
    )
    tr.translate(
        _se(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{bad"},
            }
        )
    )
    out = tr.translate(_se({"type": "content_block_stop", "index": 0}))
    assert out[0].input == {}


# ---------------------------------------------------------------------------
# Consolidated AssistantMessage
# ---------------------------------------------------------------------------


def test_assistant_message_after_stream_only_completes() -> None:
    """A stream-driven message's consolidated AssistantMessage must NOT re-emit
    parts — only MessageCompleted."""
    ctx = _ctx()
    tr = _tr(ctx)
    _stream_text(tr, message_id="msg1", text="Hi")
    msg = AssistantMessage(
        content=[TextBlock(text="Hi")],
        model="m",
        message_id="msg1",
        usage={"input_tokens": 5, "output_tokens": 3},
        stop_reason="end_turn",
    )
    out = tr.translate(msg)
    assert len(out) == 1
    assert isinstance(out[0], MessageCompleted)
    assert out[0].message_id == "msg1"
    assert out[0].finish_reason == "end_turn"
    assert out[0].tokens == {"input": 5, "output": 3, "total": 8}


def test_assistant_message_without_streaming_emits_parts_oneshot() -> None:
    """Partial messages OFF: the full AssistantMessage emits MessageCreated +
    PartStarted+PartCreated per block + MessageCompleted."""
    tr = _tr()
    msg = AssistantMessage(
        content=[TextBlock(text="Hello there")],
        model="m",
        message_id="msgX",
        usage={"output_tokens": 2},
        stop_reason="end_turn",
    )
    out = tr.translate(msg)
    types = [type(e).__name__ for e in out]
    assert types == ["MessageCreated", "PartStarted", "PartCreated", "MessageCompleted"]
    pc = next(e for e in out if isinstance(e, PartCreated))
    assert isinstance(pc.part, TextPart)
    assert pc.part.text == "Hello there"


def test_assistant_message_oneshot_thinking_and_tool() -> None:
    tr = _tr()
    msg = AssistantMessage(
        content=[
            ThinkingBlock(thinking="hmm", signature="sg"),
            ToolUseBlock(id="toolu_9", name="Edit", input={"path": "/x"}),
        ],
        model="m",
        message_id="msgY",
    )
    out = tr.translate(msg)
    reasoning = [e for e in out if isinstance(e, PartCreated) and isinstance(e.part, ReasoningPart)]
    assert reasoning and reasoning[0].part.text == "hmm"
    assert reasoning[0].part.signature == "sg"
    tool_calls = [e for e in out if isinstance(e, ToolCall)]
    assert tool_calls[0].tool_call_id == "toolu_9"
    assert tool_calls[0].tool_name == "Edit"
    assert tool_calls[0].input == {"path": "/x"}
    assert tool_calls[0].status == "running"


# ---------------------------------------------------------------------------
# Tool results (UserMessage)
# ---------------------------------------------------------------------------


def test_user_message_tool_result_completes_call() -> None:
    tr = _tr()
    msg = UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content="ok output")])
    out = tr.translate(msg)
    assert len(out) == 1
    assert isinstance(out[0], ToolCallUpdate)
    assert out[0].tool_call_id == "toolu_1"
    assert out[0].status == "completed"
    assert out[0].output == "ok output"
    assert out[0].error_text is None


def test_user_message_tool_result_error() -> None:
    tr = _tr()
    msg = UserMessage(content=[ToolResultBlock(tool_use_id="t2", content="boom", is_error=True)])
    out = tr.translate(msg)
    assert out[0].status == "error"
    assert out[0].error_text == "boom"


def test_user_message_tool_result_list_content_flattened() -> None:
    tr = _tr()
    msg = UserMessage(
        content=[ToolResultBlock(tool_use_id="t", content=[{"type": "text", "text": "line"}])]
    )
    out = tr.translate(msg)
    assert out[0].output == "line"


def test_user_message_plain_text_is_dropped() -> None:
    assert _tr().translate(UserMessage(content="my prompt echo")) == []


# ---------------------------------------------------------------------------
# Result (turn end)
# ---------------------------------------------------------------------------


def test_result_success_emits_turn_finished_when_turn_open() -> None:
    ctx = _ctx("turn-1")
    tr = _tr(ctx)
    out = tr.translate(
        _result(
            stop_reason="end_turn",
            total_cost_usd=0.02,
            usage={"input_tokens": 5, "output_tokens": 7},
        )
    )
    tf = next(e for e in out if isinstance(e, TurnFinished))
    assert tf.turn_id == "turn-1"
    assert tf.stop_reason == "end_turn"
    assert tf.cost_usd == 0.02
    assert tf.tokens == {"input": 5, "output": 7, "total": 12}
    status = next(e for e in out if isinstance(e, SessionStatusChanged))
    assert status.status == "idle"
    assert status.turn_id == "turn-1"
    assert not ctx.open_queries  # the result retired its query


def test_result_for_nothing_in_flight_is_dropped() -> None:
    # No query is open (an interrupt at idle emits no result at all), so this one
    # is a stray: publishing its error status would surface a phantom harness error.
    assert _tr(_ctx()).translate(_result(subtype="error_during_execution", is_error=True)) == []


def test_result_error_maps_to_error_turn_and_status() -> None:
    out = _tr(_ctx("t")).translate(
        _result(subtype="error_during_execution", is_error=True, result="kaboom")
    )
    tf = next(e for e in out if isinstance(e, TurnFinished))
    assert tf.stop_reason == "error"
    assert tf.error_detail == "kaboom"
    status = next(e for e in out if isinstance(e, SessionStatusChanged))
    assert status.status == "error"


@pytest.mark.parametrize(
    ("result_kw", "stop_reason"),
    [
        ({"subtype": "error_max_turns", "is_error": True}, "max_turn_requests"),
        ({"stop_reason": "max_tokens"}, "max_tokens"),
        ({"stop_reason": "tool_use"}, "end_turn"),
    ],
    ids=["max turns", "max tokens", "tool use"],
)
def test_a_results_stop_reason_maps_to_the_ir(result_kw: dict[str, Any], stop_reason: str) -> None:
    """The SDK's two stop-reason fields flatten to one IR reason, with the
    mid-turn `tool_use` pause reading as an ordinary end."""
    out = _tr(_ctx("t")).translate(_result(**result_kw))
    assert next(e for e in out if isinstance(e, TurnFinished)).stop_reason == stop_reason


# ---------------------------------------------------------------------------
# Usage flattening + cache buckets
# ---------------------------------------------------------------------------


def test_usage_flatten_promotes_cache_buckets() -> None:
    ctx = _ctx()
    tr = _tr(ctx)
    _stream_text(tr, message_id="m", text="x")
    msg = AssistantMessage(
        content=[TextBlock(text="x")],
        model="m",
        message_id="m",
        usage={
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20,
        },
    )
    out = tr.translate(msg)
    completed = next(e for e in out if isinstance(e, MessageCompleted))
    assert completed.tokens == {
        "input": 10,
        "output": 4,
        "cache_read": 100,
        "cache_write": 20,
        "total": 14,
    }


# ---------------------------------------------------------------------------
# System messages + misc stream events
# ---------------------------------------------------------------------------


def test_system_init_captures_agent_session_id() -> None:
    ctx = _ctx()
    out = _tr(ctx).translate(SystemMessage(subtype="init", data={"session_id": "cc-123"}))
    assert out == []
    assert ctx.agent_session_id == "cc-123"


def test_system_other_subtype_drops() -> None:
    assert _tr().translate(SystemMessage(subtype="status", data={})) == []


def test_native_task_subsystem_messages_are_suppressed() -> None:
    # The Claude SDK's native background-task subsystem (TaskStarted/Progress/
    # Notification) is suppressed — backgrounding is OUR asyncio registry, not the
    # CLI's task tool, so these must NEVER leak as chat events. They are SystemMessage
    # subclasses, so they route through _translate_system and are dropped to [].
    from claude_agent_sdk.types import (
        TaskNotificationMessage,
        TaskProgressMessage,
        TaskStartedMessage,
    )

    # (a) the typed messages route through the system-message path...
    for cls in (TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage):
        assert issubclass(cls, SystemMessage)
    # (b) ...and a system message carrying any task subtype is dropped (no coupling to
    # the typed classes' fields, which vary across SDK versions).
    for subtype in ("task_started", "task_progress", "task_notification"):
        assert _tr().translate(SystemMessage(subtype=subtype, data={})) == []


def test_stream_error_event_emits_error_status() -> None:
    out = _tr(_ctx("t")).translate(
        _se({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
    )
    assert isinstance(out[0], SessionStatusChanged)
    assert out[0].status == "error"
    assert out[0].detail == "busy"
    assert out[0].turn_id == "t"  # the query whose stream carried it


def test_ping_and_message_lifecycle_events_drop() -> None:
    tr = _tr()
    assert tr.translate(_se({"type": "ping"})) == []
    assert tr.translate(_se({"type": "message_delta", "delta": {}, "usage": {}})) == []
    assert tr.translate(_se({"type": "message_stop"})) == []


def test_unknown_sdk_message_yields_raw_event() -> None:
    class _Weird:
        pass

    out = _tr().translate(_Weird())
    assert isinstance(out[0], RawEvent)
    assert out[0].event_type == "_Weird"


# ---------------------------------------------------------------------------
# reset()
# ---------------------------------------------------------------------------


def test_reset_clears_all_cross_event_state() -> None:
    ctx = _ctx("t")
    tr = _tr(ctx)
    # Open a part mid-stream, then reset.
    tr.translate(_se({"type": "message_start", "message": {"id": "m"}}))
    tr.translate(
        _se({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}})
    )
    assert ctx.open_parts
    tr.reset()
    assert ctx.open_parts == {}
    # `clear()` tears the client down, so no result can follow the queries it drops.
    assert not ctx.open_queries
    assert ctx.current_message_id is None
    assert ctx.seen_message_ids == set()
    assert ctx.stream_message_ids == set()
    # A fresh message with the SAME id re-announces (state forgotten).
    out = tr.translate(_se({"type": "message_start", "message": {"id": "m"}}))
    assert isinstance(out[0], MessageCreated)
