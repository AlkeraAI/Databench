"""Regression suite for `OpencodeHttpAdapter._translate()`.

Each test feeds a synthetic native opencode event whose shape mirrors
the schema at `vendor/opencode/packages/opencode/src/...` and asserts
the IR event we emit. This is the safety net for the "untyped dict
killed us" class of bug — every shape we depend on is locked here.

When opencode upstream changes a wire shape (we bump the subtree SHA)
these tests are the first thing to break; that's the signal to revisit
the translator.

Schema citations live in the docstring of each test pointing at the
opencode source file + line.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from _mocks.adapter_seam import drain, opencode_adapter, send, statuses
from _mocks.opencode_transport import NATIVE_SESSION, StubTransport, native_frames
from alkera_cli.harness.adapters.opencode_http import OpencodeHttpAdapter
from alkera_cli.harness.adapters.opencode_translate import _OpenPart
from alkera_core.schemas.chat import (
    AgentMessageChunk,
    CompactionApplied,
    Heartbeat,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PartStarted,
    PermissionRequest,
    PermissionResolved,
    RawEvent,
    ReasoningPart,
    SessionStatusChanged,
    TextPart,
    ToolCall,
    ToolCallUpdate,
)


@pytest.fixture
def adapter(tmp_path: Path) -> OpencodeHttpAdapter:
    """Bare adapter, never started. `_translate()` is pure over `_state` and
    `_config`, so no subprocess is needed."""
    return opencode_adapter(tmp_path)


@pytest.fixture
def vendor_shell_adapter(tmp_path: Path) -> OpencodeHttpAdapter:
    """An adapter where ``bash`` is still opencode's OWN shell.

    Wherever the parent-hosted shell is registered (POSIX today) that tool gates
    its own command and raises Alkera's ask itself, so opencode's ask for it is
    answered by the adapter and never translated — see
    ``test_opencode_single_ask_per_call``. The subject these tests pin is the
    vendor shell's, which is what a Windows agent still runs."""
    adapter = opencode_adapter(tmp_path)
    adapter._translator_ctx.parent_hosted_shell = False
    return adapter


def _event(type_: str, **props: Any) -> dict[str, Any]:
    """Wrap properties in opencode's `{type, properties}` envelope."""
    return {"type": type_, "properties": props}


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def test_server_connected_dropped(adapter: OpencodeHttpAdapter) -> None:
    """Schema: empty struct. No semantic content → drop."""
    assert adapter._translate(_event("server.connected")) is None


def test_global_disposed_dropped(adapter: OpencodeHttpAdapter) -> None:
    assert adapter._translate(_event("global.disposed")) is None


def test_server_heartbeat_emits_heartbeat(adapter: OpencodeHttpAdapter) -> None:
    """Schema: empty struct (server/event.ts)."""
    out = adapter._translate(_event("server.heartbeat"))
    assert isinstance(out, Heartbeat)
    assert out.last_activity_ms == 0


# ---------------------------------------------------------------------------
# session.status / session.idle / session.error / session.updated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("native", "status", "phase"),
    [("idle", "idle", "idle"), ("busy", "running", "awaiting_llm")],
)
def test_session_status_maps_to_the_ir(
    adapter: OpencodeHttpAdapter, native: str, status: str, phase: str
) -> None:
    """status.ts: a native `idle`/`busy` status maps to the IR's status plus its
    own phase."""
    out = adapter._translate(
        _event("session.status", sessionID="opencode-sid", status={"type": native})
    )
    assert isinstance(out, SessionStatusChanged)
    assert (out.status, out.phase) == (status, phase)


def test_session_status_retry_carries_message(adapter: OpencodeHttpAdapter) -> None:
    """session.status retry expands to BOTH SessionStatusChanged
    (so the REPL's turn-idle gate stays correct) AND Retrying (a
    typed signal for UIs that want to render a distinct "retrying"
    indicator)."""
    out = adapter._translate(
        _event(
            "session.status",
            sessionID="opencode-sid",
            status={
                "type": "retry",
                "attempt": 2,
                "message": "rate limited",
                "next": 5000,
            },
        )
    )
    assert isinstance(out, list)
    assert len(out) == 2
    sched, retrying = out
    assert isinstance(sched, SessionStatusChanged)
    assert sched.detail == "rate limited"
    from alkera_core.schemas.chat import Retrying

    assert isinstance(retrying, Retrying)
    assert retrying.attempt == 2
    assert retrying.next_attempt_in_ms == 5000
    assert retrying.reason == "rate limited"


def test_session_idle_legacy_event_still_handled(adapter: OpencodeHttpAdapter) -> None:
    """Deprecated upstream but still emitted alongside `session.status`."""
    out = adapter._translate(_event("session.idle", sessionID="opencode-sid"))
    assert isinstance(out, SessionStatusChanged)
    assert out.status == "idle"


def test_session_error_distills_message(adapter: OpencodeHttpAdapter) -> None:
    """session/session.ts: `{sessionID?, error?: AssistantErrorSchema}`."""
    out = adapter._translate(
        _event(
            "session.error",
            sessionID="opencode-sid",
            error={"name": "ProviderError", "data": {"message": "401 Unauthorized"}},
        )
    )
    assert isinstance(out, SessionStatusChanged)
    assert out.status == "error"
    assert out.detail == "401 Unauthorized"
    # A non-overflow error is NOT flagged — the runtime must not try to compact it.
    assert out.overflow is False


_DEFECT_STACK = (
    "ProviderModelNotFoundError: \n"
    "    at <anonymous> (/$bunfs/root/src/provider/provider.ts:812:15)\n"
    "    at getModel (/$bunfs/root/src/session/prompt.ts:660:30)\n"
)


@pytest.mark.parametrize(
    ("message", "detail"),
    [
        pytest.param(
            _DEFECT_STACK,
            "The agent stopped on an internal error. Send the message again.",
            id="a-bare-error-name-over-a-stack",
        ),
        pytest.param(
            "TypeError: cannot read properties of undefined\n    at run (/$bunfs/root/a.ts:1:1)",
            "TypeError: cannot read properties of undefined",
            id="a-worded-error-over-a-stack-keeps-its-words",
        ),
        pytest.param(
            "    at run (/$bunfs/root/a.ts:1:1)",
            "The agent stopped on an internal error. Send the message again.",
            id="nothing-but-frames",
        ),
        pytest.param(
            "Model not found: alkera-anthropic/claude-haiku-4.5::none.",
            "Model not found: alkera-anthropic/claude-haiku-4.5::none.",
            id="an-ordinary-message-is-untouched",
        ),
        pytest.param(
            "first line\nsecond line with no frame",
            "first line\nsecond line with no frame",
            id="a-multi-line-message-with-no-stack-is-untouched",
        ),
    ],
)
def test_session_error_never_puts_a_stack_trace_on_the_transcript(
    adapter: OpencodeHttpAdapter, message: str, detail: str
) -> None:
    """An uncaught defect inside the agent reaches ``session.error`` as
    ``NamedError.Unknown`` whose message is the stack. The reader is shown a
    sentence, never the frames or the agent's file paths."""
    out = adapter._translate(
        _event(
            "session.error",
            sessionID="opencode-sid",
            error={"name": "UnknownError", "data": {"message": message}},
        )
    )
    assert isinstance(out, SessionStatusChanged)
    assert out.detail == detail
    assert "/$bunfs" not in (out.detail or "")


def test_session_error_lifts_the_gateway_s_money_refusal_off_the_wrapper(
    adapter: OpencodeHttpAdapter,
) -> None:
    """The gateway's 402 reaches opencode as the AI SDK's APICallError: the
    message lifted out, the raw body kept as a STRING under responseBody. The
    refusal's code and facts ride on the status event, and the detail is the
    refusal's own sentence — so the durable row, not the browser, knows which
    allowance ran out."""
    body = {
        "type": "error",
        "error": {
            "type": "gateway_error",
            "code": "credit.pool_exhausted",
            "message": "The Data Science pool is used up.",
            "team_id": "t-1",
            "team_name": "Data Science",
        },
    }
    out = adapter._translate(
        _event(
            "session.error",
            sessionID="opencode-sid",
            error={
                "name": "APIError",
                "data": {
                    "message": "The Data Science pool is used up.",
                    "statusCode": 402,
                    "responseBody": json.dumps(body),
                },
            },
        )
    )
    assert isinstance(out, SessionStatusChanged)
    assert out.status == "error"
    assert out.detail == "The Data Science pool is used up."
    assert out.refusal is not None
    assert out.refusal.code == "credit.pool_exhausted"
    assert out.refusal.team_name == "Data Science"
    assert out.refusal.team_id == "t-1"
    assert out.overflow is False
    # The row is what the machine persists: the refusal survives a JSON round-trip.
    assert out.model_validate(out.model_dump(mode="json")).refusal == out.refusal


def test_session_error_without_a_code_carries_no_refusal(adapter: OpencodeHttpAdapter) -> None:
    """Prose that mentions credit is not a refusal: a provider outage whose text
    says "credit" must not send the reader to top up."""
    out = adapter._translate(
        _event(
            "session.error",
            sessionID="opencode-sid",
            error={"name": "ProviderError", "data": {"message": "no credit available (seat)"}},
        )
    )
    assert isinstance(out, SessionStatusChanged)
    assert out.refusal is None
    assert out.detail == "no credit available (seat)"


def test_session_error_flags_context_overflow_from_code(
    adapter: OpencodeHttpAdapter,
) -> None:
    out = adapter._translate(
        _event(
            "session.error",
            sessionID="opencode-sid",
            error={
                "name": "ProviderError",
                "data": {"message": "prompt is too long", "code": "context_length_exceeded"},
            },
        )
    )
    assert isinstance(out, SessionStatusChanged)
    assert out.status == "error"
    assert out.overflow is True


def test_session_error_flags_overflow_from_stringified_json_message(
    adapter: OpencodeHttpAdapter,
) -> None:
    # The real shape: opencode surfaces an in-band SSE error as the whole gateway
    # error JSON (carrying the stamped code) inside `data.message` as a STRING.
    inner = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": "upstream error 400: prompt is too long: 1063592 > 1000000",
                "code": "context_length_exceeded",
            },
        }
    )
    out = adapter._translate(
        _event("session.error", sessionID="opencode-sid", error={"data": {"message": inner}})
    )
    assert isinstance(out, SessionStatusChanged)
    assert out.overflow is True


def test_session_updated_falls_through_to_raw_event(
    adapter: OpencodeHttpAdapter,
) -> None:
    """No status info on `session.updated` — we surface it as RawEvent
    rather than synthesize a SessionStatusChanged."""
    out = adapter._translate(
        _event("session.updated", title="Renamed", model={"id": "x", "providerID": "y"})
    )
    assert isinstance(out, RawEvent)
    assert out.event_type == "session.updated"


# ---------------------------------------------------------------------------
# message.updated — info: User | Assistant discriminated by `role`
# ---------------------------------------------------------------------------


def test_message_updated_user_role_emits_message_created(
    adapter: OpencodeHttpAdapter,
) -> None:
    """User shape: `{id, sessionID, role: "user", time: {created}, ...}`."""
    out = adapter._translate(
        _event(
            "message.updated",
            sessionID="opencode-sid",
            info={
                "id": "msg-u1",
                "sessionID": "opencode-sid",
                "role": "user",
                "time": {"created": 1_700_000_000_000},
                "agent": "general",
                "model": "x",
            },
        )
    )
    assert isinstance(out, MessageCreated)
    assert out.message_id == "msg-u1"
    assert out.role == "user"


@pytest.mark.parametrize(
    ("info", "stamp"),
    [
        pytest.param(
            {"providerID": "alkera-anthropic", "modelID": "claude-opus-5-5::high"},
            {"provider_id": "alkera-anthropic", "model_id": "claude-opus-5-5", "effort": "high"},
            id="the-effort-split-off-the-key",
        ),
        pytest.param(
            {"providerID": "alkera-anthropic", "modelID": "claude-opus-5-5::low::summarized"},
            {"provider_id": "alkera-anthropic", "model_id": "claude-opus-5-5", "effort": "low"},
            id="a-display-component-is-not-the-effort",
        ),
        pytest.param(
            {"providerID": "alkera-openai", "modelID": "gpt-5.5"},
            {"provider_id": "alkera-openai", "model_id": "gpt-5.5"},
            id="no-effort",
        ),
        pytest.param({}, {}, id="an-older-opencode-names-no-model"),
        pytest.param({"providerID": "p", "modelID": ""}, {}, id="an-empty-model"),
    ],
)
def test_an_assistant_message_is_stamped_with_the_model_it_ran(
    adapter: OpencodeHttpAdapter, info: dict[str, Any], stamp: dict[str, str]
) -> None:
    """opencode files a reply under the model key it ran (effort and all); the
    stamp is what the transcript records as the model that answered."""
    out = adapter._translate(
        _event(
            "message.updated",
            sessionID="opencode-sid",
            info={
                "id": "msg-a9",
                "role": "assistant",
                "time": {"created": 1_700_000_000_000},
                **info,
            },
        )
    )
    assert isinstance(out, MessageCreated)
    assert out.model == stamp


def test_a_user_message_carries_no_model_stamp(adapter: OpencodeHttpAdapter) -> None:
    """Only a reply says which model ran; a person's message did not run on one."""
    out = adapter._translate(
        _event(
            "message.updated",
            sessionID="opencode-sid",
            info={
                "id": "msg-u9",
                "role": "user",
                "time": {"created": 1_700_000_000_000},
                "model": {"providerID": "alkera-anthropic", "modelID": "claude-opus-5-5::high"},
            },
        )
    )
    assert isinstance(out, MessageCreated)
    assert out.model == {}


def test_message_updated_assistant_completed_emits_message_completed(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Assistant shape (message-v2.ts:452-490):
      `{id, role: "assistant", time: {created, completed?},
        tokens: {total?, input, output, reasoning, cache: {read, write}},
        cost, finish: Schema.optional(Schema.String), ...}`.

    Completion is detected by `time.completed` being set, NOT by an
    `info.completed` field (which doesn't exist upstream).

    CRITICAL: `finish` is a bare STRING, NOT a nested `{reason}` dict.
    """
    out = adapter._translate(
        _event(
            "message.updated",
            sessionID="opencode-sid",
            info={
                "id": "msg-a1",
                "role": "assistant",
                "time": {"created": 1_700_000_000_000, "completed": 1_700_000_001_000},
                "tokens": {
                    "input": 100,
                    "output": 50,
                    "reasoning": 7,
                    "cache": {"read": 42, "write": 13},
                    "total": 157,
                },
                "cost": 0.012,
                "finish": "stop",  # ← STRING, not `{"reason": "stop"}`
                "modelID": "claude-opus-4-7",
                "providerID": "anthropic",
                "agent": "general",
                "mode": "ask",
                "parentID": "",
                "path": "",
            },
        )
    )
    assert isinstance(out, MessageCompleted)
    assert out.message_id == "msg-a1"
    assert out.finish_reason == "stop"
    # `cache.{read,write}` flattens to `cache_read`/`cache_write` so the
    # manifest's tokens_total fold (which reads those keys) is lossless.
    assert out.tokens == {
        "input": 100,
        "output": 50,
        "reasoning": 7,
        "cache_read": 42,
        "cache_write": 13,
        "total": 157,
    }
    assert out.cost == 0.012


def test_message_updated_assistant_finish_absent_emits_empty_reason(
    adapter: OpencodeHttpAdapter,
) -> None:
    """`finish` is `Schema.optional(Schema.String)` — may be missing.
    When absent, finish_reason should be empty string (not crash, not
    default-stringify a dict)."""
    out = adapter._translate(
        _event(
            "message.updated",
            sessionID="opencode-sid",
            info={
                "id": "msg-a2",
                "role": "assistant",
                "time": {"created": 1_700_000_000_000, "completed": 1_700_000_001_000},
                "tokens": {
                    "input": 10,
                    "output": 5,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                },
                "cost": 0.0,
                "modelID": "claude-opus-4-7",
                "providerID": "anthropic",
                "agent": "general",
                "mode": "ask",
                "parentID": "",
                "path": "",
            },
        )
    )
    assert isinstance(out, MessageCompleted)
    assert out.finish_reason == ""


def test_message_updated_assistant_tokens_partial_struct(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Defensive: if upstream ever omits `cache` or `reasoning`, the
    translator must not crash — it just drops the missing fields."""
    out = adapter._translate(
        _event(
            "message.updated",
            sessionID="opencode-sid",
            info={
                "id": "msg-a3",
                "role": "assistant",
                "time": {"created": 1_700_000_000_000, "completed": 1_700_000_001_000},
                "tokens": {"input": 1, "output": 2},  # no cache, no reasoning
                "cost": 0.0,
                "finish": "stop",
                "modelID": "claude-opus-4-7",
                "providerID": "anthropic",
                "agent": "general",
                "mode": "ask",
                "parentID": "",
                "path": "",
            },
        )
    )
    assert isinstance(out, MessageCompleted)
    assert out.tokens == {"input": 1, "output": 2}


def _completed(message_id: str, **tokens: int) -> dict[str, Any]:
    """A completed assistant `message.updated` carrying one step's usage."""
    return _event(
        "message.updated",
        sessionID="opencode-sid",
        info={
            "id": message_id,
            "role": "assistant",
            "time": {"created": 1_700_000_000_000, "completed": 1_700_000_001_000},
            "tokens": {"cache": {"read": 0, "write": 0}, **tokens},
            "cost": 0.0,
            "finish": "tool-calls",
            "modelID": "claude-opus-4-7",
            "providerID": "anthropic",
        },
    )


def test_each_step_reports_its_own_usage_not_the_session_running_total(
    adapter: OpencodeHttpAdapter,
) -> None:
    """opencode opens a NEW assistant message per step (`prompt.ts` mints one
    inside the step loop) and ASSIGNS that step's usage to it
    (`processor.ts`: ``ctx.assistantMessage.tokens = usage.tokens``, not
    ``+=``). So ``MessageCompleted.tokens`` is per message, exactly as the
    schema says — a reader may add two messages' figures together without
    double-counting."""
    first = adapter._translate(_completed("msg-step-1", input=1_000, output=50))
    second = adapter._translate(_completed("msg-step-2", input=900, output=40))
    assert isinstance(first, MessageCompleted) and isinstance(second, MessageCompleted)
    assert first.message_id != second.message_id
    assert first.tokens["input"] == 1_000
    # Cumulative would read 1,900 here; per-message reads this step alone.
    assert second.tokens["input"] == 900


def test_a_completed_message_restated_unchanged_is_not_emitted_twice(
    adapter: OpencodeHttpAdapter,
) -> None:
    """opencode calls ``updateMessage`` again on a message it has already
    stamped ``time.completed`` (the turn's finalizer, milliseconds after the
    step's own write). Both writes carry the SAME usage, so emitting both put
    two identical completions on the transcript and charged every token
    aggregate twice."""
    first = adapter._translate(_completed("msg-step-1", input=1_000, output=50))
    again = adapter._translate(_completed("msg-step-1", input=1_000, output=50))
    assert isinstance(first, MessageCompleted)
    assert again is None


def test_a_completed_message_restated_with_new_usage_is_emitted_again(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Suppression is for a RESTATEMENT only: a second write that actually
    revises the message must still reach the transcript, or a late usage
    correction would be lost."""
    first = adapter._translate(_completed("msg-step-1", input=1_000, output=50))
    revised = adapter._translate(_completed("msg-step-1", input=1_000, output=90))
    assert isinstance(first, MessageCompleted)
    assert isinstance(revised, MessageCompleted)
    assert revised.tokens["output"] == 90


def test_message_updated_assistant_in_flight_emits_message_created(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Assistant message with `time.completed` UNSET (still streaming)
    should be treated as MessageCreated, not Completed."""
    out = adapter._translate(
        _event(
            "message.updated",
            sessionID="opencode-sid",
            info={
                "id": "msg-a1",
                "role": "assistant",
                "time": {"created": 1_700_000_000_000},
                "modelID": "claude-opus-4-7",
                "providerID": "anthropic",
                "agent": "general",
                "mode": "ask",
                "parentID": "",
                "path": "",
            },
        )
    )
    assert isinstance(out, MessageCreated)
    assert out.role == "assistant"


# ---------------------------------------------------------------------------
# message.part.updated — ToolPart / TextPart
# ---------------------------------------------------------------------------


def test_part_updated_text_part_first_sight_emits_part_started(
    adapter: OpencodeHttpAdapter,
) -> None:
    """TextPart schema: `{id, sessionID, messageID, type: "text", text, ...}`."""
    out = adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "p1",
                "sessionID": "opencode-sid",
                "messageID": "m1",
                "type": "text",
                "text": "",
            },
        )
    )
    assert isinstance(out, PartStarted)
    assert out.part_id == "p1"
    assert out.part_type == "text"


def test_part_updated_text_part_completed_emits_part_created(
    adapter: OpencodeHttpAdapter,
) -> None:
    """TextParts (message-v2.ts:97-110) have NO `state` field — they
    finalize when `time.end` is set, not via `state.status`."""
    # First sight (no time.end) registers it as open + emits PartStarted.
    adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "p1",
                "sessionID": "opencode-sid",
                "messageID": "m1",
                "type": "text",
                "text": "Hello",
                "time": {"start": 1_700_000_000_000},
            },
        )
    )
    # Second sight WITH time.end finalizes → PartCreated carrying text.
    out = adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "p1",
                "sessionID": "opencode-sid",
                "messageID": "m1",
                "type": "text",
                "text": "Hello world",
                "time": {"start": 1_700_000_000_000, "end": 1_700_000_001_000},
            },
        )
    )
    assert isinstance(out, PartCreated)
    assert isinstance(out.part, TextPart)
    assert out.part.text == "Hello world"


def test_part_updated_reasoning_finalizes_with_buffered_deltas(
    adapter: OpencodeHttpAdapter,
) -> None:
    """opencode leaves a reasoning part's `text` EMPTY when it finalizes — the
    reasoning text only ever streams via `message.part.delta`. The finalized
    part is the ONLY thing persisted to chat.jsonl, so the translator must
    consolidate the buffered deltas into the finalized ReasoningPart. Without
    this a replayed reasoning part is textless and the UI shows no thinking even
    with show-thoughts on."""
    sid = "opencode-sid"
    # First sight (no time.end) registers the open reasoning part.
    adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "r1",
                "sessionID": sid,
                "messageID": "m1",
                "type": "reasoning",
                "text": "",
                "time": {"start": 1},
            },
        )
    )
    # The reasoning text arrives ONLY as deltas (buffered + emitted live).
    for chunk in ("Let me ", "think about ", "this."):
        adapter._translate(
            _event(
                "message.part.delta",
                sessionID=sid,
                messageID="m1",
                partID="r1",
                field="text",
                delta=chunk,
            )
        )
    # Finalize (time.end set) — opencode's part `text` is STILL empty.
    out = adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "r1",
                "sessionID": sid,
                "messageID": "m1",
                "type": "reasoning",
                "text": "",
                "time": {"start": 1, "end": 2},
            },
        )
    )
    assert isinstance(out, PartCreated)
    assert isinstance(out.part, ReasoningPart)
    assert out.part.text == "Let me think about this."


def test_part_updated_intermediate_text_growth_is_dropped(
    adapter: OpencodeHttpAdapter,
) -> None:
    """An open text part that grows but isn't finalized (no time.end)
    emits nothing — live UI is driven by message.part.delta chunks;
    only finalized parts persist."""
    adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "p1",
                "sessionID": "opencode-sid",
                "messageID": "m1",
                "type": "text",
                "text": "Hel",
                "time": {"start": 1_700_000_000_000},
            },
        )
    )
    out = adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "p1",
                "sessionID": "opencode-sid",
                "messageID": "m1",
                "type": "text",
                "text": "Hello",
                "time": {"start": 1_700_000_000_000},
            },
        )
    )
    assert out is None


def test_part_updated_already_complete_text_emits_started_and_created(
    adapter: OpencodeHttpAdapter,
) -> None:
    """User-message text parts arrive already-complete (time.end set
    on first sight). The adapter emits BOTH PartStarted and
    PartCreated so the text lands in chat.jsonl — this is the bug
    that made resumed conversations show empty `» you` headers."""
    out = adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "pu1",
                "sessionID": "opencode-sid",
                "messageID": "mu1",
                "type": "text",
                "text": "my name is sam",
                "time": {"start": 1_700_000_000_000, "end": 1_700_000_000_000},
            },
        )
    )
    assert isinstance(out, list)
    assert len(out) == 2
    started, created = out
    assert isinstance(started, PartStarted)
    assert isinstance(created, PartCreated)
    assert isinstance(created.part, TextPart)
    assert created.part.text == "my name is sam"


def test_part_updated_synthetic_flag_is_carried_through(
    adapter: OpencodeHttpAdapter,
) -> None:
    """opencode marks injected/internal text parts `synthetic` (our
    plan reminder, its own nudges). The translator carries the flag so
    the UI can skip rendering them."""
    out = adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "ps1",
                "sessionID": "opencode-sid",
                "messageID": "ms1",
                "type": "text",
                "text": "<system-reminder>plan rules</system-reminder>",
                "synthetic": True,
                "time": {"start": 1, "end": 1},
            },
        )
    )
    assert isinstance(out, list)
    _started, created = out
    assert isinstance(created, PartCreated)
    assert isinstance(created.part, TextPart)
    assert created.part.synthetic is True


def test_part_updated_tool_part_uses_tool_field_not_name(
    adapter: OpencodeHttpAdapter,
) -> None:
    """CRITICAL: ToolPart has `tool` (string) for the name, NOT `name`.
    The previous implementation read `part.name` (which doesn't exist),
    silently producing empty tool_name. This test pins the correct
    field."""
    out = adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "tc1",
                "sessionID": "opencode-sid",
                "messageID": "m1",
                "type": "tool",
                "callID": "call-abc",
                "tool": "read",
                "state": {"status": "pending", "input": {"path": "/etc/hosts"}},
            },
        )
    )
    assert isinstance(out, ToolCall)
    assert out.tool_name == "read"
    assert out.input == {"path": "/etc/hosts"}
    assert out.status == "pending"


def test_part_updated_tool_running_emits_update(
    adapter: OpencodeHttpAdapter,
) -> None:
    # First sight registers tool.
    adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "tc1",
                "sessionID": "opencode-sid",
                "messageID": "m1",
                "type": "tool",
                "callID": "call-abc",
                "tool": "read",
                "state": {"status": "pending"},
            },
        )
    )
    out = adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "tc1",
                "sessionID": "opencode-sid",
                "messageID": "m1",
                "type": "tool",
                "callID": "call-abc",
                "tool": "read",
                "state": {"status": "running"},
            },
        )
    )
    assert isinstance(out, ToolCallUpdate)
    assert out.status == "running"


def test_part_updated_tool_completed_pulls_output_from_state(
    adapter: OpencodeHttpAdapter,
) -> None:
    """CRITICAL: tool output lives at `part.state.output` (the
    discriminated `state.completed` variant), NOT `part.output`."""
    adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "tc1",
                "sessionID": "opencode-sid",
                "messageID": "m1",
                "type": "tool",
                "callID": "call-abc",
                "tool": "read",
                "state": {"status": "pending"},
            },
        )
    )
    out = adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "tc1",
                "sessionID": "opencode-sid",
                "messageID": "m1",
                "type": "tool",
                "callID": "call-abc",
                "tool": "read",
                "state": {
                    "status": "completed",
                    "input": {"path": "/etc/hosts"},
                    "output": "127.0.0.1 localhost",
                },
            },
        )
    )
    assert isinstance(out, ToolCallUpdate)
    assert out.status == "completed"
    assert out.output == "127.0.0.1 localhost"


def test_part_updated_missing_part_id_falls_back_to_raw(
    adapter: OpencodeHttpAdapter,
) -> None:
    """A defensive guard: an event missing the required keys should
    NOT crash — RawEvent fallthrough."""
    out = adapter._translate(_event("message.part.updated", part={}))
    assert isinstance(out, RawEvent)


# ---------------------------------------------------------------------------
# message.part.delta — the bug that prompted this whole audit
# ---------------------------------------------------------------------------


def test_part_delta_uses_delta_as_string_not_dict(
    adapter: OpencodeHttpAdapter,
) -> None:
    """CRITICAL: the bug that triggered this audit. Schema:
       `{sessionID, messageID, partID, field: string, delta: string}`.

    `delta` is the string itself — NOT a dict with a `text` key.
    The old implementation did `delta.get("text")` and crashed with
    `AttributeError: 'str' object has no attribute 'get'`.
    """
    # Register the part first so the delta knows it's a text part.
    adapter._translate(
        _event(
            "message.part.updated",
            part={
                "id": "p1",
                "sessionID": "opencode-sid",
                "messageID": "m1",
                "type": "text",
                "text": "",
            },
        )
    )
    out = adapter._translate(
        _event(
            "message.part.delta",
            sessionID="opencode-sid",
            messageID="m1",
            partID="p1",
            field="text",
            delta="Hello",
        )
    )
    assert isinstance(out, AgentMessageChunk)
    assert out.text == "Hello"
    assert out.sequence == 0


def test_part_delta_non_text_field_falls_back_to_raw(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Currently only `field="text"` is used upstream. Other fields
    should NOT produce an AgentMessageChunk (their semantics aren't
    text)."""
    out = adapter._translate(
        _event(
            "message.part.delta",
            sessionID="opencode-sid",
            messageID="m1",
            partID="tc1",
            field="input",
            delta='{"path":',
        )
    )
    assert isinstance(out, RawEvent)


def test_part_delta_with_empty_delta_returns_none(
    adapter: OpencodeHttpAdapter,
) -> None:
    out = adapter._translate(
        _event(
            "message.part.delta",
            sessionID="opencode-sid",
            messageID="m1",
            partID="p1",
            field="text",
            delta="",
        )
    )
    assert out is None


# ---------------------------------------------------------------------------
# permission.asked / permission.replied
# ---------------------------------------------------------------------------


def test_permission_asked_pulls_call_id_from_tool_substruct(
    adapter: OpencodeHttpAdapter,
) -> None:
    """CRITICAL: opencode nests the tool-call linkage at
    `props.tool.callID`, NOT `props.toolCallID`. The previous code
    looked for `toolCallID` and got `None`, breaking the UI's ability
    to attribute approvals to their tool call.

    Schema (permission/index.ts:36-49):
      `{id, sessionID, permission, patterns, metadata, always,
        tool?: {messageID, callID}}`

    The `always` globs are the scope an "always" reply would record, so a real
    one is passed here and the ask offers all four decisions.
    """
    out = adapter._translate(
        _event(
            "permission.asked",
            id="req-1",
            sessionID="opencode-sid",
            permission="run",
            patterns=["git status"],
            metadata={},
            always=["git status*"],
            tool={"messageID": "m1", "callID": "call-abc"},
        )
    )
    assert isinstance(out, PermissionRequest)
    assert out.request_id == "req-1"
    assert out.tool_call_id == "call-abc"
    assert out.patterns == ["git status"]
    assert [o.option_id for o in out.options] == [
        "allow_once",
        "allow_always",
        "reject_once",
        "reject_always",
    ]


def test_permission_asked_without_tool_substruct_has_null_call_id(
    adapter: OpencodeHttpAdapter,
) -> None:
    out = adapter._translate(
        _event(
            "permission.asked",
            id="req-1",
            sessionID="opencode-sid",
            permission="run",
            patterns=[],
            metadata={},
            always=[],
        )
    )
    assert isinstance(out, PermissionRequest)
    assert out.tool_call_id is None


@pytest.mark.parametrize(
    ("native", "canonical"),
    [
        # opencode native (vendor/opencode/.../tool/*.ts permission keys)
        # → canonical kind the harness-agnostic policy code consumes.
        ("edit", "edit"),
        ("bash", "shell"),
        ("webfetch", "network"),
        ("websearch", "network"),
        ("task", "task"),
        ("external_directory", "external"),
        # Unmapped / read-only-ish tools fall through to "other".
        ("read", "other"),
        ("glob", "other"),
        ("grep", "other"),
        ("lsp", "other"),
        ("todowrite", "other"),
        ("repo_overview", "other"),
        ("repo_clone", "other"),
        ("skill", "other"),
        # Forward-compat: an unknown future opencode permission key
        # MUST NOT raise — defaults to "other".
        ("future_unknown_kind", "other"),
    ],
)
def test_permission_asked_maps_native_kind_to_canonical(
    vendor_shell_adapter: OpencodeHttpAdapter, native: str, canonical: str
) -> None:
    """Every opencode-native permission key must translate to a
    `CanonicalPermissionKind`. The native string is preserved verbatim
    on `permission_kind`; the canonical lives on `canonical_kind`. Policy
    code (mode_auto_decision) reasons only over canonical."""
    out = vendor_shell_adapter._translate(
        _event(
            "permission.asked",
            id="req-1",
            sessionID="opencode-sid",
            permission=native,
            patterns=[],
            metadata={},
            always=[],
        )
    )
    assert isinstance(out, PermissionRequest)
    assert out.permission_kind == native
    assert out.canonical_kind == canonical


def _asked(
    adapter: OpencodeHttpAdapter,
    *,
    permission: str,
    patterns: list[str],
    metadata: dict[str, object] | None = None,
) -> PermissionRequest:
    out = adapter._translate(
        _event(
            "permission.asked",
            id="r",
            sessionID="opencode-sid",
            permission=permission,
            patterns=patterns,
            metadata=metadata or {},
            always=[],
        )
    )
    assert isinstance(out, PermissionRequest)
    return out


def test_permission_asked_bash_carries_classified_subject(
    vendor_shell_adapter: OpencodeHttpAdapter,
) -> None:
    # bash patterns are classified by the shell classifier → effect on the subject
    # so the runtime policy can reason over it (rm -rf hits the floor).
    safe = _asked(vendor_shell_adapter, permission="bash", patterns=["ls -la"])
    assert safe.subject is not None
    assert safe.subject["capability"] == "shell"
    assert safe.subject["effect"] == "read"

    danger = _asked(vendor_shell_adapter, permission="bash", patterns=["ls", "rm -rf /tmp/x"])
    assert danger.subject is not None
    assert danger.subject["effect"] == "destroy"  # MAX over the compound command


def test_permission_asked_bash_empty_patterns_fails_closed(
    vendor_shell_adapter: OpencodeHttpAdapter,
) -> None:
    out = _asked(vendor_shell_adapter, permission="bash", patterns=[])
    assert out.subject is not None
    assert out.subject["effect"] == "write"
    assert out.subject["confidence"] == "unknown"


def test_permission_asked_read_tool_is_fs_read_subject(adapter: OpencodeHttpAdapter) -> None:
    out = _asked(adapter, permission="read", patterns=["/etc/hosts"])
    assert out.subject is not None
    assert out.subject["capability"] == "fs"
    assert out.subject["effect"] == "read"


def test_permission_asked_edit_tool_is_fs_write_subject(adapter: OpencodeHttpAdapter) -> None:
    out = _asked(adapter, permission="edit", patterns=["src/main.py"])
    assert out.subject is not None
    assert out.subject["capability"] == "fs"
    assert out.subject["effect"] == "write"


def test_permission_asked_external_directory_is_fs_write_subject(
    adapter: OpencodeHttpAdapter,
) -> None:
    # Cross-directory access (a path outside the project cwd) is a recoverable fs
    # WRITE so it flows through the policy + auto-mode judge — NOT a subject-less
    # request that would PROMPT in auto (the model can't be judged on it).
    out = _asked(adapter, permission="external_directory", patterns=["/some/dir/**"])
    assert out.subject is not None
    assert out.subject["capability"] == "fs"
    assert out.subject["effect"] == "write"
    assert out.subject["operation"] == "external_directory"


def test_permission_asked_external_directory_recovers_real_path_from_metadata(
    adapter: OpencodeHttpAdapter,
) -> None:
    """opencode fires `external_directory` with `patterns=['<dir>/*']` (the
    allow-rule glob) and the REAL file in `metadata.filepath`. The adapter must
    recover the real path into the descriptor's `raw`/target — not the glob — so
    the permission UI names the actual file. Pins the exact wire shape captured
    from the real opencode harness."""
    out = _asked(
        adapter,
        permission="external_directory",
        patterns=["/etc/*"],
        metadata={"filepath": "/etc/hosts", "parentDir": "/etc"},
    )
    assert out.subject is not None
    assert out.subject["raw"] == "/etc/hosts"  # the real file, NOT the "/etc/*" glob
    assert out.subject["targets"][0]["name"] == "/etc/hosts"
    # The glob still rides `patterns` (the allow-rule); only the descriptor target
    # is the real path.
    assert out.patterns == ["/etc/*"]


def test_permission_asked_edit_recovers_absolute_path_from_metadata(
    adapter: OpencodeHttpAdapter,
) -> None:
    """An `edit`/`write` ask carries `metadata.filepath` (the absolute path) while
    `patterns[0]` is a leading-slash-STRIPPED project-relative form opencode emits
    (`private/var/.../out.txt`). The adapter must use the metadata absolute path so
    the UI can shorten it relative to the workspace — captured from the real wire."""
    out = _asked(
        adapter,
        permission="edit",
        patterns=["private/var/proj/out.txt"],  # opencode's no-leading-slash form
        metadata={"filepath": "/private/var/proj/out.txt"},
    )
    assert out.subject is not None
    assert out.subject["raw"] == "/private/var/proj/out.txt"  # the clean absolute path


def test_permission_asked_repo_clone_keeps_the_repo_url_not_the_cache_path(
    adapter: OpencodeHttpAdapter,
) -> None:
    """`repo_clone` carries the repo URL in `patterns[0]` and a LOCAL cache path in
    `metadata.path` (NOT `filepath`). The metadata-recovery must read only
    `filepath`, so the descriptor keeps the repo URL — never the cache path. Pins
    that the recovery is scoped to `filepath` and can't hijack a URL-target tool."""
    out = _asked(
        adapter,
        permission="repo_clone",
        patterns=["https://github.com/x/y"],
        metadata={"repository": "https://github.com/x/y", "path": "/cache/x-y"},
    )
    assert out.subject is not None
    assert out.subject["raw"] == "https://github.com/x/y"  # the URL, NOT /cache/x-y


def test_external_directory_is_auto_allowed_so_only_the_tool_gate_prompts() -> None:
    """The double-interrupt fix: opencode's `external_directory` gate fires ON TOP
    OF the tool's own gate, so a single `rm /tmp/x` would prompt twice. It is
    auto-allowed in the permission config so ONLY the tool's own action gate
    (bash 'Delete these files?', edit, …) prompts — the meaningful one."""
    from alkera_cli.harness.adapters.opencode_http import _OPENCODE_PERMISSION_ASK

    assert _OPENCODE_PERMISSION_ASK["external_directory"] == "allow"
    # The tool gates that name the real action are NOT auto-allowed — they still
    # prompt (the default "*": "ask"), so the action is never silently run.
    assert "edit" not in _OPENCODE_PERMISSION_ASK  # falls through to "*": "ask"
    # On POSIX the `bash` key names OUR parent-hosted tool, not opencode's native
    # ShellTool: the vendor patch behind ALKERA_PARENT_SHELL drops the native from
    # the advertised set and re-advertises our loopback-MCP tool under the bare
    # name. So it auto-allows exactly like the `alkera_*` / `web_*` globs — the
    # meaningful action gate is our tool's in-tool broker gate.
    #
    # A "deny" here would be wrong twice over: it would gate OUR tool, and it never
    # hid the native anyway (opencode's advertised set has no permission filter, so
    # `deny` only fails at call time). On Windows our tool is unavailable, so the
    # native shell stays and must fall through to "*": "ask" — an "allow" there
    # would hand the native shell a free pass.
    if os.name == "posix":
        assert _OPENCODE_PERMISSION_ASK["bash"] == "allow"
    else:
        assert "bash" not in _OPENCODE_PERMISSION_ASK


def test_permission_asked_edit_carries_diff_preview_on_request(
    adapter: OpencodeHttpAdapter,
) -> None:
    """An edit's `permission.asked` ships `{filepath, diff}` in its metadata.
    The translator carries the rendered diff + counts ON the PermissionRequest
    so the UI can show the change in the permission card BEFORE the user
    decides — the `file.edited` that also carries it lands only AFTER the
    write. The mixed patch has one `+` and one `-` body line; the `+++`/`---`
    headers are excluded."""
    diff = "--- /repo/x.py\n+++ /repo/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    out = adapter._translate(
        _event(
            "permission.asked",
            id="req-edit",
            sessionID="opencode-sid",
            permission="edit",
            patterns=["x.py"],
            metadata={"filepath": "/repo/x.py", "diff": diff},
            always=["*"],
        )
    )
    assert isinstance(out, PermissionRequest)
    assert out.insertions == 1
    assert out.deletions == 1
    # `path` is carried so a consumer can correlate the preview to a write card by
    # file path (the ask's tool-call id is a different id space than the card).
    assert out.preview == {
        "kind": "diff",
        "content": diff,
        "title": "x.py",
        "path": "/repo/x.py",
    }


def test_permission_asked_non_edit_has_no_diff_preview(
    vendor_shell_adapter: OpencodeHttpAdapter,
) -> None:
    """A bash ask carries no `{filepath, diff}` metadata, so the request has
    no preview to render — the fields stay `None` rather than empty stubs."""
    out = _asked(vendor_shell_adapter, permission="bash", patterns=["ls -la"])
    assert out.preview is None
    assert out.insertions is None
    assert out.deletions is None


def test_permission_asked_edit_with_incomplete_metadata_has_no_preview(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Malformed edit metadata — `filepath` present but no `diff` — must not
    build a half-formed preview; the capture guard fails closed and leaves all
    three fields `None`."""
    out = adapter._translate(
        _event(
            "permission.asked",
            id="req-edit",
            sessionID="opencode-sid",
            permission="edit",
            patterns=["x.py"],
            metadata={"filepath": "/repo/x.py"},
            always=["*"],
        )
    )
    assert isinstance(out, PermissionRequest)
    assert out.preview is None
    assert out.insertions is None
    assert out.deletions is None


def test_permission_replied_uses_request_id_not_id(
    adapter: OpencodeHttpAdapter,
) -> None:
    """CRITICAL: opencode uses `requestID`, not `id`, on the reply
    event. Schema (permission/index.ts:71-78):
      `{sessionID, requestID, reply: "once"|"always"|"reject"}`.
    """
    out = adapter._translate(
        _event(
            "permission.replied",
            sessionID="opencode-sid",
            requestID="req-1",
            reply="once",
        )
    )
    assert isinstance(out, PermissionResolved)
    assert out.request_id == "req-1"
    assert out.option_id == "allow_once"


def test_permission_replied_reject_maps_to_reject_once(
    adapter: OpencodeHttpAdapter,
) -> None:
    out = adapter._translate(
        _event(
            "permission.replied",
            sessionID="opencode-sid",
            requestID="req-1",
            reply="reject",
        )
    )
    assert isinstance(out, PermissionResolved)
    assert out.option_id == "reject_once"


# ---------------------------------------------------------------------------
# Unknown event types fall through to RawEvent (forward-compat)
# ---------------------------------------------------------------------------


def test_unknown_event_type_falls_through(adapter: OpencodeHttpAdapter) -> None:
    out = adapter._translate(_event("opencode.future_event_we_dont_know"))
    assert isinstance(out, RawEvent)
    assert out.event_type == "opencode.future_event_we_dont_know"


# ---------------------------------------------------------------------------
# question.asked / question.replied / question.rejected
# ---------------------------------------------------------------------------


def test_question_asked_translates_full_struct(adapter: OpencodeHttpAdapter) -> None:
    """Schema (question/index.ts:58-66):
      `{id, sessionID, questions: Info[], tool?: {messageID, callID}}`
    Each Info: `{question, header, options, multiple?, custom?}`."""
    from alkera_core.schemas.chat import QuestionRequest

    out = adapter._translate(
        _event(
            "question.asked",
            id="q-1",
            sessionID="opencode-sid",
            questions=[
                {
                    "question": "Pick a color",
                    "header": "color",
                    "options": [
                        {"label": "Red", "description": "warm"},
                        {"label": "Blue"},
                    ],
                    "multiple": False,
                    "custom": True,
                }
            ],
            tool={"messageID": "msg-1", "callID": "call-1"},
        )
    )
    assert isinstance(out, QuestionRequest)
    assert out.request_id == "q-1"
    assert out.tool_call_id == "call-1"
    assert len(out.questions) == 1
    q = out.questions[0]
    assert q.question == "Pick a color"
    assert q.header == "color"
    assert q.multiple is False
    assert q.custom is True
    assert [o.label for o in q.options] == ["Red", "Blue"]
    assert q.options[0].description == "warm"
    assert q.options[1].description is None


def test_question_asked_custom_defaults_to_true(adapter: OpencodeHttpAdapter) -> None:
    """Per question/index.ts:43-45 upstream's `custom` defaults to true
    when omitted. Our translator mirrors that default."""
    from alkera_core.schemas.chat import QuestionRequest

    out = adapter._translate(
        _event(
            "question.asked",
            id="q-2",
            sessionID="opencode-sid",
            questions=[{"question": "ok?", "options": []}],
        )
    )
    assert isinstance(out, QuestionRequest)
    assert out.questions[0].custom is True


def test_question_replied_translates_answers(adapter: OpencodeHttpAdapter) -> None:
    """Schema (question/index.ts:78-82): `{sessionID, requestID,
    answers: Answer[]}` where Answer = string[]."""
    from alkera_core.schemas.chat import QuestionAnswered

    out = adapter._translate(
        _event(
            "question.replied",
            sessionID="opencode-sid",
            requestID="q-1",
            answers=[["Red"], ["A", "B"]],
        )
    )
    assert isinstance(out, QuestionAnswered)
    assert out.request_id == "q-1"
    assert out.answers == [["Red"], ["A", "B"]]


def test_question_rejected(adapter: OpencodeHttpAdapter) -> None:
    """Schema (question/index.ts:84-87): `{sessionID, requestID}`."""
    from alkera_core.schemas.chat import QuestionRejected

    out = adapter._translate(
        _event(
            "question.rejected",
            sessionID="opencode-sid",
            requestID="q-1",
        )
    )
    assert isinstance(out, QuestionRejected)
    assert out.request_id == "q-1"


# ---------------------------------------------------------------------------
# session.compacted / todo.updated / file.edited / command.executed
# ---------------------------------------------------------------------------


def test_session_compacted_maps_to_compaction_applied(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Schema (session/compaction.ts:Compacted): `{sessionID}`. We
    emit a CompactionApplied with empty summary — opencode doesn't
    include the summary text in the event."""
    from alkera_core.schemas.chat import CompactionApplied

    out = adapter._translate(_event("session.compacted", sessionID="opencode-sid"))
    assert isinstance(out, CompactionApplied)
    assert out.summary_text == ""


def test_todo_updated_maps_to_plan_updated(adapter: OpencodeHttpAdapter) -> None:
    """Schema (session/todo.ts): `{sessionID, todos: Todo[]}` where
    Todo = `{content, status, priority}`."""
    from alkera_core.schemas.chat import PlanUpdated

    out = adapter._translate(
        _event(
            "todo.updated",
            sessionID="opencode-sid",
            todos=[
                {"content": "write tests", "status": "in_progress", "priority": "high"},
                {"content": "ship it", "status": "pending", "priority": "low"},
            ],
        )
    )
    assert isinstance(out, PlanUpdated)
    assert len(out.entries) == 2
    assert out.entries[0]["content"] == "write tests"
    assert out.entries[0]["status"] == "in_progress"
    assert out.entries[0]["priority"] == "high"


def test_file_edited_maps_to_typed_event(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Schema (file/index.ts:Edited): `{file: string}`. Translates to
    the generic `FileEdited` IR event — the CLI/webview match by
    type, not by opencode-specific string."""
    from alkera_core.schemas.chat import FileEdited

    out = adapter._translate(_event("file.edited", file="/repo/src/foo.py"))
    assert isinstance(out, FileEdited)
    assert out.path == "/repo/src/foo.py"
    # No prior permission.asked → no diff captured.
    assert out.preview is None
    assert out.insertions is None
    assert out.deletions is None


def test_new_file_write_attaches_all_green_diff_preview(
    adapter: OpencodeHttpAdapter,
) -> None:
    """The write tool ships `{filepath, diff}` in the permission-ask
    metadata before publishing `file.edited`. The translator stashes it
    and drains it onto the matching `FileEdited` as a diff preview. A
    new file's `createTwoFilesPatch("", new)` is all-additions → green."""
    from alkera_core.schemas.chat import FileEdited

    diff = "--- /repo/new.py\n+++ /repo/new.py\n@@ -0,0 +1,2 @@\n+line a\n+line b\n"
    adapter._translate(
        _event(
            "permission.asked",
            id="req-1",
            sessionID="opencode-sid",
            permission="edit",
            patterns=["new.py"],
            metadata={"filepath": "/repo/new.py", "diff": diff},
            always=["*"],
        )
    )
    out = adapter._translate(_event("file.edited", file="/repo/new.py"))
    assert isinstance(out, FileEdited)
    assert out.path == "/repo/new.py"
    assert out.preview == {
        "kind": "diff",
        "content": diff,
        "title": "new.py",
        "path": "/repo/new.py",
    }
    # `+++`/`---` headers excluded; two `+` body lines, no `-` lines.
    assert out.insertions == 2
    assert out.deletions == 0


def test_edit_diff_counts_both_additions_and_deletions(
    adapter: OpencodeHttpAdapter,
) -> None:
    """An edit's mixed patch counts `+` and `-` body lines, excluding the
    `+++`/`---` file headers."""
    from alkera_core.schemas.chat import FileEdited

    diff = "--- /repo/x.py\n+++ /repo/x.py\n@@ -1,3 +1,3 @@\n keep\n-old line\n+new line\n keep2\n"
    adapter._translate(
        _event(
            "permission.asked",
            id="req-2",
            sessionID="opencode-sid",
            permission="edit",
            patterns=["x.py"],
            metadata={"filepath": "/repo/x.py", "diff": diff},
            always=["*"],
        )
    )
    out = adapter._translate(_event("file.edited", file="/repo/x.py"))
    assert isinstance(out, FileEdited)
    assert out.insertions == 1
    assert out.deletions == 1


def test_captured_diff_drains_only_to_matching_path(
    adapter: OpencodeHttpAdapter,
) -> None:
    """A stashed diff drains onto the matching path only — a `file.edited`
    for a different path gets no preview, and the stash is popped so it
    can't leak onto a later edit of the original path."""
    from alkera_core.schemas.chat import FileEdited

    diff = "--- /repo/a.py\n+++ /repo/a.py\n@@ -0,0 +1 @@\n+x\n"
    adapter._translate(
        _event(
            "permission.asked",
            id="req-3",
            sessionID="opencode-sid",
            permission="edit",
            patterns=["a.py"],
            metadata={"filepath": "/repo/a.py", "diff": diff},
            always=["*"],
        )
    )
    other = adapter._translate(_event("file.edited", file="/repo/b.py"))
    assert isinstance(other, FileEdited)
    assert other.preview is None

    matched = adapter._translate(_event("file.edited", file="/repo/a.py"))
    assert isinstance(matched, FileEdited)
    assert matched.preview is not None

    # Stash popped — a second edit of the same path gets no stale diff.
    again = adapter._translate(_event("file.edited", file="/repo/a.py"))
    assert isinstance(again, FileEdited)
    assert again.preview is None


def test_command_executed_maps_to_typed_event(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Schema (command/index.ts:Executed): `{name, sessionID,
    arguments, messageID}`. Translates to the generic
    `CommandExecuted` IR event."""
    from alkera_core.schemas.chat import CommandExecuted

    out = adapter._translate(
        _event(
            "command.executed",
            sessionID="opencode-sid",
            name="help",
            arguments="",
            messageID="m1",
        )
    )
    assert isinstance(out, CommandExecuted)
    assert out.name == "help"
    assert out.arguments == ""
    assert out.message_id == "m1"


def test_session_diff_falls_through_as_raw_event(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Schema (session/session.ts:Diff): `{sessionID, diff: FileDiff[]}`.
    Falls through to RawEvent — opencode's FileDiff shape is rich +
    harness-specific and we haven't designed a generic IR for it yet.
    The chat.jsonl record preserves the data via Pydantic extras
    so future readers don't lose it."""
    out = adapter._translate(
        _event(
            "session.diff",
            sessionID="opencode-sid",
            diff=[{"path": "foo.py", "before": "a", "after": "b"}],
        )
    )
    assert isinstance(out, RawEvent)
    assert out.event_type == "session.diff"


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def test_compaction_captures_summary_and_suppresses_summary_message(
    adapter: OpencodeHttpAdapter,
) -> None:
    """The full opencode compaction sequence
    (`compaction.ts` / `prompt.ts`):

      compaction part (boundary, carries tail_start_id)
      → assistant message `summary:true`
      → its text parts (the summary markdown)
      → session.compacted

    must surface as exactly ONE `CompactionApplied` (summary text joined +
    the elided ids before tail_start_id), with the summary:true message and
    its parts SUPPRESSED so the raw template never renders as a chat turn. The
    summary message's CREATE additionally emits a `compacting` phase so the UI
    opens its "Compacting…" card the moment the fold starts."""
    t = adapter._translate

    # Pre-compaction history → populates message_order.
    assert isinstance(
        t(_event("message.updated", info={"id": "u1", "role": "user"})),
        MessageCreated,
    )
    assert isinstance(
        t(
            _event(
                "message.updated",
                info={
                    "id": "a1",
                    "role": "assistant",
                    "time": {"created": 1, "completed": 2},
                    "finish": "stop",
                    "tokens": {
                        "input": 1,
                        "output": 1,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                },
            )
        ),
        MessageCompleted,
    )
    # u2 is the first RETAINED message (the compaction tail boundary).
    assert isinstance(
        t(_event("message.updated", info={"id": "u2", "role": "user"})),
        MessageCreated,
    )

    # Compaction boundary: the synthetic user message + its compaction part.
    assert isinstance(
        t(_event("message.updated", info={"id": "ucomp", "role": "user"})),
        MessageCreated,
    )
    assert (
        t(
            _event(
                "message.part.updated",
                part={
                    "id": "pc",
                    "messageID": "ucomp",
                    "type": "compaction",
                    "auto": False,
                    "tail_start_id": "u2",
                },
            )
        )
        is None
    )

    # The summary:true assistant message's CREATE signals "compaction begun"
    # (running/compacting) — the one place the start is detectable. Its text
    # part is captured into the summary buffer, not streamed.
    begun = t(
        _event(
            "message.updated",
            info={"id": "asum", "role": "assistant", "summary": True},
        )
    )
    assert isinstance(begun, SessionStatusChanged)
    assert begun.status == "running"
    assert begun.phase == "compacting"
    assert (
        t(
            _event(
                "message.part.updated",
                part={
                    "id": "ps",
                    "messageID": "asum",
                    "type": "text",
                    "text": "SUMMARY-MARKER body",
                    "time": {"start": 1, "end": 2},
                },
            )
        )
        is None
    )

    # session.compacted finalizes the enriched CompactionApplied.
    ev = t(_event("session.compacted", sessionID="oc-sid"))
    assert isinstance(ev, CompactionApplied)
    assert ev.summary_text == "SUMMARY-MARKER body"
    # Everything before the retained tail (u2) is elided.
    assert ev.summarised_message_ids == ["u1", "a1"]


_TWO_PARTS = [("ps", "Let me review."), ("ps2", "## Goal\n- depot report")]


@pytest.mark.parametrize(
    ("initial", "rewrites", "expected"),
    [
        pytest.param(
            [("ps", "Let me review.\n\n## Goal\n- depot report")],
            [("ps", "## Goal\n- depot report")],
            "## Goal\n- depot report",
            id="a-rewritten-part-replaces-its-earlier-text",
        ),
        pytest.param(
            _TWO_PARTS,
            [("ps", "## Goal\n- depot report"), ("ps2", "")],
            "## Goal\n- depot report",
            id="the-first-part-takes-the-whole-and-the-emptied-second-drops-out",
        ),
        pytest.param(
            _TWO_PARTS,
            [("ps2", "")],
            "Let me review.",
            id="emptying-one-part-leaves-the-others-in-place",
        ),
        pytest.param(
            _TWO_PARTS,
            [],
            "Let me review.\n\n## Goal\n- depot report",
            id="without-a-rewrite-the-parts-join-in-arrival-order",
        ),
    ],
)
def test_a_rewritten_summary_part_replaces_rather_than_repeats(
    adapter: OpencodeHttpAdapter,
    initial: list[tuple[str, str]],
    rewrites: list[tuple[str, str]],
    expected: str,
) -> None:
    """opencode normalizes the summary AFTER its parts were finalized: the first
    text part is rewritten to the cleaned template and any further text part is
    rewritten to empty. Each rewrite arrives as another finalized
    `message.part.updated` for the SAME part id, so the buffer is keyed by part —
    a rewrite replaces that part's text, an emptied part drops out, and the
    `CompactionApplied` carries the cleaned summary once, never
    "before + after"."""
    t = adapter._translate
    assert isinstance(
        t(_event("message.updated", info={"id": "asum", "role": "assistant", "summary": True})),
        SessionStatusChanged,
    )

    def finalized(part_id: str, text: str) -> None:
        assert (
            t(
                _event(
                    "message.part.updated",
                    part={
                        "id": part_id,
                        "messageID": "asum",
                        "type": "text",
                        "text": text,
                        "time": {"start": 1, "end": 2},
                    },
                )
            )
            is None
        )

    for part_id, text in [*initial, *rewrites]:
        finalized(part_id, text)

    ev = t(_event("session.compacted", sessionID="oc-sid"))
    assert isinstance(ev, CompactionApplied)
    assert ev.summary_text == expected


def test_compaction_summary_message_emits_compacting_once(
    adapter: OpencodeHttpAdapter,
) -> None:
    """The summary:true message's CREATE emits the `compacting` phase exactly
    once; its later `message.updated`s (the same in-flight message) are silent —
    so the UI opens one card, not a flapping status."""
    t = adapter._translate
    summary = _event(
        "message.updated",
        info={"id": "asum", "role": "assistant", "summary": True},
    )
    first = t(summary)
    assert isinstance(first, SessionStatusChanged)
    assert first.phase == "compacting"
    # The same summary message updates again (e.g. token deltas land) — no second
    # `compacting`, because the start was already announced.
    assert t(summary) is None


def test_compaction_suppresses_summary_message_deltas(
    adapter: OpencodeHttpAdapter,
) -> None:
    """Live token deltas of the summary:true message are dropped (the
    summary is surfaced once, via CompactionApplied — never streamed). The
    message's CREATE emits the `compacting` phase (the fold-start signal)."""
    t = adapter._translate
    begun = t(
        _event(
            "message.updated",
            info={"id": "asum", "role": "assistant", "summary": True},
        )
    )
    assert isinstance(begun, SessionStatusChanged)
    assert begun.phase == "compacting"
    assert (
        t(
            _event(
                "message.part.delta",
                messageID="asum",
                partID="ps",
                field="text",
                delta="chunk",
            )
        )
        is None
    )


def test_compaction_without_tail_or_summary_is_still_valid(
    adapter: OpencodeHttpAdapter,
) -> None:
    """A bare session.compacted (no captured summary/boundary — e.g. a
    no-op compaction) still yields a valid CompactionApplied with empty
    fields rather than raising."""
    ev = adapter._translate(_event("session.compacted", sessionID="oc-sid"))
    assert isinstance(ev, CompactionApplied)
    assert ev.summary_text == ""
    assert ev.summarised_message_ids == []


def test_compaction_failure_resets_state_and_surfaces_error(
    adapter: OpencodeHttpAdapter,
) -> None:
    """If compaction FAILS — the summarization model call errors, so opencode
    publishes `session.error` and never `session.compacted` — surface the
    error (SessionStatusChanged) AND drop the half-captured summary so its
    stale text can't leak into a later compaction."""
    t = adapter._translate
    # A compaction boundary + a summary:true message whose text partly streamed.
    assert (
        t(
            _event(
                "message.part.updated",
                part={
                    "id": "pc",
                    "messageID": "ucomp",
                    "type": "compaction",
                    "auto": False,
                    "tail_start_id": "u1",
                },
            )
        )
        is None
    )
    begun = t(
        _event(
            "message.updated",
            info={"id": "asum", "role": "assistant", "summary": True},
        )
    )
    assert isinstance(begun, SessionStatusChanged)
    assert begun.phase == "compacting"
    assert (
        t(
            _event(
                "message.part.updated",
                part={
                    "id": "ps",
                    "messageID": "asum",
                    "type": "text",
                    "text": "PARTIAL-SUMMARY",
                    "time": {"end": 1},
                },
            )
        )
        is None
    )
    # The summarization call errors.
    err = t(
        _event(
            "session.error",
            error={"name": "ProviderError", "data": {"message": "boom"}},
        )
    )
    assert isinstance(err, SessionStatusChanged)
    assert err.status == "error"
    # State reset: a subsequent (unrelated) session.compacted yields an EMPTY
    # summary — the failed compaction's partial text did not leak.
    ev = t(_event("session.compacted", sessionID="oc-sid"))
    assert isinstance(ev, CompactionApplied)
    assert ev.summary_text == ""


# ---------------------------------------------------------------------------
# Clear (translator reset)
# ---------------------------------------------------------------------------


def test_reset_drops_all_cross_event_state(
    adapter: OpencodeHttpAdapter,
) -> None:
    """`/clear` mints a fresh opencode session; the translator must forget
    ALL cross-event state (open parts, message-order ledger, in-flight
    compaction capture) so stale ids from the old session can't bleed
    across the boundary into post-clear events."""
    ctx = adapter._translator_ctx
    ctx.open_parts["p1"] = _OpenPart(message_id="m1", part_id="p1", part_type="text", buffer=["x"])
    ctx.message_order.extend(["u1", "a1"])
    ctx.summary_message_id = "asum"
    ctx.summary_buffer["ps"] = "PARTIAL"
    ctx.compaction_tail_start_id = "u2"
    ctx.open_attempt("t")

    adapter._translator.reset()

    assert ctx.open_parts == {}
    assert ctx.message_order == []
    assert ctx.summary_message_id is None
    assert ctx.summary_buffer == {}
    assert ctx.compaction_tail_start_id is None
    assert not ctx.attempt_live and ctx.pending_attempt is None


# ---------------------------------------------------------------------------
# EVERY gating opencode permission kind must get a typed descriptor — a
# subject-LESS kind would PROMPT in auto (the model can't be judged on it). This
# matrix is the regression net for the external_directory / repo_clone bug.
# ---------------------------------------------------------------------------

import pytest as _pytest  # noqa: E402
from alkera_cli.contracts.tool_types import Effect as _Effect  # noqa: E402
from alkera_cli.harness.adapters.opencode_translate import (  # noqa: E402
    _descriptor_for_opencode,
)


@_pytest.mark.parametrize(
    "kind,capability,effect",
    [
        ("edit", "fs", _Effect.WRITE),
        ("read", "fs", _Effect.READ),
        ("glob", "fs", _Effect.READ),
        ("grep", "fs", _Effect.READ),
        ("list", "fs", _Effect.READ),
        ("lsp", "fs", _Effect.READ),
        # An outbound fetch is an exfiltration channel (the URL carries the
        # payload), so it is EGRESS — it must never auto-allow as a read.
        ("webfetch", "network", _Effect.EGRESS),
        ("websearch", "network", _Effect.READ),
        ("external_directory", "fs", _Effect.WRITE),
        ("repo_clone", "fs", _Effect.WRITE),
        ("repo_overview", "fs", _Effect.READ),
        # meta-signals the model can trigger — modeled so auto judges them, not prompts
        ("doom_loop", "shell", _Effect.WRITE),
        ("workflow_tool_approval", "shell", _Effect.WRITE),
    ],
)
def test_every_gating_opencode_kind_has_a_descriptor(
    kind: str, capability: str, effect: _Effect
) -> None:
    # A workspace root is supplied because the directory-walking kinds
    # (grep/glob/lsp/…) fail CLOSED to EGRESS when there is no location to check
    # — see apps/cli/tests/harness/test_opencode_fs_sensitive_floor.py. Here we're pinning
    # the base capability/effect mapping, so give them a benign root.
    d = _descriptor_for_opencode(kind, ["/some/path"], workspace_root=Path("/srv/app"))
    assert d is not None, f"opencode kind {kind!r} is subject-less → would PROMPT in auto"
    assert d.capability == capability
    assert d.effect == effect


def test_bash_opencode_kind_is_classified_by_command() -> None:
    # bash routes through the shell classifier — a read auto-allows, rm floors.
    assert _descriptor_for_opencode("bash", ["ls -la"]).effect == _Effect.READ
    assert _descriptor_for_opencode("bash", ["rm -rf x"]).effect == _Effect.DESTROY
    # an empty bash ask fails CLOSED to a prompting write (never silently allowed)
    empty = _descriptor_for_opencode("bash", [])
    assert empty is not None and empty.effect == _Effect.WRITE and empty.confidence == "unknown"


def test_task_kind_stays_subject_less() -> None:
    # `task` (subagent spawn) is denied upstream, not gated here — no descriptor.
    assert _descriptor_for_opencode("task", ["explore"]) is None


def test_no_opencode_gating_kind_is_subject_less_in_auto() -> None:
    """INVENTORY-DERIVED net for the external_directory / doom_loop bug class:
    every opencode permission key that GATES (isn't `allow`/`deny` in our config)
    must carry a descriptor — a subject-less kind PROMPTS the human in auto mode.
    The list is the full set the vendored opencode can `.ask()` (grep
    `permission: "..."` over vendor/opencode/...); a NEW key here is the signal to
    map it. (Hardcoding a smaller list is exactly how doom_loop escaped.)"""
    from alkera_cli.harness.adapters.opencode_http import _OPENCODE_PERMISSION_ASK

    # Every key the vendored opencode emits a `permission.asked` for.
    emittable = {
        "bash",
        "edit",
        "read",
        "glob",
        "grep",
        "list",
        "lsp",
        "webfetch",
        "websearch",
        "external_directory",
        "repo_clone",
        "repo_overview",
        "doom_loop",
        "workflow_tool_approval",
        "task",
        "todowrite",
        "skill",
    }
    explicitly_allowed = {"question", "plan_present"}
    for kind in emittable:
        config = _OPENCODE_PERMISSION_ASK.get(kind, _OPENCODE_PERMISSION_ASK["*"])
        if config in ("allow", "deny") or kind in explicitly_allowed:
            continue  # auto-allowed or disabled upstream — never reaches the gate
        d = _descriptor_for_opencode(kind, ["x"])
        assert d is not None, (
            f"opencode kind {kind!r} GATES but is subject-less → it would PROMPT the "
            f"human in auto mode. Map it in _OPENCODE_KIND_TO_ACTION."
        )


# ---------------------------------------------------------------------------
# Attempt attribution, driven through the real SSE pump.
#
# opencode's status frames carry no attempt identity, so the adapter decides it
# from run boundaries: every run opens with `busy`, and once a run has published
# its first terminal the rest of its tail is redundant. What a cancel, a clear,
# or a crash does to the same state is owned at the adapter seam, in
# `test_harness_crash_invariant.py`.
# ---------------------------------------------------------------------------


class _ScriptedSse:
    """Minimal stand-in for httpx_sse's EventSource: replays a fixed list of
    native opencode events through the adapter's real `_iter_sse` pump, so a
    test exercises the SAME publish path the live SSE stream drives."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._frames = [type("Frame", (), {"data": json.dumps(e)})() for e in events]

    async def aiter_sse(self) -> Any:
        for frame in self._frames:
            yield frame


async def _pump(adapter: OpencodeHttpAdapter, events: list[dict[str, Any]]) -> list[Any]:
    """Feed `events` through the adapter's real SSE pump and return everything a
    UI subscriber saw on the bus. Live pub/sub has no replay, so the subscription
    opens before the pump."""
    sub = adapter._bus.subscribe()
    await adapter._iter_sse(_ScriptedSse(events))  # type: ignore[arg-type]
    return await drain(sub)


async def _wire_send(adapter: OpencodeHttpAdapter, turn_id: str) -> None:
    """The translator suites start from a bare adapter, so the transport is
    attached on the first send rather than by the fixture."""
    adapter._state.http_client = StubTransport()  # type: ignore[assignment]
    adapter._state.started = True
    adapter._state.opencode_session_id = NATIVE_SESSION
    await send(adapter, turn_id)


async def test_each_attempt_publishes_one_terminal_stamped_with_itself(
    adapter: OpencodeHttpAdapter,
) -> None:
    """However many terminal-shaped frames a run's tail carries, each attempt
    publishes exactly one terminal, stamped with itself."""
    await _wire_send(adapter, "A")
    tail = native_frames("busy", "busy", "error", "idle", "session.idle", "idle", "session.idle")
    assert statuses(await _pump(adapter, tail)) == [
        ("running", "A"),
        ("running", "A"),
        ("error", "A"),
    ]

    await _wire_send(adapter, "B")
    clean = native_frames("busy", "idle", "session.idle")
    assert statuses(await _pump(adapter, clean)) == [("running", "B"), ("idle", "B")]


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        pytest.param(("error",), [("error", "B")], id="stranded"),
        pytest.param(
            ("idle", "busy", "idle"),
            [("idle", "B"), ("running", "B"), ("idle", "B")],
            id="fresh run",
        ),
    ],
)
async def test_a_prompt_sent_into_a_live_run_inherits_its_outcome(
    adapter: OpencodeHttpAdapter, tail: tuple[str, ...], expected: list[tuple[str, str]]
) -> None:
    """A prompt sent into a live run inherits whatever outcome that run reaches,
    because opencode's `ensureRunning` joins it to the loop already running."""
    await _wire_send(adapter, "A")
    assert statuses(await _pump(adapter, native_frames("busy"))) == [("running", "A")]

    await _wire_send(adapter, "B")
    assert statuses(await _pump(adapter, native_frames(*tail))) == expected


async def test_the_old_sessions_tail_is_dropped_after_a_clear(
    adapter: OpencodeHttpAdapter,
) -> None:
    """After a clear, frames naming the old native session are dropped while a
    session-less error still lands."""
    adapter._state.opencode_session_id = "new-sid"
    stale = native_frames("busy", "error", "idle", session="old-sid")
    ours = _event("session.error", error={"data": {"message": "plugin died"}})

    assert statuses(await _pump(adapter, [*stale, ours])) == [("error", None)]


# ---------------------------------------------------------------------------
# permission.asked — the ask names the PART the transcript keys the call by
# ---------------------------------------------------------------------------


def _tool_part(part_id: str, call_id: str, status: str) -> dict[str, Any]:
    return _event(
        "message.part.updated",
        part={
            "id": part_id,
            "sessionID": "opencode-sid",
            "messageID": "m1",
            "type": "tool",
            "tool": "write",
            "callID": call_id,
            "state": {"status": status, "input": {"filePath": "x.txt", "content": "hi"}},
        },
    )


def _ask_for(call_id: str) -> dict[str, Any]:
    return _event(
        "permission.asked",
        id="req-1",
        sessionID="opencode-sid",
        permission="edit",
        patterns=["x.txt"],
        metadata={},
        always=[],
        tool={"messageID": "m1", "callID": call_id},
    )


def test_permission_asked_names_the_part_the_transcript_keys_the_call_by(
    adapter: OpencodeHttpAdapter,
) -> None:
    """opencode announces the call as a part (``prt_…``) and raises the ask
    under the provider's call id (``call_…``). Every ``tool.call`` /
    ``tool.call_update`` on the transcript is keyed by the part, so an ask
    that only named the provider's id could not be matched back to its call
    by anything reading the transcript later. The ask carries the part id as
    its call id once the part has been seen, with the provider's id alongside;
    the call itself records the provider's id too."""
    started = adapter._translate(_tool_part("prt-1", "call-abc", "running"))
    assert isinstance(started, ToolCall)
    assert started.tool_call_id == "prt-1"
    assert started.provider_call_id == "call-abc"

    out = adapter._translate(_ask_for("call-abc"))
    assert isinstance(out, PermissionRequest)
    assert out.tool_call_id == "prt-1", "the transcript's key for the call"
    assert out.provider_call_id == "call-abc"


def test_permission_asked_before_its_part_keeps_the_provider_id_under_both_names(
    adapter: OpencodeHttpAdapter,
) -> None:
    """An ask that arrives before the part was ever seen (a dropped frame) has
    only the provider's id — it is carried under both names, never invented."""
    out = adapter._translate(_ask_for("call-unseen"))
    assert isinstance(out, PermissionRequest)
    assert out.tool_call_id == "call-unseen"
    assert out.provider_call_id == "call-unseen"


def test_permission_asked_does_not_map_onto_another_calls_part(
    adapter: OpencodeHttpAdapter,
) -> None:
    """The part → provider-id binding is exact: an ask for a different call
    does not borrow the part of the one that was seen."""
    adapter._translate(_tool_part("prt-1", "call-abc", "running"))
    out = adapter._translate(_ask_for("call-other"))
    assert isinstance(out, PermissionRequest)
    assert out.tool_call_id == "call-other"


def test_a_closed_parts_provider_id_is_forgotten(adapter: OpencodeHttpAdapter) -> None:
    """The binding lives as long as the part is open; a later ask reusing a
    provider id after its part closed maps to nothing stale."""
    adapter._translate(_tool_part("prt-1", "call-abc", "running"))
    adapter._translate(_tool_part("prt-1", "call-abc", "completed"))
    out = adapter._translate(_ask_for("call-abc"))
    assert isinstance(out, PermissionRequest)
    assert out.tool_call_id == "call-abc"


def _generic_ask(request_id: str = "req-race") -> dict[str, Any]:
    """opencode's ask for an MCP-hosted tool: the rule glob and nothing else."""
    return _event(
        "permission.asked",
        id=request_id,
        sessionID="opencode-sid",
        permission="bash",
        patterns=["*"],
        metadata={},
        always=["*"],
        tool={"messageID": "m1", "callID": "call-race"},
    )


def _running_bash_part(command: str = "git status") -> dict[str, Any]:
    return _event(
        "message.part.updated",
        part={
            "id": "prt-race",
            "sessionID": "opencode-sid",
            "messageID": "m1",
            "type": "tool",
            "callID": "call-race",
            "tool": "bash",
            "state": {"status": "running", "input": {"command": command}},
        },
    )


def test_an_ask_that_lands_before_its_part_is_raised_again_with_the_command(
    vendor_shell_adapter: OpencodeHttpAdapter,
) -> None:
    """The ask and the part come from two consumers of the model stream with
    nothing ordering them. Ask first: it goes out waiting for its subject, and
    the part that names the command re-raises it — same id — with the command."""
    first = vendor_shell_adapter._translate(_generic_ask())
    assert isinstance(first, PermissionRequest)
    assert first.patterns == []
    assert getattr(first, "subject_pending", None) is True

    out = vendor_shell_adapter._translate(_running_bash_part())
    assert isinstance(out, list) and len(out) == 2
    call, again = out
    assert isinstance(call, ToolCall) and call.provider_call_id == "call-race"
    assert isinstance(again, PermissionRequest)
    assert again.request_id == first.request_id
    assert again.event_id != first.event_id
    assert again.patterns == ["git status"]
    assert again.tool_call_id == "prt-race"
    assert getattr(again, "subject_pending", None) is None
    assert again.subject is not None and again.subject.get("raw") == "git status"

    # Raised once: a later frame for the same call does not raise it a third time.
    later = vendor_shell_adapter._translate(_running_bash_part())
    assert not isinstance(later, list)


def test_an_ask_that_lands_after_its_part_carries_the_command_at_once(
    vendor_shell_adapter: OpencodeHttpAdapter,
) -> None:
    vendor_shell_adapter._translate(_running_bash_part())
    out = vendor_shell_adapter._translate(_generic_ask())
    assert isinstance(out, PermissionRequest)
    assert out.patterns == ["git status"]
    assert getattr(out, "subject_pending", None) is None


def test_an_ask_for_a_call_whose_part_named_no_command_is_not_left_waiting(
    vendor_shell_adapter: OpencodeHttpAdapter,
) -> None:
    """A part already seen with no `command` is a genuine unnamed ask, not one
    whose subject is on the way: nothing later would complete it."""
    part = _running_bash_part()
    part["properties"]["part"]["state"]["input"] = {"path": "/etc/hosts"}
    vendor_shell_adapter._translate(part)
    out = vendor_shell_adapter._translate(_generic_ask())
    assert isinstance(out, PermissionRequest)
    assert out.patterns == []
    assert getattr(out, "subject_pending", None) is None


def test_a_waiting_ask_the_harness_already_replied_to_is_not_raised_again(
    adapter: OpencodeHttpAdapter,
) -> None:
    adapter._translate(_generic_ask())
    adapter._translate(
        _event("permission.replied", sessionID="opencode-sid", requestID="req-race", reply="reject")
    )
    out = adapter._translate(_running_bash_part())
    assert isinstance(out, ToolCall)


def test_an_edit_ask_naming_its_file_in_metadata_never_waits_for_a_part(
    adapter: OpencodeHttpAdapter,
) -> None:
    """An ask whose kind carries its subject on the ask itself (opencode's
    `external_directory` ask names the file in `metadata.filepath`) has nothing
    to wait for, part or no part."""
    out = adapter._translate(
        _event(
            "permission.asked",
            id="req-edit",
            sessionID="opencode-sid",
            permission="external_directory",
            patterns=["/tmp/elsewhere/*"],
            metadata={"filepath": "/tmp/elsewhere/notes.txt"},
            always=["/tmp/elsewhere/*"],
            tool={"messageID": "m1", "callID": "call-edit"},
        )
    )
    assert isinstance(out, PermissionRequest)
    assert getattr(out, "subject_pending", None) is None
    assert out.subject is not None
    assert adapter._translator_ctx.asks_awaiting_part == {}


def test_a_waiting_ask_whose_part_never_lands_is_raised_as_it_is_when_the_wait_is_up(
    vendor_shell_adapter: OpencodeHttpAdapter,
) -> None:
    """The policy leaves a waiting ask alone, so one whose part never comes (a
    frame lost on an SSE reconnect) would hold the turn forever. Once the wait
    runs out it is raised as it is, ahead of whatever frame noticed the time,
    and only once."""
    from freezegun import freeze_time

    vendor_shell_adapter._translator_ctx.ask_subject_wait_seconds = 5.0
    with freeze_time("2026-09-21T12:00:00Z") as frozen:
        first = vendor_shell_adapter._translate(_generic_ask())
        assert isinstance(first, PermissionRequest)
        assert getattr(first, "subject_pending", None) is True

        frozen.tick(4.0)
        early = vendor_shell_adapter._translate(_event("server.heartbeat"))
        assert isinstance(early, Heartbeat), "still within the wait: nothing raised"

        frozen.tick(1.5)
        out = vendor_shell_adapter._translate(_event("server.heartbeat"))
        assert isinstance(out, list) and len(out) == 2
        raised, beat = out
        assert isinstance(raised, PermissionRequest)
        assert raised.request_id == first.request_id
        assert raised.patterns == []
        assert getattr(raised, "subject_pending", None) is None
        assert isinstance(beat, Heartbeat)
        assert vendor_shell_adapter._translator_ctx.asks_awaiting_part == {}

        again = vendor_shell_adapter._translate(_event("server.heartbeat"))
        assert isinstance(again, Heartbeat)


def test_a_waiting_ask_is_raised_as_it_is_when_the_session_goes_idle(
    vendor_shell_adapter: OpencodeHttpAdapter,
) -> None:
    """No part can follow an idle: the turn is over, and the ask must go out
    before its wait is up or the idle is the wedge."""
    vendor_shell_adapter._translator_ctx.ask_subject_wait_seconds = 3600.0
    vendor_shell_adapter._translate(_generic_ask())
    out = vendor_shell_adapter._translate(
        _event("session.status", sessionID="opencode-sid", status={"type": "idle"})
    )
    assert isinstance(out, list)
    assert isinstance(out[0], PermissionRequest)
    assert out[0].request_id == "req-race"
    assert getattr(out[0], "subject_pending", None) is None


def test_the_waiting_asks_are_capped_and_the_oldest_makes_room(
    vendor_shell_adapter: OpencodeHttpAdapter,
) -> None:
    vendor_shell_adapter._translator_ctx.ask_subject_wait_max = 2
    for n in range(3):
        ask = _generic_ask(f"req-{n}")
        ask["properties"]["tool"]["callID"] = f"call-{n}"
        vendor_shell_adapter._translate(ask)
    assert list(vendor_shell_adapter._translator_ctx.asks_awaiting_part) == ["call-1", "call-2"]


# ---------------------------------------------------------------------------
# what an `always` reply may travel for
# ---------------------------------------------------------------------------


class _RecordingClient:
    """The adapter's upstream, recording what it was told to reply."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str]]] = []

    async def post(self, path: str, json: dict[str, str]) -> Any:
        self.posts.append((path, json))

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

        return _Response()


async def _replied(
    adapter: OpencodeHttpAdapter, request: PermissionRequest, option: str
) -> dict[str, str]:
    """What opencode is told when ``option`` answers ``request``."""
    client = _RecordingClient()
    adapter._state.started = True
    adapter._state.http_client = client  # type: ignore[assignment]
    await adapter.resolve_permission(request.request_id, option)  # type: ignore[arg-type]
    assert len(client.posts) == 1
    return client.posts[0][1]


def _scoped_ask(
    adapter: OpencodeHttpAdapter, *, permission: str, patterns: list[str], always: list[str]
) -> PermissionRequest:
    out = adapter._translate(
        _event(
            "permission.asked",
            id="r-always",
            sessionID="opencode-sid",
            permission=permission,
            patterns=patterns,
            metadata={},
            always=always,
        )
    )
    assert isinstance(out, PermissionRequest)
    return out


async def test_a_shell_always_never_travels_to_opencode(
    vendor_shell_adapter: OpencodeHttpAdapter,
) -> None:
    """opencode turns an ``always`` into a COARSE prefix rule and then stops
    raising the ask for anything it matches — ``uv *`` in its approved set buys
    every later ``uv`` command unasked, unaudited and past the workspace fence,
    ``uv run python -c 'open("~/.alkera/auth.yml").read()'`` included. Alkera's
    own precise rule is what carries a person's "always" for a shell command, so
    the vendor is told about this one call whatever the answer says."""
    ask = _scoped_ask(
        vendor_shell_adapter, permission="bash", patterns=["uv run pytest"], always=["uv *"]
    )
    assert ask.subject is not None
    assert ask.subject["capability"] == "shell"
    assert await _replied(vendor_shell_adapter, ask, "allow_always") == {"reply": "once"}


async def test_a_scoped_non_shell_always_still_travels(adapter: OpencodeHttpAdapter) -> None:
    """The control: what the scope proof is FOR. A file edit's ask carries a real
    glob and records no shell prefix, so its standing grant reaches opencode and
    the reader is not asked for every file under it."""
    ask = _scoped_ask(adapter, permission="edit", patterns=["src/main.py"], always=["src/**/*.py"])
    assert ask.subject is not None
    assert ask.subject["capability"] == "fs"
    assert await _replied(adapter, ask, "allow_always") == {"reply": "always"}
