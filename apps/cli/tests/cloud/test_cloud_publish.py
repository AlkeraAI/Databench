"""The pure publisher half: roles, the 16 KiB entry bound (a tool result is
shrunk to a preview that keeps the blob handle), and chunk coalescing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from alkera_cli.cloud.publish import (
    AGENT_EXIT_COPY,
    ENTRY_MAX_BYTES,
    MEMORY_LIMIT_COPY,
    MIN_PREVIEW_ROWS,
    PREVIEW_TEXT_CHARS,
    ChunkCoalescer,
    RoleIndex,
    append_entry,
    bound_entry,
    entry_size,
    is_chunk,
)
from alkera_cli.harness.sandbox import memory_limit_detail
from alkera_core.events import MAX_FRAME_BYTES, MAX_PAYLOAD_BYTES
from alkera_core.schemas.chat import (
    AgentMessageChunk,
    AgentThoughtChunk,
    Heartbeat,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PartStarted,
    PermissionOption,
    PermissionRequest,
    QuestionPrompt,
    QuestionRequest,
    SessionStatusChanged,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolCallUpdate,
)
from alkera_core.schemas.objects import ChatTranscriptEntry

_T = datetime(2026, 9, 6, tzinfo=UTC)


def _created(message_id: str, role: str) -> MessageCreated:
    return MessageCreated(
        event_id=f"c-{message_id}",
        time=_T,
        session_id="s",
        message_id=message_id,
        role=role,  # type: ignore[arg-type]
    )


def _text_part(message_id: str, text: str, *, event_id: str = "p1") -> PartCreated:
    return PartCreated(
        event_id=event_id,
        time=_T,
        session_id="s",
        part=TextPart(part_id=f"{event_id}-part", message_id=message_id, text=text),
    )


def _tool_result(output: Any, *, event_id: str = "t1") -> PartCreated:
    return PartCreated(
        event_id=event_id,
        time=_T,
        session_id="s",
        part=ToolCallPart(
            part_id=f"{event_id}-part",
            message_id="m-tool",
            call_id="call-1",
            name="sql.query",
            input={"connection": "pg", "sql": "select 1"},
            state="completed",
            output=output,
        ),
    )


# --------------------------------------------------------------------------- #
# roles
# --------------------------------------------------------------------------- #


def test_a_part_carries_the_role_of_its_message() -> None:
    roles = RoleIndex()
    roles.observe(_created("m-user", "user"))
    roles.observe(_created("m-asst", "assistant"))
    assert roles.role_for(_text_part("m-user", "hi")) == "user"
    assert roles.role_for(_text_part("m-asst", "hello")) == "assistant"
    assert roles.role_for(_text_part("m-unknown", "?")) == "assistant"
    assert (
        roles.role_for(
            MessageCompleted(event_id="mc", time=_T, session_id="s", message_id="m-user")
        )
        == "user"
    )


@pytest.mark.parametrize(
    ("event", "role"),
    [
        pytest.param(_created("m", "user"), "user", id="message-created-user"),
        pytest.param(_created("m", "assistant"), "assistant", id="message-created-assistant"),
        pytest.param(
            ToolCall(event_id="tc", time=_T, session_id="s", tool_call_id="c", message_id="m"),
            "tool",
            id="tool-call",
        ),
        pytest.param(
            ToolCallUpdate(event_id="tu", time=_T, session_id="s", tool_call_id="c"),
            "tool",
            id="tool-call-update",
        ),
        pytest.param(_tool_result({"columns": []}), "tool", id="tool-result-part"),
        pytest.param(
            PartStarted(
                event_id="ps",
                time=_T,
                session_id="s",
                message_id="m",
                part_id="p",
                part_type="tool_call",
            ),
            "tool",
            id="tool-part-started",
        ),
        pytest.param(
            PermissionRequest(
                event_id="pr",
                time=_T,
                session_id="s",
                request_id="r",
                permission_kind="bash",
                options=[PermissionOption(option_id="allow_once", name="Allow")],
            ),
            "assistant",
            id="permission-request",
        ),
        pytest.param(
            QuestionRequest(
                event_id="qr",
                time=_T,
                session_id="s",
                request_id="q",
                questions=[QuestionPrompt(question="Which?")],
            ),
            "assistant",
            id="question-request",
        ),
        pytest.param(
            SessionStatusChanged(event_id="ss", time=_T, session_id="s", status="running"),
            "system",
            id="status-is-system",
        ),
    ],
)
def test_role_of_each_event_kind(event: Any, role: str) -> None:
    assert RoleIndex().role_for(event) == role


@pytest.mark.parametrize(
    ("event", "chunk"),
    [
        pytest.param(
            AgentMessageChunk(
                event_id="k1",
                time=_T,
                session_id="s",
                message_id="m",
                part_id="p",
                sequence=1,
                text="a",
            ),
            True,
            id="message-chunk",
        ),
        pytest.param(
            AgentThoughtChunk(
                event_id="k2",
                time=_T,
                session_id="s",
                message_id="m",
                part_id="p",
                sequence=1,
                text="a",
            ),
            True,
            id="thought-chunk",
        ),
        pytest.param(
            Heartbeat(event_id="h", time=_T, session_id="s", last_activity_ms=0),
            True,
            id="heartbeat",
        ),
        pytest.param(_text_part("m", "x"), False, id="part-created-is-durable"),
    ],
)
def test_is_chunk_names_exactly_the_never_persisted_events(event: Any, chunk: bool) -> None:
    assert is_chunk(event) is chunk


# --------------------------------------------------------------------------- #
# the entry bound
# --------------------------------------------------------------------------- #


def test_an_entry_carries_the_contract_fields_and_the_event_verbatim() -> None:
    roles = RoleIndex()
    roles.observe(_created("m", "assistant"))
    entry = append_entry(_text_part("m", "hello"), roles)
    # The four contract keys the server persists and every reader indexes on,
    # plus what the versioned envelope adds so a row can ever be migrated, plus
    # the sequence slot the SERVER fills in: the machine cannot know it, so an
    # entry leaves the box unstamped and is named when it is recorded.
    assert set(entry) == {
        "event_id",
        "role",
        "kind",
        "payload",
        "schema_version",
        "metadata",
        "seq",
    }
    assert entry["seq"] is None, "the sequence is assigned by the durable write, not the box"
    assert entry["schema_version"] == ChatTranscriptEntry.SCHEMA_VERSION
    assert entry["event_id"] == "p1"
    assert entry["role"] == "assistant"
    assert entry["kind"] == "part.created"
    assert entry["payload"]["part"]["text"] == "hello"
    assert "truncated" not in entry["payload"]
    # And it reads back as the model the server validates it with.
    assert ChatTranscriptEntry.model_validate(entry).model_dump(mode="json") == entry


def test_a_completed_message_publishes_the_usage_the_meter_counted() -> None:
    """The transcript row is where every cost surface reads a turn's usage, so
    the entry carries the harness event's figures unchanged — not a rebuilt
    object that would have to be kept in step with them."""
    roles = RoleIndex()
    roles.observe(_created("m", "assistant"))
    tokens = {"input": 4997, "output": 148, "cache_read": 28032, "cache_write": 0, "total": 33177}
    event = MessageCompleted(
        event_id="mc1",
        time=_T,
        session_id="s",
        message_id="m",
        finish_reason="tool-calls",
        tokens=tokens,
        cost=0.0125,
    )
    entry = append_entry(event, roles)
    assert entry["kind"] == "message.completed"
    assert entry["payload"]["tokens"] == tokens
    assert entry["payload"]["cost"] == 0.0125


def test_the_bound_sits_under_the_event_log_cap() -> None:
    assert 0 < ENTRY_MAX_BYTES < MAX_PAYLOAD_BYTES


def test_what_the_box_publishes_fits_the_frame_the_gateway_accepts() -> None:
    """The three bounds are one chain: what the publisher emits fits the frame
    cap, and the frame cap holds a whole payload. Raising the log's ceiling
    without the frame cap following is what put the mirror in a reconnect loop —
    so a future edit of either constant fails here rather than on a chat."""
    from backend.services.realtime.session import MAX_FRAME_BYTES as GATEWAY_FRAME_BYTES

    assert ENTRY_MAX_BYTES <= MAX_FRAME_BYTES
    assert MAX_PAYLOAD_BYTES <= MAX_FRAME_BYTES
    assert GATEWAY_FRAME_BYTES == MAX_FRAME_BYTES, (
        "the gateway must refuse at the same number the box publishes under"
    )


def _wide_rows(count: int, width: int = 400) -> list[list[Any]]:
    return [[i, "x" * width, "y" * width] for i in range(count)]


def rows_over_the_bound(width: int = 400) -> list[list[Any]]:
    """Just enough wide rows to overflow ``ENTRY_MAX_BYTES``.

    Counted from the bound rather than written down, so a raised payload cap
    moves the fixture with it instead of leaving a row set that quietly fits --
    and kept to a handful of rows past it, because the shrink re-measures the
    whole entry for every row it drops."""
    row_bytes = len(json.dumps([0, "x" * width, "y" * width]).encode()) + 1  # + its comma
    return _wide_rows(ENTRY_MAX_BYTES // row_bytes + 8, width)


def test_a_tool_result_over_the_bound_drops_preview_rows_and_keeps_the_handle() -> None:
    blob = {"sha256": "f" * 64, "size": 123456, "media_type": "application/json"}
    rows = rows_over_the_bound()
    output = {
        "columns": ["id", "a", "b"],
        "preview_rows": rows,
        "row_count": 5000,
        "truncated": True,
        "blob": blob,
    }
    raw = _tool_result(output)
    assert entry_size(append_entry(raw, RoleIndex(), limit=10**9)) > ENTRY_MAX_BYTES
    entry = append_entry(raw, RoleIndex())
    assert entry_size(entry) <= ENTRY_MAX_BYTES
    shrunk = entry["payload"]["part"]["output"]
    assert shrunk["blob"] == blob
    assert shrunk["truncated"] is True
    assert entry["payload"]["truncated"] is True
    assert MIN_PREVIEW_ROWS <= len(shrunk["preview_rows"]) < len(rows)
    assert shrunk["preview_rows"][0] == [0, "x" * 400, "y" * 400]  # the head survives
    assert shrunk["row_count"] == 5000  # the total is still told
    assert shrunk["columns"] == ["id", "a", "b"]


def test_a_text_part_over_the_bound_is_cut_to_a_preview() -> None:
    big = "z" * (ENTRY_MAX_BYTES + 1000)
    entry = append_entry(_text_part("m", big), RoleIndex())
    assert entry_size(entry) <= ENTRY_MAX_BYTES
    assert entry["payload"]["part"]["text"] == "z" * PREVIEW_TEXT_CHARS
    assert entry["payload"]["part"]["truncated"] is True
    assert entry["payload"]["truncated"] is True


def test_the_last_resort_preview_keeps_the_blob_handle() -> None:
    """Rows too wide to shrink below the floor: the payload becomes a text
    preview of itself, with the handle lifted out so the rows stay fetchable."""
    blob = {"sha256": "a" * 64, "size": 99, "media_type": "application/json"}
    output = {
        "columns": ["a"],
        "preview_rows": [["w" * 4000] for _ in range(MIN_PREVIEW_ROWS)],
        "blob": blob,
    }
    entry = bound_entry(
        {
            "event_id": "e",
            "role": "tool",
            "kind": "part.created",
            "payload": {"event_type": "part.created", "out": output},
        },
        limit=6000,
    )
    assert entry_size(entry) <= 6000
    assert entry["payload"]["truncated"] is True
    assert entry["payload"]["blob"] == blob
    assert entry["payload"]["event_type"] == "part.created"
    assert entry["payload"]["preview"].startswith("{")


def test_an_entry_under_the_bound_is_returned_untouched() -> None:
    entry = {"event_id": "e", "role": "tool", "kind": "k", "payload": {"x": 1}}
    assert bound_entry(entry) is entry


# --------------------------------------------------------------------------- #
# chunk coalescing
# --------------------------------------------------------------------------- #


def _chunk(seq: int, text: str, *, part: str = "p", final: bool = False) -> AgentMessageChunk:
    return AgentMessageChunk(
        event_id=f"k{seq}",
        time=_T,
        session_id="s",
        message_id="m",
        part_id=part,
        sequence=seq,
        text=text,
        is_final=final,
    )


def test_consecutive_deltas_of_one_part_merge_into_one_chunk() -> None:
    coalescer = ChunkCoalescer()
    coalescer.add(_chunk(1, "Hel"))
    coalescer.add(_chunk(2, "lo "))
    coalescer.add(_chunk(3, "world", final=True))
    assert len(coalescer) == 1
    [merged] = coalescer.flush()
    assert merged["text"] == "Hello world"
    assert merged["sequence"] == 3
    assert merged["is_final"] is True
    assert merged["event_type"] == "agent.message_chunk"
    assert coalescer.flush() == []


def test_deltas_of_different_parts_stay_separate_in_first_seen_order() -> None:
    coalescer = ChunkCoalescer()
    coalescer.add(_chunk(1, "a", part="p1"))
    coalescer.add(_chunk(1, "b", part="p2"))
    coalescer.add(_chunk(2, "c", part="p1"))
    flushed = coalescer.flush()
    assert [(f["part_id"], f["text"]) for f in flushed] == [("p1", "ac"), ("p2", "b")]


def test_thought_and_message_deltas_never_merge() -> None:
    coalescer = ChunkCoalescer()
    coalescer.add(_chunk(1, "visible"))
    coalescer.add(
        AgentThoughtChunk(
            event_id="t1",
            time=_T,
            session_id="s",
            message_id="m",
            part_id="p",
            sequence=1,
            text="hidden",
        )
    )
    flushed = coalescer.flush()
    assert {f["event_type"] for f in flushed} == {"agent.message_chunk", "agent.thought_chunk"}


@pytest.mark.parametrize(
    ("rc", "stopping", "expected"),
    [
        pytest.param(-15, False, "restarted", id="sigterm-mirror-not-yet-stopping"),
        pytest.param(-9, False, "restarted", id="sigkill"),
        pytest.param(1, True, "restarted", id="mirror-knows-it-is-stopping"),
        pytest.param(1, False, "stopped", id="exit-the-mirror-did-not-cause"),
    ],
)
def test_agent_exit_detail_reaches_the_reader_in_the_workspace_s_words(
    rc: int, stopping: bool, expected: str
) -> None:
    """A restarted mirror must not put a signal number in the reader's face."""
    entry = append_entry(
        SessionStatusChanged(
            event_id="crash",
            time=_T,
            session_id="s",
            status="error",
            phase="error",
            detail=f"agent exited unexpectedly (rc={rc})",
        ),
        RoleIndex(),
        stopping=stopping,
    )
    assert entry["payload"]["detail"] == AGENT_EXIT_COPY[expected]
    serialized = json.dumps(entry)
    assert "exited unexpectedly" not in serialized
    assert f"rc={rc}" not in serialized


@pytest.mark.parametrize(
    ("memory_mb", "words"),
    [
        pytest.param(2048, "2 GB", id="free"),
        pytest.param(4096, "4 GB", id="plus"),
        pytest.param(8192, "8 GB", id="pro"),
        pytest.param(1536, "1.5 GB", id="half"),
        pytest.param(512, "512 MB", id="under-a-gigabyte"),
    ],
)
@pytest.mark.parametrize("stopping", [False, True], ids=["running", "mirror-stopping"])
def test_a_kill_for_memory_names_the_chats_limit_never_a_stopped_workspace(
    memory_mb: int, words: str, stopping: bool
) -> None:
    """The adapter read the kill off the chat's cgroup; the reader is told the
    limit and that the process was stopped — whatever the exit code, and even
    while the mirror is going down, since "ask again" would only run into the
    same limit."""
    detail = f"agent exited unexpectedly (rc=137): {memory_limit_detail(memory_mb)}"
    entry = append_entry(
        SessionStatusChanged(
            event_id="oom", time=_T, session_id="s", status="error", phase="error", detail=detail
        ),
        RoleIndex(),
        stopping=stopping,
    )
    assert entry["payload"]["detail"] == (
        f"The process used more than the chat's {words} memory limit and was stopped."
    )
    assert entry["payload"]["detail"] == MEMORY_LIMIT_COPY.format(limit=words)
    serialized = json.dumps(entry)
    assert "rc=137" not in serialized and "exited unexpectedly" not in serialized
    assert "stopped answering" not in serialized


def test_a_failure_that_is_not_an_agent_exit_keeps_its_own_words() -> None:
    entry = append_entry(
        SessionStatusChanged(
            event_id="e",
            time=_T,
            session_id="s",
            status="error",
            phase="error",
            detail="SSE reconnect attempts exhausted",
        ),
        RoleIndex(),
    )
    assert entry["payload"]["detail"] == "SSE reconnect attempts exhausted"


# --------------------------------------------------------------------------- #
# the extreme case: a 50 MB tool result
# --------------------------------------------------------------------------- #

#: A tool result far past anything a harness should produce -- 50 MB of text,
#: 25x the event log's whole payload cap. The bound has to hold at this size
#: without the entry being dropped, split, or turned into something the browser
#: cannot key by the call it belongs to.
_EXTREME_BYTES = 50 * 1024 * 1024


def _extreme_text() -> str:
    """50 MB of 80-column lines -- a ``cat`` of a generated file, the shape an
    agent actually produces when it dumps one."""
    return ("x" * 79 + "\n") * (_EXTREME_BYTES // 80)


def _extreme_tool_update(output: Any) -> ToolCallUpdate:
    return ToolCallUpdate(
        event_id="tu-extreme",
        time=_T,
        session_id="s",
        tool_call_id="call-extreme",
        status="completed",
        output=output,
        metadata={"truncated": True, "outputPath": "/box/.opencode/truncation/tool_01"},
    )


def _find_blob_in(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if isinstance(value.get("sha256"), str):
            return value
        for child in value.values():
            found = _find_blob_in(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_blob_in(child)
            if found is not None:
                return found
    return None


def test_a_fifty_megabyte_tool_result_still_publishes_as_one_bounded_entry() -> None:
    """The whole point of the cap: the row the browser reads fits the event log
    AND one websocket frame, and it is still the same tool call -- the entry is
    not dropped and the turn is not split around it."""
    entry = append_entry(_extreme_tool_update(_extreme_text()), RoleIndex())
    size = entry_size(entry)
    assert size <= ENTRY_MAX_BYTES
    assert size <= MAX_PAYLOAD_BYTES
    assert size <= MAX_FRAME_BYTES
    assert entry["kind"] == "tool.call_update"
    assert entry["event_id"] == "tu-extreme"
    assert entry["role"] == "tool"
    assert entry["payload"]["tool_call_id"] == "call-extreme"
    assert entry["payload"]["status"] == "completed"
    assert entry["payload"]["output"] == _extreme_text()[:PREVIEW_TEXT_CHARS]
    assert entry["payload"]["truncated"] is True


def test_a_cut_entry_says_how_many_bytes_it_lost() -> None:
    """``truncated`` alone tells a reader something is missing; the card has to
    be able to say HOW MUCH, which it can only do if the publisher measures the
    loss before the bytes are gone."""
    entry = append_entry(_extreme_tool_update(_extreme_text()), RoleIndex())
    lost = entry["payload"]["truncated_bytes"]
    # Within a hair of the whole 50 MB: everything but the kept preview went.
    # Measured on the JSON text, where each of the 655k newlines escapes to two
    # bytes, so the figure runs a little OVER the raw size.
    assert _EXTREME_BYTES <= lost <= _EXTREME_BYTES + 1024 * 1024
    # And it is a real measurement, not a constant: half the input loses half
    # the bytes.
    half = append_entry(_extreme_tool_update(_extreme_text()[: _EXTREME_BYTES // 2]), RoleIndex())
    assert half["payload"]["truncated_bytes"] == pytest.approx(lost // 2, rel=0.01)


@pytest.mark.parametrize("limit", [4096, 8192, 65536])
def test_the_loss_figure_is_paid_for_inside_the_bound(limit: int) -> None:
    """The figure is stamped on a payload that has just been shrunk to the
    limit, so a naive stamp would push the entry back over it. Checked at tight
    limits, where a few extra bytes have nowhere to hide."""
    entry = append_entry(_extreme_tool_update(_extreme_text()), RoleIndex(), limit=limit)
    assert entry_size(entry) <= limit
    assert entry["payload"]["truncated_bytes"] > 0


def test_a_fifty_megabyte_result_keeps_the_handle_to_the_whole_output() -> None:
    """An alkera tool spills its full result to the content-addressed blob store
    and returns a preview beside the handle. However hard the entry is cut, that
    handle has to survive -- it is the browser's only way to the whole output."""
    blob = {"sha256": "b" * 64, "size": _EXTREME_BYTES, "media_type": "text/plain"}
    entry = append_entry(
        _extreme_tool_update({"preview": _extreme_text(), "blob": blob, "truncated": True}),
        RoleIndex(),
    )
    assert entry_size(entry) <= ENTRY_MAX_BYTES
    assert entry["payload"]["truncated"] is True
    assert entry["payload"]["truncated_bytes"] > 0
    assert _find_blob_in(entry["payload"]) == blob


def test_a_fifty_megabyte_result_too_wide_to_shrink_still_keeps_its_handle() -> None:
    """The last-resort branch: rows that cannot be dropped below the floor and
    no text field to cut. The payload becomes a preview of itself, and the
    handle is lifted out of it rather than lost with the rest."""
    blob = {"sha256": "c" * 64, "size": _EXTREME_BYTES, "media_type": "application/json"}
    rows = [["y" * (_EXTREME_BYTES // MIN_PREVIEW_ROWS)] for _ in range(MIN_PREVIEW_ROWS)]
    entry = bound_entry(
        {
            "event_id": "e-wide",
            "role": "tool",
            "kind": "tool.call_update",
            "payload": {
                "event_type": "tool.call_update",
                "out": {"columns": ["a"], "preview_rows": rows, "blob": blob},
            },
        },
        limit=8192,
    )
    assert entry_size(entry) <= 8192
    assert entry["payload"]["truncated"] is True
    assert entry["payload"]["blob"] == blob
    assert entry["payload"]["truncated_bytes"] > _EXTREME_BYTES // 2


def test_an_entry_inside_the_bound_is_never_stamped_with_a_loss() -> None:
    """The negative case the marker would be worthless without: a result that
    fit carries no truncation fields at all, so a card cannot claim bytes were
    dropped when none were."""
    entry = append_entry(_extreme_tool_update("a modest result"), RoleIndex())
    assert "truncated_bytes" not in entry["payload"]
    assert entry["payload"].get("truncated") is not True
    assert entry["payload"]["output"] == "a modest result"
