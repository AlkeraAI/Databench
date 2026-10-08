"""Minimal async HTTP/1.1 server speaking BOTH OpenAI streaming wires —
Chat Completions and the **Responses API** — so it backs two distinct e2e shapes:

1. **Direct** (chat-completions): a real opencode points at this server via
   ``OPENCODE_CONFIG_CONTENT='{"provider":{"mock":{"npm":"@ai-sdk/openai-compatible",
   "options":{"baseURL":"http://127.0.0.1:<port>/v1","apiKey":"test"}}}}'``. The
   provider id is ``mock`` (not ``openai``) on purpose — opencode special-cases the
   built-in ``openai`` id to force the Responses API; see
   ``_helpers/opencode_runner._build_oc_config``. Pass a ``MockScript`` (the
   ``script`` arg) of ``ChatCompletionChunk`` lists.

2. **Gateway-proxied** (Responses): the model-gateway's ``OpenAIResponsesTransport``
   POSTs ``/v1/responses`` here (``settings.openai_base_url`` → this server). The
   real opencode upstream is ``@ai-sdk/openai`` (default ``languageModel`` IS the
   Responses API). Pass a ``ResponsesScript`` (the ``responses_script`` arg) of
   ``ResponsesEvent`` lists built with ``responses_text`` / ``responses_thinking``
   / ``responses_tool_use``.

Tests script the model's behavior so e2e runs are fully deterministic — no
dependency on a remote LLM, no real network.

The protocol surface we honor:

  POST /v1/chat/completions   (Chat Completions; `data: {...}` chunks → `data: [DONE]`)
  POST /v1/responses          (Responses API; `event: <type>` + `data: {...}` frames,
                               ending in `response.completed` — NO `[DONE]`)
  GET  /v1/models             (minimal list, for the direct provider-config check)

Scripting (both wires): the selector matches the last user-message string exactly
(stripped); if no entry matches, the ``"*"`` default runs. The Responses selector
also routes a tool-result follow-up turn (a trailing ``function_call_output`` item)
to ``"*"`` — mirroring the Anthropic mock's empty-text routing.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from collections.abc import Sequence
from typing import Any, TypeAlias

# A "chunk" is the OpenAI ChatCompletion streaming-chunk dict shape:
# {"id", "object": "chat.completion.chunk", "created", "model",
#  "choices": [{"index", "delta": {"role"?,"content"?,"tool_calls"?},
#               "finish_reason"?}]}
ChatCompletionChunk: TypeAlias = dict[str, Any]
MockScript: TypeAlias = dict[str, Sequence[ChatCompletionChunk]]

# A Responses "event" is one streamed SSE frame's data JSON; it self-describes via
# a ``type`` field (``response.created``, ``response.output_text.delta``,
# ``response.completed``, …). The @ai-sdk/openai Responses parser switches on it.
ResponsesEvent: TypeAlias = dict[str, Any]
ResponsesScript: TypeAlias = dict[str, Sequence[ResponsesEvent]]

# opencode's compaction agent appends its SUMMARY_TEMPLATE (compaction.ts)
# as the LAST user message of the summarization call. This substring is the
# reliable signal that "this request is a compaction" — distinct from any
# normal turn — so the mock can return a scripted summary the same way the
# real provider would.
COMPACTION_MARKER = "single-sentence task summary"

#: Optional script key for the turn AFTER a tool ran (the request whose last
#: message is the tool result). A script that returns a `tool_call` from "*"
#: uses this to return plain text on the follow-up — so a multi-step tool turn
#: terminates instead of re-firing the tool. Absent → follow-ups fall back to
#: "*" (the legacy behaviour, unchanged for existing single-default scripts).
FOLLOWUP_KEY = "__after_tool__"


# ---------------------------------------------------------------------------
# Chunk builders — small helpers so scripts read naturally
# ---------------------------------------------------------------------------


def _chunk(
    *,
    completion_id: str,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> ChatCompletionChunk:
    body: ChatCompletionChunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return body


def text_chunks(
    text: str,
    *,
    finish: bool = True,
    usage_total: int | None = None,
    finish_reason: str = "stop",
) -> list[ChatCompletionChunk]:
    """Build a streaming text response: role chunk → one content chunk
    per character → finish chunk. Realistic granularity for a test
    that wants to verify the harness's delta path.

    `usage_total` appends a trailing usage-only chunk (OpenAI's
    `include_usage` shape) so opencode records that many tokens — used to
    overflow a small model context limit and trigger AUTO compaction.

    `finish_reason="length"` is the provider's "I hit the output cap" report:
    the answer stops mid-thought and opencode goes idle with no turn result."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    model = "mock-model"
    out: list[ChatCompletionChunk] = [
        _chunk(
            completion_id=completion_id,
            model=model,
            delta={"role": "assistant"},
        )
    ]
    for ch in text:
        out.append(
            _chunk(
                completion_id=completion_id,
                model=model,
                delta={"content": ch},
            )
        )
    if finish:
        out.append(
            _chunk(
                completion_id=completion_id,
                model=model,
                delta={},
                finish_reason=finish_reason,
            )
        )
    if usage_total is not None:
        # Usage-only final chunk (empty choices) — the AI SDK reads this
        # and opencode uses it for the overflow check.
        out.append(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": usage_total,
                    "completion_tokens": 0,
                    "total_tokens": usage_total,
                },
            }
        )
    return out


def tool_call_chunks(
    tool_name: str,
    arguments: dict[str, Any] | str,
    *,
    call_id: str | None = None,
    finish: bool = True,
) -> list[ChatCompletionChunk]:
    """Build a streaming tool-call response: role chunk → tool-call
    name chunk → argument chunks → finish_reason="tool_calls"."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    model = "mock-model"
    cid = call_id or f"call_{secrets.token_hex(6)}"
    args_str = arguments if isinstance(arguments, str) else json.dumps(arguments)
    out: list[ChatCompletionChunk] = [
        _chunk(
            completion_id=completion_id,
            model=model,
            delta={"role": "assistant"},
        ),
        _chunk(
            completion_id=completion_id,
            model=model,
            delta={
                "tool_calls": [
                    {
                        "index": 0,
                        "id": cid,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": ""},
                    }
                ]
            },
        ),
        _chunk(
            completion_id=completion_id,
            model=model,
            delta={
                "tool_calls": [
                    {
                        "index": 0,
                        "function": {"arguments": args_str},
                    }
                ]
            },
        ),
    ]
    if finish:
        out.append(
            _chunk(
                completion_id=completion_id,
                model=model,
                delta={},
                finish_reason="tool_calls",
            )
        )
    return out


def parallel_tool_call_chunks(
    calls: list[tuple[str, dict[str, Any] | str]],
) -> list[ChatCompletionChunk]:
    """A SINGLE assistant turn emitting MULTIPLE tool calls (one per index) — the
    wire shape for parallel tool use. The harness fires them concurrently; used to
    prove wall-clock OVERLAP of two ``spawn_agent`` calls in one turn."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    model = "mock-model"
    out: list[ChatCompletionChunk] = [
        _chunk(completion_id=completion_id, model=model, delta={"role": "assistant"})
    ]
    for index, (tool_name, arguments) in enumerate(calls):
        cid = f"call_{secrets.token_hex(6)}"
        args_str = arguments if isinstance(arguments, str) else json.dumps(arguments)
        out.append(
            _chunk(
                completion_id=completion_id,
                model=model,
                delta={
                    "tool_calls": [
                        {
                            "index": index,
                            "id": cid,
                            "type": "function",
                            "function": {"name": tool_name, "arguments": ""},
                        }
                    ]
                },
            )
        )
        out.append(
            _chunk(
                completion_id=completion_id,
                model=model,
                delta={"tool_calls": [{"index": index, "function": {"arguments": args_str}}]},
            )
        )
    out.append(
        _chunk(completion_id=completion_id, model=model, delta={}, finish_reason="tool_calls")
    )
    return out


# ---------------------------------------------------------------------------
# Responses-API event builders — the @ai-sdk/openai (v3) Responses stream wire.
# Field shapes mirror exactly what that SDK's stream parser reads:
#   output_text.delta            → item_id + delta
#   function_call_arguments.delta→ output_index + delta
#   output_item.done(function)   → item.{call_id,name,arguments} (arguments = the
#                                  FINAL JSON string — the tool-call input)
#   reasoning_summary_text.delta → item_id + summary_index + delta
#   response.completed           → response.usage (the only usage carrier)
# ---------------------------------------------------------------------------


def _responses_usage(input_tokens: int, output_tokens: int, cached_tokens: int) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached_tokens},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": input_tokens + output_tokens,
    }


def _resp_created(rid: str) -> ResponsesEvent:
    return {
        "type": "response.created",
        "response": {
            "id": rid,
            "object": "response",
            "status": "in_progress",
            "model": "mock-model",
            "output": [],
        },
    }


def _resp_completed(
    rid: str, output: list[dict[str, Any]], usage: dict[str, Any]
) -> ResponsesEvent:
    return {
        "type": "response.completed",
        "response": {
            "id": rid,
            "object": "response",
            "status": "completed",
            "model": "mock-model",
            "output": output,
            "usage": usage,
        },
    }


def responses_text(
    text: str,
    *,
    input_tokens: int = 50,
    output_tokens: int = 10,
    cached_tokens: int = 0,
    reasoning_summary: str | None = None,
    reasoning_encrypted: str | None = None,
) -> list[ResponsesEvent]:
    """A Responses stream: optional reasoning item (summary + encrypted_content for
    stateless continuity) → an assistant message streaming ``text`` → a terminal
    ``response.completed`` carrying usage. ``reasoning_encrypted`` is the blob
    opencode must echo back on the NEXT turn's ``input`` (the continuity anchor)."""
    rid = f"resp_{uuid.uuid4().hex[:12]}"
    output: list[dict[str, Any]] = []
    events: list[ResponsesEvent] = [_resp_created(rid)]
    oidx = 0
    if reasoning_summary is not None:
        rs_id = f"rs_{uuid.uuid4().hex[:10]}"
        done_item = {
            "id": rs_id,
            "type": "reasoning",
            "encrypted_content": reasoning_encrypted,
            "summary": [{"type": "summary_text", "text": reasoning_summary}],
        }
        events += [
            {
                "type": "response.output_item.added",
                "output_index": oidx,
                "item": {
                    "id": rs_id,
                    "type": "reasoning",
                    "encrypted_content": reasoning_encrypted,
                    "summary": [],
                },
            },
            {
                "type": "response.reasoning_summary_part.added",
                "output_index": oidx,
                "item_id": rs_id,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            },
            {
                "type": "response.reasoning_summary_text.delta",
                "output_index": oidx,
                "item_id": rs_id,
                "summary_index": 0,
                "delta": reasoning_summary,
            },
            {
                "type": "response.reasoning_summary_text.done",
                "output_index": oidx,
                "item_id": rs_id,
                "summary_index": 0,
                "text": reasoning_summary,
            },
            {
                "type": "response.reasoning_summary_part.done",
                "output_index": oidx,
                "item_id": rs_id,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": reasoning_summary},
            },
            {"type": "response.output_item.done", "output_index": oidx, "item": done_item},
        ]
        output.append(done_item)
        oidx += 1
    msg_id = f"msg_{uuid.uuid4().hex[:12]}"
    msg_done = {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }
    events += [
        {
            "type": "response.output_item.added",
            "output_index": oidx,
            "item": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "output_index": oidx,
            "item_id": msg_id,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "output_index": oidx,
            "item_id": msg_id,
            "content_index": 0,
            "text": text,
        },
        {"type": "response.output_item.done", "output_index": oidx, "item": msg_done},
    ]
    output.append(msg_done)
    events.append(_responses_usage_event(rid, output, input_tokens, output_tokens, cached_tokens))
    return events


def _responses_usage_event(
    rid: str,
    output: list[dict[str, Any]],
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
) -> ResponsesEvent:
    usage = _responses_usage(input_tokens, output_tokens, cached_tokens)
    return _resp_completed(rid, output, usage)


def responses_thinking(
    summary: str,
    answer: str,
    *,
    input_tokens: int = 50,
    output_tokens: int = 12,
    encrypted: str = "ENC_MOCK_REASONING",
) -> list[ResponsesEvent]:
    """A reasoning turn: a streamed reasoning summary (``summary``) + encrypted
    reasoning blob, then the visible ``answer``. The OpenAI-wire analogue of the
    Anthropic mock's ``thinking_events``."""
    return responses_text(
        answer,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_summary=summary,
        reasoning_encrypted=encrypted,
    )


def responses_tool_use(
    tool_name: str,
    arguments: dict[str, Any] | str,
    *,
    call_id: str | None = None,
    input_tokens: int = 50,
    output_tokens: int = 8,
) -> list[ResponsesEvent]:
    """A Responses function-call turn: a ``function_call`` output item whose final
    ``arguments`` string is the tool input opencode executes. The OpenAI-wire
    analogue of the Anthropic mock's ``tool_use_events``."""
    rid = f"resp_{uuid.uuid4().hex[:12]}"
    fc_id = f"fc_{uuid.uuid4().hex[:10]}"
    cid = call_id or f"call_{secrets.token_hex(6)}"
    args_str = arguments if isinstance(arguments, str) else json.dumps(arguments)
    fc_done = {
        "id": fc_id,
        "type": "function_call",
        "name": tool_name,
        "call_id": cid,
        "arguments": args_str,
        "status": "completed",
    }
    events: list[ResponsesEvent] = [
        _resp_created(rid),
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": fc_id,
                "type": "function_call",
                "name": tool_name,
                "call_id": cid,
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": fc_id,
            "delta": args_str,
        },
        {
            "type": "response.function_call_arguments.done",
            "output_index": 0,
            "item_id": fc_id,
            "arguments": args_str,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": fc_done},
        _responses_usage_event(rid, [fc_done], input_tokens, output_tokens, 0),
    ]
    return events


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


class MockOpenAIServer:
    """Async HTTP/1.1 server serving scripted SSE chat completions.

    Usage::

        script: MockScript = {
            "say hi": text_chunks("Hi!"),
            "*": text_chunks("default"),
        }
        async with MockOpenAIServer(script) as server:
            base_url = server.base_url  # "http://127.0.0.1:<port>/v1"
            # ... point a real opencode at base_url ...
    """

    def __init__(
        self,
        script: MockScript | None = None,
        *,
        responses_script: ResponsesScript | None = None,
        compaction_summary: str | None = None,
        error_on_marker: str | None = None,
        error_status: int = 400,
        error_message: str = "mock injected error",
    ) -> None:
        self._script: MockScript = dict(script or {})
        self._chat_sequence: list[list[ChatCompletionChunk]] = []
        self._error_message = error_message
        self._responses_script: ResponsesScript = dict(responses_script or {})
        """Responses-API scripts (gateway-proxied path). Keyed the same way as
        ``script`` — last user message → events; ``"*"`` default; a trailing
        ``function_call_output`` (tool-result follow-up) routes to ``"*"``."""
        self._compaction_summary = compaction_summary
        """When set, any request carrying opencode's compaction marker (a
        summarization call) gets this text back as the summary — letting an
        e2e exercise opencode's real compaction path deterministically."""
        self._error_on_marker = error_on_marker
        self._error_status = error_status
        """If `error_on_marker` is in a request's last user message, the
        mock returns `error_status` instead of a completion — injects a
        turn failure (or, with `error_on_marker=COMPACTION_MARKER`, a
        compaction failure)."""
        self._server: asyncio.Server | None = None
        self._port: int = 0
        self.requests: list[dict[str, Any]] = []
        """Every parsed JSON body the server received — for assertions."""
        self.paths: list[tuple[str, str]] = []
        """Every `(method, path)` the server saw — for diagnosing which
        endpoint opencode's provider actually hits (chat vs responses)."""

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}/v1"

    def queue_chat_responses(
        self,
        sequence: Sequence[Sequence[ChatCompletionChunk]],
    ) -> None:
        """Queue request-ordered chat responses for deterministic multi-tool turns."""
        self._chat_sequence.extend(list(chunks) for chunks in sequence)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, host="127.0.0.1", port=0)
        sockets = self._server.sockets
        assert sockets, "asyncio.start_server returned no sockets"
        self._port = sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def __aenter__(self) -> MockOpenAIServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            try:
                method, path, _http_version = (
                    request_line.decode("ascii", errors="replace").strip().split(" ", 2)
                )
            except ValueError:
                await self._write_simple(writer, 400, b"")
                return

            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                decoded = line.decode("ascii", errors="replace").rstrip("\r\n")
                if ":" not in decoded:
                    continue
                key, _, val = decoded.partition(":")
                headers[key.strip().lower()] = val.strip()

            content_length = int(headers.get("content-length", "0") or "0")
            body = await reader.readexactly(content_length) if content_length else b""

            self.paths.append((method, path))
            if path.rstrip("/") == "/v1/chat/completions" and method == "POST":
                await self._serve_chat_completion(writer, body, headers)
            elif path.rstrip("/") == "/v1/responses" and method == "POST":
                await self._serve_responses(writer, body, headers)
            elif path == "/v1/models" and method == "GET":
                # opencode lists models on startup to validate the
                # provider config. Return a minimal one.
                payload = json.dumps({"data": [{"id": "mock-model", "object": "model"}]}).encode()
                await self._write_simple(writer, 200, payload, content_type="application/json")
            else:
                await self._write_simple(writer, 404, b"")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _serve_chat_completion(
        self, writer: asyncio.StreamWriter, body: bytes, headers: dict[str, str] | None = None
    ) -> None:
        try:
            request = json.loads(body) if body else {}
        except json.JSONDecodeError:
            await self._write_simple(writer, 400, b"invalid JSON body")
            return
        # Capture the request headers (under a reserved key, atomic with the body
        # append) so e2e tests can assert opencode forwarded e.g.
        # `x-alkera-thinking-display`. Body-reading helpers ignore the key.
        request["__headers__"] = dict(headers or {})
        self.requests.append(request)
        last_user = _last_user_message(request)
        if self._error_on_marker is not None and self._error_on_marker in last_user:
            # Inject a hard provider error (non-retryable 4xx) — opencode
            # surfaces it as session.error / a failed message.
            err = {"error": {"message": self._error_message, "type": "invalid_request_error"}}
            await self._write_simple(
                writer,
                self._error_status,
                json.dumps(err).encode(),
                content_type="application/json",
            )
            return
        # A tool-result follow-up turn routes to a dedicated key (it must NOT
        # re-match the original prompt key, or a tool conversation loops forever).
        # Prefer an explicit FOLLOWUP_KEY (lets a script return a tool_call from
        # "*" for the FIRST turn — robust against an injected mode/system prompt
        # that perturbs the user-message key — and plain text once the tool ran),
        # falling back to "*" for the legacy single-default scripts. Mirrors the
        # Responses selector's function_call_output → "*" rule.
        chunks = self._select_chat_chunks(request, last_user)

        head = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        writer.write(head)
        await writer.drain()
        try:
            for chunk in chunks:
                data = json.dumps(chunk)
                await self._write_chunked(writer, f"data: {data}\n\n".encode())
            await self._write_chunked(writer, b"data: [DONE]\n\n")
            # Zero-length final chunk = end of body for chunked encoding.
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            # Client (opencode) hung up mid-stream — fine, it might
            # have been cancelled.
            pass

    async def _serve_responses(
        self, writer: asyncio.StreamWriter, body: bytes, headers: dict[str, str] | None = None
    ) -> None:
        """Serve a Responses-API stream: `event: <type>` + `data: <json>` frames
        ending in `response.completed` (no `[DONE]`). The gateway POSTs here."""
        try:
            request = json.loads(body) if body else {}
        except json.JSONDecodeError:
            await self._write_simple(writer, 400, b"invalid JSON body")
            return
        request["__headers__"] = dict(headers or {})
        self.requests.append(request)
        last_user = _responses_last_user(request)
        if self._error_on_marker is not None and self._error_on_marker in last_user:
            # Inject a hard provider error — the gateway turns a non-200 into an
            # in-band error frame, which opencode surfaces as a terminal error.
            err = {"error": {"message": self._error_message, "type": "invalid_request_error"}}
            await self._write_simple(
                writer,
                self._error_status,
                json.dumps(err).encode(),
                content_type="application/json",
            )
            return
        events = self._select_responses(last_user)

        head = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        writer.write(head)
        await writer.drain()
        try:
            for event in events:
                frame = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                await self._write_chunked(writer, frame.encode())
            writer.write(b"0\r\n\r\n")  # end chunked body — no `[DONE]` on Responses
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass

    def _select_responses(self, last_user: str) -> Sequence[ResponsesEvent]:
        normalized = last_user.strip()
        if normalized in self._responses_script:
            return self._responses_script[normalized]
        if "*" in self._responses_script:
            return self._responses_script["*"]
        return responses_text("(mock default)")

    async def _write_chunked(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        writer.write(f"{len(payload):x}\r\n".encode())
        writer.write(payload)
        writer.write(b"\r\n")
        await writer.drain()

    async def _write_simple(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        *,
        content_type: str = "text/plain",
    ) -> None:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("ascii")
        writer.write(head + body)
        await writer.drain()

    def _select_chat_chunks(
        self,
        request: dict[str, Any],
        last_user: str,
    ) -> Sequence[ChatCompletionChunk]:
        """Select queued responses before the ordinary prompt/follow-up script."""
        if self._chat_sequence:
            return self._chat_sequence.pop(0)
        if _is_chat_tool_followup(request) and (
            FOLLOWUP_KEY in self._script or "*" in self._script
        ):
            key = FOLLOWUP_KEY if FOLLOWUP_KEY in self._script else "*"
            return self._script[key]
        return self._select_chunks(last_user)

    def _select_chunks(self, last_user: str) -> Sequence[ChatCompletionChunk]:
        # A compaction/summarization call (opencode appends SUMMARY_TEMPLATE
        # as the last user message) → return the scripted summary.
        if self._compaction_summary is not None and COMPACTION_MARKER in last_user:
            return text_chunks(self._compaction_summary)
        normalized = last_user.strip()
        if normalized in self._script:
            return self._script[normalized]
        # Default fallthrough.
        if "*" in self._script:
            return self._script["*"]
        return text_chunks("(mock default)")


def _is_chat_tool_followup(request: dict[str, Any]) -> bool:
    """True when a ChatCompletion request's latest message is a tool result —
    the follow-up turn after the model called a tool. Such turns route to the
    ``"*"`` script so a multi-step tool conversation doesn't re-match (and
    re-fire) the original prompt key, looping forever."""
    messages = request.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    return isinstance(last, dict) and last.get("role") == "tool"


def _last_user_message(request: dict[str, Any]) -> str:
    """Pull the most recent ``role=="user"`` message's text out of a
    ChatCompletion request body. Tolerates both plain-string content
    and the multimodal list-of-parts shape."""
    messages = request.get("messages") or []
    if not isinstance(messages, list):
        return ""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("type") in (None, "text")
            )
    return ""


def _responses_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") in (None, "input_text", "output_text", "text")
        )
    return ""


def _responses_last_user(request: dict[str, Any]) -> str:
    """Pull the routing key out of a Responses-API request's ``input``. Walks the
    items in reverse: a trailing ``function_call_output`` (a tool-result follow-up
    turn) returns ``""`` so it routes to the ``"*"`` script (mirrors the Anthropic
    mock's empty-text routing); otherwise returns the latest user message's text.
    Skips reasoning / function_call / assistant items."""
    inp = request.get("input")
    if isinstance(inp, str):
        return inp
    if not isinstance(inp, list):
        return ""
    for item in reversed(inp):
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "function_call_output":
            return ""  # tool-result follow-up → "*"
        if item.get("role") == "user":
            return _responses_content_text(item.get("content"))
    return ""


__all__ = [
    "COMPACTION_MARKER",
    "FOLLOWUP_KEY",
    "ChatCompletionChunk",
    "MockOpenAIServer",
    "MockScript",
    "ResponsesEvent",
    "ResponsesScript",
    "responses_text",
    "responses_thinking",
    "responses_tool_use",
    "text_chunks",
    "tool_call_chunks",
]
