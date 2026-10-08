"""Claude Agent SDK → IR event translator.

Pure-ish dispatch over the typed messages the ``claude-agent-sdk`` yields from
``ClaudeSDKClient.receive_messages()`` plus the raw Anthropic ``StreamEvent``
frames (when ``include_partial_messages=True``). No I/O, no subprocess — testable
by constructing SDK message objects directly. Parallel to
:mod:`alkera_cli.harness.adapters.opencode_translate`.

The mutable cross-event state lives in :class:`_ClaudeTranslatorContext`, which the
adapter holds and passes by reference so its synthesize-close path walks the same
``open_parts`` table the translator writes to (the streaming-closure invariant).

Two input shapes drive parts:

- **Streaming** (``StreamEvent.event`` = raw Anthropic events): ``message_start`` →
  ``content_block_start`` → ``content_block_delta`` → ``content_block_stop`` →
  ``message_delta`` → ``message_stop``. This is the primary path (we enable
  ``include_partial_messages``), giving token-level deltas.
- **Consolidated** (``AssistantMessage``): arrives after a message's stream
  completes. For a stream-driven message it only contributes ``MessageCompleted``
  (parts already finalized); when streaming is OFF it emits the parts in one shot.

Turn lifecycle: the CLI serializes queries and emits exactly one ``ResultMessage``
per query, an interrupted one included (none for an interrupt at idle), so
``ctx.open_queries`` is a FIFO of the queries sent and not yet resulted. The
adapter appends on ``send_prompt``; this translator pops the head on
``ResultMessage`` and emits its ``TurnFinished``; the adapter's synthesize-close
marks a query closed (cancel/crash) without popping, so the result that follows
attributes to the right query and emits nothing.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from alkera_core.schemas.chat import (
    AgentMessageChunk,
    AgentThoughtChunk,
    Event,
    MessageCompleted,
    MessageCreated,
    PartCreated,
    PartStarted,
    PartType,
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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Open-part / open-tool tracking
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _OpenPart:
    """An in-flight text/reasoning part so we can synthesize a closing
    ``PartCreated`` on cancel/crash. Tool parts are NOT tracked here (they have no
    buffered text); they finalize on ``content_block_stop`` / the tool result."""

    message_id: str
    part_id: str
    part_type: PartType
    buffer: list[str] = field(default_factory=list)
    signature: str | None = None


@dataclass(slots=True)
class _OpenTool:
    """An in-flight tool_use block: accumulates the streamed ``input_json_delta``
    until ``content_block_stop`` parses it into the call input."""

    tool_use_id: str
    name: str
    json_buffer: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _OpenQuery:
    """A query written to the CLI whose ``ResultMessage`` has not arrived.
    ``closed`` means the adapter already synthesized its terminal (cancel/crash),
    so the result that still follows must emit nothing."""

    turn_id: str
    closed: bool = False


@dataclass(slots=True)
class _ClaudeTranslatorContext:
    """Mutable state shared (by reference) between adapter + translator."""

    session_id: str
    """The alkera chat id stamped on every IR event (distinct from the SDK's
    own session id, which the adapter pins separately)."""
    open_parts: dict[str, _OpenPart] = field(default_factory=dict)
    open_queries: deque[_OpenQuery] = field(default_factory=deque)
    """Queries sent and not yet resulted, in query order (the CLI's own order).
    Appended by the adapter's ``send_prompt``; popped by ``ResultMessage``."""
    agent_session_id: str | None = None
    """The SDK's own session id, captured from the ``init`` system message — a
    fallback for ``native_state`` if the adapter didn't pin one explicitly."""

    # --- per-message streaming state ---
    current_message_id: str | None = None
    block_parts: dict[int, str] = field(default_factory=dict)
    """Anthropic content-block index → our part_id (text/reasoning blocks)."""
    block_tools: dict[int, _OpenTool] = field(default_factory=dict)
    """Anthropic content-block index → in-flight tool block."""
    stream_message_ids: set[str] = field(default_factory=set)
    """Message ids whose parts were emitted from the stream, so the consolidated
    ``AssistantMessage`` doesn't double-emit them."""
    seen_message_ids: set[str] = field(default_factory=set)
    """Message ids we've already announced via ``MessageCreated``."""

    def close_open_queries(self, *, oldest_only: bool) -> list[_OpenQuery]:
        """The adapter synthesizes a terminal: mark the oldest open query (a
        cancel) or every open query (a crash, a clear) closed and return them,
        oldest first. They stay in the FIFO so the result the CLI still sends
        for each pops it and emits nothing."""
        open_queries = [q for q in self.open_queries if not q.closed]
        closing = open_queries[:1] if oldest_only else open_queries
        for query in closing:
            query.closed = True
        return closing

    def reset(self) -> None:
        """Drop ALL cross-event state (used on a hard ``/clear`` boundary)."""
        self.open_parts.clear()
        self.open_queries.clear()
        self.current_message_id = None
        self.block_parts.clear()
        self.block_tools.clear()
        self.stream_message_ids.clear()
        self.seen_message_ids.clear()


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------


class ClaudeEventTranslator:
    """Stateless-ish dispatcher over Claude-agent-SDK messages."""

    def __init__(self, ctx: _ClaudeTranslatorContext) -> None:
        self._ctx = ctx

    def reset(self) -> None:
        self._ctx.reset()

    # -- entrypoint ----------------------------------------------------------

    def translate(self, message: object) -> list[Event]:
        """Map one SDK message → zero or more IR events."""
        if isinstance(message, StreamEvent):
            return self._translate_stream_event(message.event)
        if isinstance(message, AssistantMessage):
            return self._translate_assistant_message(message)
        if isinstance(message, UserMessage):
            return self._translate_user_message(message)
        if isinstance(message, ResultMessage):
            return self._translate_result(message)
        if isinstance(message, SystemMessage):
            return self._translate_system(message)
        # Unknown SDK message type — surface for visibility, never crash.
        return [
            RawEvent(
                event_id=self._eid(),
                time=self._now(),
                session_id=self._sid(),
                event_type=type(message).__name__,
            )
        ]

    # -- streaming (raw Anthropic events) -----------------------------------

    def _translate_stream_event(self, event: dict[str, Any]) -> list[Event]:
        etype = event.get("type")
        if etype == "message_start":
            return self._on_message_start(event)
        if etype == "content_block_start":
            return self._on_block_start(event)
        if etype == "content_block_delta":
            return self._on_block_delta(event)
        if etype == "content_block_stop":
            return self._on_block_stop(event)
        if etype in ("message_delta", "message_stop", "ping"):
            # message_delta carries the per-message stop_reason/usage, but we
            # surface completion + tokens from the consolidated AssistantMessage
            # (richer + always present). Nothing to emit here.
            return []
        if etype == "error":
            head = self._ctx.open_queries[0].turn_id if self._ctx.open_queries else None
            return [self._status("error", _anthropic_error_text(event.get("error")), head)]
        return []

    def _on_message_start(self, event: dict[str, Any]) -> list[Event]:
        msg = event.get("message")
        message_id = str(msg.get("id", "")) if isinstance(msg, dict) else ""
        if not message_id:
            return []
        ctx = self._ctx
        ctx.current_message_id = message_id
        ctx.block_parts.clear()
        ctx.block_tools.clear()
        ctx.stream_message_ids.add(message_id)
        if message_id in ctx.seen_message_ids:
            return []
        ctx.seen_message_ids.add(message_id)
        return [self._message_created(message_id)]

    def _on_block_start(self, event: dict[str, Any]) -> list[Event]:
        ctx = self._ctx
        index = _as_int(event.get("index"))
        block = event.get("content_block")
        if index is None or not isinstance(block, dict):
            return []
        message_id = ctx.current_message_id or ""
        btype = block.get("type")
        if btype == "tool_use":
            tool_use_id = str(block.get("id") or self._tool_id())
            name = str(block.get("name", ""))
            ctx.block_tools[index] = _OpenTool(tool_use_id=tool_use_id, name=name)
            return [
                ToolCall(
                    event_id=self._eid(),
                    time=self._now(),
                    session_id=self._sid(),
                    tool_call_id=tool_use_id,
                    provider_call_id=tool_use_id,
                    message_id=message_id,
                    tool_name=name,
                    input=_safe_dict(block.get("input")),
                    status="pending",
                )
            ]
        part_type: PartType = "reasoning" if btype == "thinking" else "text"
        part_id = f"{message_id}:{index}"
        ctx.block_parts[index] = part_id
        ctx.open_parts[part_id] = _OpenPart(
            message_id=message_id, part_id=part_id, part_type=part_type
        )
        return [
            PartStarted(
                event_id=self._eid(),
                time=self._now(),
                session_id=self._sid(),
                message_id=message_id,
                part_id=part_id,
                part_type=part_type,
            )
        ]

    def _on_block_delta(self, event: dict[str, Any]) -> list[Event]:
        ctx = self._ctx
        index = _as_int(event.get("index"))
        delta = event.get("delta")
        if index is None or not isinstance(delta, dict):
            return []
        dtype = delta.get("type")
        if dtype == "input_json_delta":
            tool = ctx.block_tools.get(index)
            if tool is not None:
                tool.json_buffer.append(str(delta.get("partial_json", "")))
            return []
        part_id = ctx.block_parts.get(index)
        if part_id is None:
            return []
        open_part = ctx.open_parts.get(part_id)
        if open_part is None:
            return []
        if dtype == "signature_delta":
            open_part.signature = (open_part.signature or "") + str(delta.get("signature", ""))
            return []
        text = ""
        if dtype == "text_delta":
            text = str(delta.get("text", ""))
        elif dtype == "thinking_delta":
            text = str(delta.get("thinking", ""))
        else:
            return []
        if not text:
            return []
        open_part.buffer.append(text)
        seq = len(open_part.buffer) - 1
        if open_part.part_type == "reasoning":
            return [
                AgentThoughtChunk(
                    event_id=self._eid(),
                    time=self._now(),
                    session_id=self._sid(),
                    message_id=open_part.message_id,
                    part_id=part_id,
                    sequence=seq,
                    text=text,
                )
            ]
        return [
            AgentMessageChunk(
                event_id=self._eid(),
                time=self._now(),
                session_id=self._sid(),
                message_id=open_part.message_id,
                part_id=part_id,
                sequence=seq,
                text=text,
            )
        ]

    def _on_block_stop(self, event: dict[str, Any]) -> list[Event]:
        ctx = self._ctx
        index = _as_int(event.get("index"))
        if index is None:
            return []
        tool = ctx.block_tools.pop(index, None)
        if tool is not None:
            return [
                ToolCallUpdate(
                    event_id=self._eid(),
                    time=self._now(),
                    session_id=self._sid(),
                    tool_call_id=tool.tool_use_id,
                    status="running",
                    input=_parse_json_obj("".join(tool.json_buffer)),
                )
            ]
        part_id = ctx.block_parts.pop(index, None)
        if part_id is None:
            return []
        open_part = ctx.open_parts.pop(part_id, None)
        if open_part is None:
            return []
        return [self._finalize_text_part(open_part)]

    # -- consolidated assistant message -------------------------------------

    def _translate_assistant_message(self, msg: AssistantMessage) -> list[Event]:
        message_id = getattr(msg, "message_id", None) or self._synthetic_msg_id()
        ctx = self._ctx
        tokens = _flatten_usage(msg.usage)
        finish = str(getattr(msg, "stop_reason", "") or "")

        # Stream-driven: parts already emitted; only complete the message.
        if message_id in ctx.stream_message_ids:
            return [self._message_completed(message_id, finish, tokens)]

        events: list[Event] = []
        if message_id not in ctx.seen_message_ids:
            ctx.seen_message_ids.add(message_id)
            events.append(self._message_created(message_id))
        for i, block in enumerate(msg.content):
            events.extend(self._oneshot_block(message_id, i, block))
        events.append(self._message_completed(message_id, finish, tokens))
        return events

    def _oneshot_block(self, message_id: str, index: int, block: object) -> list[Event]:
        """Emit a block in one shot (no streaming) — PartStarted+PartCreated for
        text/reasoning, ToolCall for tool_use."""
        if isinstance(block, ToolUseBlock):
            return [
                ToolCall(
                    event_id=self._eid(),
                    time=self._now(),
                    session_id=self._sid(),
                    tool_call_id=block.id,
                    message_id=message_id,
                    tool_name=block.name,
                    input=_safe_dict(block.input),
                    status="running",
                )
            ]
        part_id = f"{message_id}:{index}"
        if isinstance(block, ThinkingBlock):
            part_type: PartType = "reasoning"
            part: TextPart | ReasoningPart = ReasoningPart(
                part_id=part_id,
                message_id=message_id,
                text=block.thinking,
                signature=block.signature or None,
            )
        elif isinstance(block, TextBlock):
            part_type = "text"
            part = TextPart(part_id=part_id, message_id=message_id, text=block.text)
        else:
            return []
        return [
            PartStarted(
                event_id=self._eid(),
                time=self._now(),
                session_id=self._sid(),
                message_id=message_id,
                part_id=part_id,
                part_type=part_type,
            ),
            PartCreated(
                event_id=self._eid(),
                time=self._now(),
                session_id=self._sid(),
                part=part,
            ),
        ]

    # -- user message (tool results) ----------------------------------------

    def _translate_user_message(self, msg: UserMessage) -> list[Event]:
        content = msg.content
        if not isinstance(content, list):
            # Plain-text user echo of our own prompt — the UI already rendered it.
            return []
        events: list[Event] = []
        for block in content:
            if not isinstance(block, ToolResultBlock):
                continue
            is_error = bool(block.is_error)
            events.append(
                ToolCallUpdate(
                    event_id=self._eid(),
                    time=self._now(),
                    session_id=self._sid(),
                    tool_call_id=block.tool_use_id,
                    status="error" if is_error else "completed",
                    output=_tool_result_output(block.content),
                    error_text=(_tool_result_output(block.content) if is_error else None),
                )
            )
        return events

    # -- result (turn end) --------------------------------------------------

    def _translate_result(self, msg: ResultMessage) -> list[Event]:
        ctx = self._ctx
        # One result per query, in query order: the head is the query this result
        # settles. A closed head (cancel/crash already synthesized its terminal)
        # emits nothing: that is the `error_during_execution` result the CLI sends
        # after an interrupt, which would otherwise read as a harness error and,
        # with a newer query queued behind it, end that query instead.
        if not ctx.open_queries:
            return []
        query = ctx.open_queries.popleft()
        if query.closed:
            return []
        is_error = bool(getattr(msg, "is_error", False)) or str(
            getattr(msg, "subtype", "")
        ).startswith("error")
        stop_reason = _result_stop_reason(msg, is_error)
        tokens = _flatten_usage(getattr(msg, "usage", None))
        cost = getattr(msg, "total_cost_usd", None)
        error_detail = None
        if is_error:
            error_detail = (
                getattr(msg, "result", None) or getattr(msg, "subtype", None) or "harness error"
            )
        return [
            TurnFinished(
                event_id=self._eid(),
                time=self._now(),
                session_id=self._sid(),
                turn_id=query.turn_id,
                stop_reason=stop_reason,
                cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
                tokens=tokens or None,
                error_detail=str(error_detail) if error_detail else None,
            ),
            self._status(
                "error" if is_error else "idle",
                str(error_detail) if error_detail else None,
                query.turn_id,
            ),
        ]

    def _status(self, status: Any, detail: str | None, turn_id: str | None) -> SessionStatusChanged:
        """Every status this translator constructs, stamped with the query it
        settles. `phase` mirrors `status` for terminals."""
        return SessionStatusChanged(
            event_id=self._eid(),
            time=self._now(),
            session_id=self._sid(),
            status=status,
            phase=status,
            detail=detail,
            turn_id=turn_id,
        )

    # -- system -------------------------------------------------------------

    def _translate_system(self, msg: SystemMessage) -> list[Event]:
        # Only the `init` system message carries state we keep (the native session id).
        # EVERY other system message is dropped — including the Claude SDK's native
        # background-task subsystem (TaskStarted/TaskProgress/TaskNotificationMessage,
        # all SystemMessage subclasses). We deliberately suppress that subsystem:
        # backgrounding is OUR asyncio registry, not the CLI's task tool, so its
        # messages must never leak as events (pinned by test_harness_claude_translate).
        # We also never set AgentDefinition.background, so it's never activated at all.
        if msg.subtype == "init":
            sid = msg.data.get("session_id")
            if isinstance(sid, str) and sid:
                self._ctx.agent_session_id = sid
        return []

    # -- helpers ------------------------------------------------------------

    def _finalize_text_part(self, open_part: _OpenPart) -> PartCreated:
        text = "".join(open_part.buffer)
        part: TextPart | ReasoningPart
        if open_part.part_type == "reasoning":
            part = ReasoningPart(
                part_id=open_part.part_id,
                message_id=open_part.message_id,
                text=text,
                signature=open_part.signature,
            )
        else:
            part = TextPart(part_id=open_part.part_id, message_id=open_part.message_id, text=text)
        return PartCreated(
            event_id=self._eid(),
            time=self._now(),
            session_id=self._sid(),
            part=part,
        )

    def _message_created(self, message_id: str) -> MessageCreated:
        return MessageCreated(
            event_id=self._eid(),
            time=self._now(),
            session_id=self._sid(),
            message_id=message_id,
            role="assistant",
        )

    def _message_completed(
        self, message_id: str, finish: str, tokens: dict[str, int]
    ) -> MessageCompleted:
        return MessageCompleted(
            event_id=self._eid(),
            time=self._now(),
            session_id=self._sid(),
            message_id=message_id,
            finish_reason=finish,
            tokens=tokens,
        )

    def _sid(self) -> str:
        return self._ctx.session_id

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _eid(self) -> str:
        return secrets.token_hex(10)

    def _tool_id(self) -> str:
        return f"toolu_{secrets.token_hex(8)}"

    def _synthetic_msg_id(self) -> str:
        return f"msg_{secrets.token_hex(8)}"


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_json_obj(raw: str) -> dict[str, Any]:
    """Parse accumulated ``input_json_delta`` text into the tool input dict.
    Tolerates an empty/partial buffer (returns ``{}``)."""
    raw = raw.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_result_output(content: str | list[dict[str, Any]] | None) -> str:
    """Flatten an Anthropic tool_result content into a string for the IR."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") and block.get("type") != "text":
                parts.append(json.dumps(block))
    return "\n".join(parts) if parts else json.dumps(content)


def _flatten_usage(usage: Any) -> dict[str, int]:
    """Anthropic ``usage`` → flat IR token dict, promoting cache buckets to the
    ``cache_read`` / ``cache_write`` names the manifest aggregator expects."""
    if not isinstance(usage, dict):
        return {}
    out: dict[str, int] = {}
    mapping = {
        "input_tokens": "input",
        "output_tokens": "output",
        "cache_read_input_tokens": "cache_read",
        "cache_creation_input_tokens": "cache_write",
    }
    for src, dst in mapping.items():
        v = usage.get(src)
        if isinstance(v, (int, float)):
            out[dst] = int(v)
    if "input" in out or "output" in out:
        out["total"] = out.get("input", 0) + out.get("output", 0)
    return out


def _result_stop_reason(msg: ResultMessage, is_error: bool) -> Any:
    if is_error:
        if str(getattr(msg, "subtype", "")) == "error_max_turns":
            return "max_turn_requests"
        return "error"
    raw = getattr(msg, "stop_reason", None)
    mapping = {
        "end_turn": "end_turn",
        "max_tokens": "max_tokens",
        "tool_use": "end_turn",
        "stop_sequence": "end_turn",
        "refusal": "refusal",
    }
    return mapping.get(str(raw), "end_turn")


def _anthropic_error_text(error: Any) -> str:
    if isinstance(error, dict):
        msg = error.get("message")
        if isinstance(msg, str) and msg:
            return msg
        etype = error.get("type")
        if isinstance(etype, str) and etype:
            return etype
    if isinstance(error, str) and error:
        return error
    return "harness error"


__all__ = [
    "ClaudeEventTranslator",
]
